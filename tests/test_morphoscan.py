"""MorphoScan family (R-MSC04 / R-MSC06): handshake, decoding and trust rules.

Frame vectors are real R-MSC04 captures published in KristianP26/ble-scale-sync
issue #117 (the record was computed for a 187 cm profile at 95.55 kg). They
are used as data only.
"""

from __future__ import annotations

import asyncio

import pytest
from pytest_homeassistant_custom_component.common import async_capture_events

from homeassistant.core import HomeAssistant

from custom_components.renpho import ble_protocol as proto, ble_scale
from custom_components.renpho.const import DOMAIN
from custom_components.renpho.diagnostics import async_get_config_entry_diagnostics

from .test_ble import (
    ALEX,
    SAM,
    MSC_ADVERT,
    MSC_DEVICE,
    PERSON,
    FakeChar,
    FakeClient,
    FakeService,
    FakeServices,
    run_session,
    setup_scale,
)

IND = proto.INDICATE_UUID
NOTE = proto.NOTIFY_UUID
REC = ["ad040255aa2500240411000025530a00de0c200b", "ae0401ef08e4092a00ae0ac10a9007e108250100", "af0400ed011101b70008ea"]
FINAL_9555 = "55aa240006011100002553b3"
LIVE_9560 = "55aa2100050100002558a3"
HISTORY = ["ad010255aa2600280111000005ad000025710a00", "ae0101e10c2f0bee090e095000b10acb0a8f0800", "af010008430100f0011201b500086a"]
ALEX_187 = {**ALEX, "height_cm": 187, "start_weight": 95.0}


def status(seq: int, state: int, stored: int = 0) -> bytes:
    return proto._frame(proto.MSC_STATUS, bytes([seq, state, 0x01, stored, 0x46]))


class FakeMscClient(FakeClient):
    """A MorphoScan scale: 0x1A10 with 2A10 notify, 2A11 write, 2A12 indicate.

    Like the real one it answers each B2 with ``22 [seq, 09]`` and each B3
    with ``23 [seq, 00]`` on 2A12.
    """

    def __init__(self, answer_profile: bool = True):
        super().__init__()
        self.services = FakeServices([
            FakeService("00001a10-0000-1000-8000-00805f9b34fb", [
                FakeChar(proto.COMMAND_UUID, 10, ["write"]),
                FakeChar(proto.NOTIFY_UUID, 12, ["notify"]),
                FakeChar(proto.INDICATE_UUID, 15, ["indicate"]),
            ]),
            FakeService("0000180a-0000-1000-8000-00805f9b34fb", [FakeChar(proto.FIRMWARE_UUID, 30, ["read"])]),
            # The real R-MSC06 also exposes Nordic legacy DFU (seen in its diagnostics)
            FakeService("00001530-1212-efde-1523-785feabcd123", [
                FakeChar("00001531-1212-efde-1523-785feabcd123", 38, ["write", "notify"]),
            ]),
        ])
        self.answer_profile = answer_profile

    async def read_gatt_char(self, ch):
        return bytearray(b"A1.0.1")

    async def write_gatt_char(self, ch, data, response=False):
        data = bytes(data)
        self.writes.append(data)
        if data[2] == proto.MSC_PROFILE and self.answer_profile:
            self.push(IND, proto._frame(proto.MSC_PROFILE_REPLY, bytes([data[5], 0x09])))
        elif data[2] == proto.MSC_CLOCK:
            self.push(IND, proto._frame(proto.MSC_CLOCK_REPLY, bytes([data[5], 0x00])))

    def ready(self) -> bool:
        return sum(1 for w in self.writes if w[2] == proto.MSC_CLOCK) == 1

    def cmds(self) -> list[int]:
        return [w[2] for w in self.writes]

    def profiles(self) -> list[bytes]:
        return [w for w in self.writes if w[2] == proto.MSC_PROFILE]

    def drop(self) -> None:
        self.is_connected = False
        self.disconnected_callback(self)


async def full_weigh_in(client: FakeMscClient) -> None:
    client.push(IND, status(0, proto.MSC_STATE_MEASURING))
    for _ in range(4):
        client.push(NOTE, bytes.fromhex(LIVE_9560))
    await asyncio.sleep(0.02)  # let a re-profile write go out, as on a real link
    client.push(IND, bytes.fromhex(FINAL_9555))
    client.push(IND, status(1, proto.MSC_STATE_LOCKED))
    client.push(IND, status(2, proto.MSC_STATE_COMPLETE))
    for fragment in REC:
        client.push(IND, bytes.fromhex(fragment))


async def msc_scale(hass, profiles, **kw):
    return await setup_scale(hass, profiles=profiles, model_id=0x0041, **kw)


# ---------------------------------------------------------------- protocol


def test_family_detection() -> None:
    r_msc06 = proto.parse_advert(MSC_ADVERT, "60:65:F4:12:34:56")
    r_msc04 = proto.parse_advert(bytes.fromhex("001200356030f2735d8e0001"), "60:30:F2:73:5D:8E")  # #434
    assert proto.family(r_msc06) == proto.family(r_msc04) == proto.FAMILY_MSC
    assert proto.model_name(r_msc06.model_id) == "R-MSC06" and proto.model_name(r_msc04.model_id) == "R-MSC04"
    basic = proto.parse_advert(bytes.fromhex("000400036065f41234560101"), "60:65:F4:12:34:56")
    assert proto.family(basic) == proto.FAMILY_X55AA and proto.family(None) == proto.FAMILY_X55AA


def test_record_decodes_captured_vector() -> None:
    buf, frames = bytearray(), []
    for fragment in REC:
        buf.extend(proto.strip_fragment(bytes.fromhex(fragment)))
        frames += proto.iter_frames(buf)
    rec = proto.parse_msc_record(frames[0])
    assert rec == proto.MscRecord(
        weight_kg=95.55, body_fat=23.7, bmi=27.3, skeletal_muscle=43.9, visceral_fat=8,
        impedance_20khz=(22.2, 310.4, 305.5, 227.6, 234.6),
        impedance_100khz=(17.4, 275.3, 270.4, 201.7, 208.5),
    )
    assert round(rec.weight_kg / 1.87**2, 1) == rec.bmi  # computed for the 187 cm profile


def test_weights_and_status() -> None:
    (final,) = proto.iter_frames(bytearray.fromhex(FINAL_9555))
    (live,) = proto.iter_frames(bytearray.fromhex(LIVE_9560))
    assert proto.parse_msc_weight(final) == 95.55 and proto.parse_msc_weight(live) == 95.6
    (st,) = proto.iter_frames(bytearray.fromhex("55aa200005031101005089"))
    assert proto.parse_msc_status(st) == proto.MscStatus(seq=3, state=0x11, stored=0)


def test_write_builders() -> None:
    assert proto.build_msc_ack(3) == bytes.fromhex("55aab000020301b5")
    b2 = proto.build_msc_profile(0, 187, 95.55, 0xA9)
    assert b2[:5] == bytes.fromhex("55aab20009") and b2[5:14] == bytes.fromhex("0009074e2553a9ff02")
    assert sum(b2[:-1]) & 0xFF == b2[-1]
    assert proto.build_msc_profile(4, None, None, 0x29)[5:14] == bytes.fromhex("040906a41b5829ff02")  # 170 cm / 70 kg
    b3 = proto.build_msc_clock(1, 0x6ADC2C2A, 36000)  # UTC+10 → 600 minutes
    assert b3[:5] == bytes.fromhex("55aab3000b") and b3[5:16] == bytes.fromhex("010701016adc2c2a025800")
    assert proto.build_msc_clock(1, 0, -5 * 3600)[13:15] == b"\x00\x00"  # west of UTC is sent as 0


# ---------------------------------------------------------------- sessions


async def test_handshake_and_weigh_in_without_profile_byte(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await msc_scale(hass, (ALEX_187,))
    events = async_capture_events(hass, f"{DOMAIN}_measurement")
    client = await run_session(hass, adv_callbacks, entry, full_weigh_in, client=FakeMscClient(), advert=MSC_ADVERT)

    # B2 then B3 on connect, then one B0 ack per status frame
    assert client.cmds()[:2] == [proto.MSC_PROFILE, proto.MSC_CLOCK]
    acks = [w for w in client.writes if w[2] == proto.MSC_ACK]
    assert [a[5] for a in acks] == [0, 1, 2]
    b2 = client.profiles()[0]
    assert b2[5:12] == bytes([0, 9]) + (1870).to_bytes(2, "big") + (9500).to_bytes(2, "big") + b"\xa9"
    assert set(client.handlers) >= {NOTE, IND}
    assert "00001531-1212-efde-1523-785feabcd123" not in client.handlers  # firmware updater left alone

    assert float(hass.states.get(f"{PERSON}_weight").state) == 95.55
    assert float(hass.states.get(f"{PERSON}_bmi").state) == 27.3
    # No profile byte for Alex: the scale's analysis may be for the wrong sex/age
    assert hass.states.get(f"{PERSON}_body_fat").state == "unknown"
    assert hass.states.get(f"{PERSON}_visceral_fat").state == "unknown"
    assert float(hass.states.get(f"{MSC_DEVICE}_last_weight").state) == 95.55
    assert events[0].data["method"] == "final"
    assert events[0].data["scale_record"]["body_fat"] == 23.7
    assert events[0].data["scale_record"]["trusted"] is False


async def test_profile_byte_makes_scale_analysis_trusted(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await msc_scale(hass, ({**ALEX_187, "scale_profile_byte": "29"},))
    events = async_capture_events(hass, f"{DOMAIN}_measurement")
    client = await run_session(hass, adv_callbacks, entry, full_weigh_in, client=FakeMscClient(), advert=MSC_ADVERT)
    assert client.profiles()[0][11] == 0x29
    assert float(hass.states.get(f"{PERSON}_body_fat").state) == 23.7
    assert float(hass.states.get(f"{PERSON}_visceral_fat").state) == 8
    assert float(hass.states.get(f"{PERSON}_skeletal_muscle").state) == 43.9
    assert hass.states.get(f"{PERSON}_body_water").state not in ("unknown", "unavailable")  # derived
    assert events[0].data["scale_record"]["trusted"] is True


async def test_bmi_mismatch_is_not_trusted(hass: HomeAssistant, adv_callbacks) -> None:
    # Configured as 175 cm, but the record's BMI says the scale used 187 cm
    entry = await msc_scale(hass, ({**ALEX_187, "height_cm": 175, "scale_profile_byte": "29"},))
    await run_session(hass, adv_callbacks, entry, full_weigh_in, client=FakeMscClient(), advert=MSC_ADVERT)
    # 175 cm went out in the B2, but this record was computed for 187 cm
    assert hass.states.get(f"{PERSON}_body_fat").state == "unknown"


async def test_unanswered_profile_is_not_trusted(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await msc_scale(hass, ({**ALEX_187, "scale_profile_byte": "29"},))
    await run_session(
        hass, adv_callbacks, entry, full_weigh_in, client=FakeMscClient(answer_profile=False), advert=MSC_ADVERT
    )
    assert float(hass.states.get(f"{PERSON}_weight").state) == 95.55
    assert hass.states.get(f"{PERSON}_body_fat").state == "unknown"


async def test_two_people_reprofile_once_weight_settles(hass: HomeAssistant, adv_callbacks) -> None:
    sam = {**SAM, "scale_profile_byte": "11"}
    entry = await msc_scale(hass, ({**ALEX_187, "scale_profile_byte": "29"}, sam))
    client = await run_session(hass, adv_callbacks, entry, full_weigh_in, client=FakeMscClient(), advert=MSC_ADVERT)
    first, second = client.profiles()
    assert first[11] == proto.MSC_DEFAULT_PROFILE_BYTE  # nobody known yet: placeholder
    assert (first[7] << 8 | first[8]) == 1700
    assert second[5] == 2 and second[11] == 0x29 and (second[7] << 8 | second[8]) == 1870  # Alex, seq 2
    assert float(hass.states.get(f"{PERSON}_body_fat").state) == 23.7
    assert hass.states.get("sensor.renpho_scale_sam_weight").state == "unknown"


async def test_link_drop_before_record_keeps_weight(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await msc_scale(hass, (ALEX_187,))
    events = async_capture_events(hass, f"{DOMAIN}_measurement")

    async def script(client):
        client.push(IND, status(0, proto.MSC_STATE_MEASURING))
        client.push(IND, bytes.fromhex(FINAL_9555))
        client.drop()  # the known ~5.5 s drop

    await run_session(hass, adv_callbacks, entry, script, client=FakeMscClient(), advert=MSC_ADVERT)
    assert float(hass.states.get(f"{PERSON}_weight").state) == 95.55
    assert events[0].data["method"] == "final" and events[0].data["scale_record"] is None


async def test_late_connect_uses_record(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await msc_scale(hass, (ALEX_187,))
    events = async_capture_events(hass, f"{DOMAIN}_measurement")

    async def script(client):
        client.push(IND, status(0, proto.MSC_STATE_LOCKED, stored=0))
        client.push(IND, status(1, proto.MSC_STATE_COMPLETE))
        for fragment in REC:
            client.push(IND, bytes.fromhex(fragment))

    await run_session(hass, adv_callbacks, entry, script, client=FakeMscClient(), advert=MSC_ADVERT)
    assert float(hass.states.get(f"{PERSON}_weight").state) == 95.55
    assert events[0].data["method"] == "record"


async def test_backlog_record_without_final_is_ignored(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await msc_scale(hass, (ALEX_187,))
    events = async_capture_events(hass, f"{DOMAIN}_measurement")

    async def script(client):
        # The scale still holds 3 stored weigh-ins, so a bare record could be one of them
        client.push(IND, status(0, proto.MSC_STATE_COMPLETE, stored=3))
        for fragment in REC:
            client.push(IND, bytes.fromhex(fragment))
        client.push(IND, status(1, proto.MSC_STATE_ENDING))

    await run_session(hass, adv_callbacks, entry, script, client=FakeMscClient(), advert=MSC_ADVERT)
    assert events == []


async def test_history_sync_session(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await msc_scale(hass, (ALEX_187,))
    events = async_capture_events(hass, f"{DOMAIN}_measurement")

    async def script(client):
        client.push(IND, status(0, proto.MSC_STATE_IDLE, stored=3))
        for fragment in HISTORY:
            client.push(IND, bytes.fromhex(fragment))
        client.push(IND, status(1, proto.MSC_STATE_ENDING))  # scale ends the session

    await run_session(hass, adv_callbacks, entry, script, client=FakeMscClient(), advert=MSC_ADVERT)
    assert events == []
    assert entry.runtime_data.history_records == 1
    assert entry.runtime_data._task is None  # 0x04 ended it without waiting for the timeout


async def test_read_only_mode_never_writes(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await msc_scale(hass, (ALEX_187,), options={"allow_writes": "never", "capture_all_characteristics": True})
    client = FakeMscClient()
    client.ready = lambda: {NOTE, IND} <= set(client.handlers)

    async def script(c):
        c.push(IND, status(0, proto.MSC_STATE_MEASURING))
        c.push(IND, bytes.fromhex(FINAL_9555))
        c.drop()

    await run_session(hass, adv_callbacks, entry, script, client=client, advert=MSC_ADVERT)
    assert client.writes == []
    assert float(hass.states.get(f"{PERSON}_weight").state) == 95.55


async def test_diagnostics_keep_profile_frames_private(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await msc_scale(hass, ({**ALEX_187, "scale_profile_byte": "29"},))
    await run_session(hass, adv_callbacks, entry, full_weigh_in, client=FakeMscClient(), advert=MSC_ADVERT)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    ble = diag["ble"]
    assert ble["family"] == proto.FAMILY_MSC
    assert [w["what"] for w in ble["writes"]][:2] == ["b2 profile", "b3 clock"]
    assert "hex" not in ble["writes"][0]  # the B2 carries height, weight and the profile byte
    assert ble["morphoscan"]["record"]["body_fat"] == 23.7
    assert "height_cm" not in ble["morphoscan"]["profile_sent"]
    assert "29" not in str(diag["profiles"])


@pytest.mark.parametrize("byte", ["zz", "123", "g1"])
async def test_profile_byte_validation(hass: HomeAssistant, adv_callbacks, byte) -> None:
    entry = await msc_scale(hass, (ALEX_187,))
    result = await hass.config_entries.subentries.async_init((entry.entry_id, "profile"), context={"source": "user"})
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**SAM, "scale_profile_byte": byte}
    )
    assert result["errors"] == {"scale_profile_byte": "invalid_profile_byte"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**SAM, "scale_profile_byte": " a9 "}
    )
    assert result["type"] == "create_entry"
    assert result["data"]["scale_profile_byte"] == "A9"
