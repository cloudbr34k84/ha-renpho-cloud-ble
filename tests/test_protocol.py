"""Cross-check the ported protocol, body-metrics and crypto against the upstream libraries."""

from __future__ import annotations

from datetime import date
import itertools
import random

import pytest
from renpho import crypto as lib_crypto
from renpho.constants import BODY_WEIGHT_SCALES
from renpho_escs20m import body_metrics as lib_bm
from renpho_escs20m.x55aa import protocol as lib_p

from custom_components.renpho import ble_protocol as proto, body_metrics as bm, cloud_api as cloud

MAC = "60:65:F4:12:34:56"
R_MSC06_ADVERT = bytes.fromhex("001200416065f41234560101")  # real R-MSC06 advert layout (address replaced)


def test_r_msc06_advert() -> None:
    a = proto.parse_advert(R_MSC06_ADVERT, MAC)
    assert a == proto.Advert(prefix=0x0012, model_id=0x0041, trailer=b"\x01\x01")
    assert proto.parse_advert(R_MSC06_ADVERT, "60:65:F4:12:34:57") is None
    assert proto.parse_advert(R_MSC06_ADVERT[:9], MAC) is None
    # The reason the upstream integration rejects this scale:
    assert not lib_p.is_advertisement(R_MSC06_ADVERT, MAC)


@pytest.mark.parametrize("seed", range(5))
def test_frames_match_library(seed: int) -> None:
    rnd = random.Random(seed)
    for _ in range(200):
        w, r = rnd.randint(1000, 20000), rnd.randint(0, 900)
        p14 = bytes([rnd.choice([0, 1, 0x10, 0x11])]) + w.to_bytes(4, "big") + r.to_bytes(2, "big")
        raw = lib_p._build_frame(0x14, p14)
        (mine,) = proto.iter_frames(bytearray(raw))
        (lib,) = lib_p.iter_frames(bytearray(raw))
        m, lm = proto.parse_reading(mine), lib_p.parse_measurement(lib)
        assert (m.weight_kg, m.resistance, m.final, m.zero_current) == (
            lm.weight_kg, lm.resistance, lm.is_final, lm.zero_current,
        )
        p18 = (
            bytes([rnd.choice([1, 0x11]), 1, 9]) + w.to_bytes(4, "big") + r.to_bytes(2, "big")
            + rnd.randint(150, 400).to_bytes(2, "big") + rnd.randint(50, 450).to_bytes(2, "big")
        )
        raw = lib_p._build_frame(0x18, p18)
        m = proto.parse_reading(proto.iter_frames(bytearray(raw))[0])
        lm = lib_p.parse_extended_measurement(lib_p.iter_frames(bytearray(raw))[0])
        assert (m.weight_kg, m.resistance, m.final, m.scale_bmi, m.scale_body_fat) == (
            lm.weight_kg, lm.resistance, lm.is_final, lm.bmi, lm.body_fat,
        )


def test_stream_resync() -> None:
    f1 = lib_p._build_frame(0x14, bytes([0, 0, 0, 0x1F, 0x40, 0, 0]))
    f2 = lib_p._build_frame(0x14, bytes([1, 0, 0, 0x1F, 0x40, 0x01, 0xF4]))
    bad = bytearray(lib_p._build_frame(0x11, bytes(5)))
    bad[-1] ^= 0xFF
    stream = b"\x01\x02" + f1 + bytes(bad) + f2
    buf, got = bytearray(), []
    for i in range(0, len(stream), 7):  # arbitrary notification boundaries
        buf.extend(stream[i : i + 7])
        got += proto.iter_frames(buf)
    assert [g.payload for g in got] == [f1[5:-1], f2[5:-1]]
    final = proto.parse_reading(got[1])
    assert final.final and final.weight_kg == 80.0 and final.resistance == 500
    assert proto.strip_fragment(b"\xad\x00\x01" + f1) == f1


@pytest.mark.parametrize(
    ("female", "athlete", "algo", "imp"),
    list(itertools.product([False, True], [False, True], [0x03, 0x04], [True, False])),
)
def test_guest_profile_matches_library(female, athlete, algo, imp) -> None:
    bday = date(1985, 7, 14)
    mine = proto.build_guest_profile(
        female=female, birthday=bday, height_cm=178.5, last_weight_kg=82.35,
        algorithm=algo, athlete=athlete, measure_impedance=imp,
    )
    profile = lib_p.X55AAProfile(
        sex=lib_bm.Sex.Female if female else lib_bm.Sex.Male, birthday=bday,
        height_m=1.785, athlete=athlete, algorithm=algo,
    )
    assert mine == lib_p.build_guest_profile_command(profile, 82.35, measure_impedance=imp)
    lib_p.assert_allowed_write(mine)  # the library's own write allow-list


def test_body_metrics_match_library() -> None:
    rnd = random.Random(42)
    for _ in range(3000):
        w, h = round(rnd.uniform(30, 160), 2), round(rnd.uniform(1.4, 2.05), 2)
        age, sex, res = rnd.randint(10, 90), rnd.choice([0, 1]), rnd.randint(200, 900)
        algo, ath = rnd.choice([0x03, 0x04]), rnd.choice([False, True])
        lib = lib_bm.calculate_body_fat(w, h, age, lib_bm.Sex(sex), res, algorithm=algo, athlete=ath)
        assert bm.calculate_body_fat(w, h, age, sex, res, algorithm=algo, athlete=ath) == lib
        L = lib_bm.BodyMetrics(w, h, age, lib_bm.Sex(sex), lib)
        assert bm.derive_metrics(w, h, sex, lib) == {
            "bmi": L.body_mass_index, "body_fat": L.body_fat_percentage,
            "fat_free_mass": L.fat_free_mass, "body_water": L.body_water_percentage,
            "skeletal_muscle": L.skeletal_muscle_percentage, "bone_mass": L.bone_mass,
            "muscle_mass": L.muscle_mass, "protein": L.protein_percentage,
            "bmr": L.basal_metabolic_rate,
        }
    assert bm.derive_metrics(80, 1.8, 0, None) == {"bmi": 24.7}


def test_age() -> None:
    assert bm.age_on(date(1990, 10, 25), date(2026, 10, 24)) == 35
    assert bm.age_on(date(1990, 10, 24), date(2026, 10, 24)) == 36


@pytest.mark.parametrize(
    "obj", [{}, {"a": 1}, {"pageNum": 1, "pageSize": 50, "userIds": ["123"], "tableName": "measurements_info_3"}]
)
def test_cloud_crypto_matches_renpho_api(obj) -> None:
    assert cloud._encrypt(obj) == lib_crypto.encrypt_request(obj)
    assert cloud._decrypt(lib_crypto.encrypt_request(obj)["encryptData"]) == obj


def test_cloud_constants() -> None:
    assert cloud._encrypt(None) == lib_crypto.encrypt_empty_bytes()
    assert cloud._SCALE_TYPES == BODY_WEIGHT_SCALES
