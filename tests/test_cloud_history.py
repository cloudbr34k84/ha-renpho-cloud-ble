"""Cloud: body-composition-analyser sensors and the history import into statistics.

Records are synthetic but shaped like a live 8-electrode (MorphoScan) record.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.renpho.cloud_api import RenphoCloud
from custom_components.renpho.const import DOMAIN

@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock: Recorder, enable_custom_integrations):
    """The recorder has to come up before hass, so it goes first here."""
    yield


HOUR = 3600
T0 = 1_790_000_000 - 1_790_000_000 % HOUR  # an hour boundary


def morphoscan(ts: int, weight: float, uid: str = "1001") -> dict:
    return {
        "id": ts, "userId": uid, "bUserId": uid, "timeStamp": ts, "timeZone": "AEST",
        "weight": weight, "bmi": 27.0, "bodyfat": 30.0, "water": 50.0, "muscle": 38.0, "bone": 3.9,
        "bmr": 1700, "visfat": 12.0, "subfat": 22.0, "protein": 14.0, "bodyage": 40, "sinew": 55.0,
        "fatFreeWeight": 60.0, "sinewRatio": 58.0, "resistance": True,
        "weightMin": 60.0, "weightMax": 80.0, "weightStd": 70.0, "bfpMin": 10.0, "bfpMax": 20.0, "bfpStd": 15.0,
        "bmrMin": 1500, "bmrMax": 1800, "bmrStd": 1650, "waterMassMin": 38.0, "waterMassMax": 47.0,
        "proteinMassMin": 10.0, "proteinMassMax": 13.0, "muscleMassMin": 45.0, "muscleMassMax": 58.0,
        "bfmMin": 8.0, "bfmMax": 16.0, "bfmStd": 10.0, "boneMin": 3.4, "boneMax": 4.2,
        "smmMass": 33.0, "smmMin": 28.0, "smmMax": 35.0, "smmStd": 31.0, "smi": 8.5,
        "bodyScore": 70.0, "bodyType": 5, "whr": 0.95, "obesityDegree": 120,
        "weightTarget": 72.0, "weightControl": -8.0, "bfmCtrl": -8.0, "ffmCtrl": 0.0,
        "laBodyFatMass": 2.0, "laBodyFatPct": 300.0, "laBodyFatStd": 0.67, "laMuscle": 95.0, "laMuscleMass": 3.5, "laMuscleStd": 3.7,
        "raBodyFatMass": 2.0, "raBodyFatPct": 298.0, "raBodyFatStd": 0.67, "raMuscle": 96.0, "raMuscleMass": 3.6, "raMuscleStd": 3.7,
        "tBodyFatMass": 15.0, "tBodyFatPct": 350.0, "tBodyFatStd": 4.3, "tMuscle": 92.0, "tMuscleMass": 27.0, "tMuscleStd": 29.3,
        "llBodyFatMass": 4.5, "llBodyFatPct": 260.0, "llBodyFatStd": 1.7, "llMuscle": 100.0, "llMuscleMass": 10.0, "llMuscleStd": 10.0,
        "rlBodyFatMass": 4.4, "rlBodyFatPct": 258.0, "rlBodyFatStd": 1.7, "rlMuscle": 99.0, "rlMuscleMass": 9.9, "rlMuscleStd": 10.0,
        "z20Body": 22.0, "z20HandL": 300.0, "z20HandR": 290.0, "z20FootL": 240.0, "z20FootR": 245.0,
        "z100Body": 19.0, "z100HandL": 265.0, "z100HandR": 255.0, "z100FootL": 215.0, "z100FootR": 220.0,
    }


def classic(ts: int, weight: float, uid: str) -> dict:
    return {"id": ts, "userId": uid, "timeStamp": ts, "weight": weight, "bodyfat": 25.0, "bone": 2.5}


def payload(records: list[dict], users: dict | None = None) -> dict:
    latest = {}
    for r in sorted(records, key=lambda r: r["timeStamp"]):
        latest[r["userId"]] = r
    return {
        "users": users or {"1001": "Alex"}, "scales": [], "latest": latest,
        "records": sorted(records, key=lambda r: r["timeStamp"]), "profiles": {},
    }


def cloud_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="cloud_1001", title="Renpho Cloud (a@b.c)",
        data={"source": "cloud", "email": "a@b.c", "password": "pw"},
    )
    entry.add_to_hass(hass)
    return entry


B = "sensor.renpho_cloud_alex"


async def test_morphoscan_sensors(hass: HomeAssistant) -> None:
    entry = cloud_entry(hass)
    data = payload([morphoscan(T0, 95.0), classic(T0, 61.0, "1002")], {"1001": "Alex", "1002": "Sam"})
    with patch.object(RenphoCloud, "latest_measurements", AsyncMock(return_value=data)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert float(hass.states.get(f"{B}_bone_mass").state) == 3.9
    assert hass.states.get(f"{B}_bone_mass").attributes["unit_of_measurement"] == "kg"
    assert hass.states.get(f"{B}_skeletal_muscle").state == "38.0"  # renamed, entity id from new name
    assert float(hass.states.get(f"{B}_skeletal_muscle_mass").state) == 33.0
    assert float(hass.states.get(f"{B}_body_fat_mass").state) == 28.5  # 95 kg × 30 %
    assert float(hass.states.get(f"{B}_left_arm_fat_mass").state) == 2.0
    assert float(hass.states.get(f"{B}_trunk_muscle_vs_standard").state) == 92.0
    assert float(hass.states.get(f"{B}_muscle_control").state) == 0.0  # zero is a real answer here
    assert hass.states.get(f"{B}_body_type").state == "5"
    weight = hass.states.get(f"{B}_weight").attributes
    assert (weight["normal_min"], weight["normal_max"], weight["standard"]) == (60.0, 80.0, 70.0)
    assert hass.states.get(f"{B}_left_arm_fat_mass").attributes == {
        **hass.states.get(f"{B}_left_arm_fat_mass").attributes, "standard": 0.67,
    }
    registry = er.async_get(hass)
    impedance = registry.async_get(f"{B}_impedance_20_khz_trunk")
    assert impedance is not None and impedance.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    # A 4-electrode scale user gets the classic set only
    assert hass.states.get("sensor.renpho_cloud_sam_weight") is not None
    assert registry.async_get("sensor.renpho_cloud_sam_left_arm_fat_mass") is None
    assert registry.async_get("sensor.renpho_cloud_sam_body_score") is None


async def test_retired_bone_percent_entity_removed(hass: HomeAssistant) -> None:
    entry = cloud_entry(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", DOMAIN, "cloud_1001_1001_bone_percent", config_entry=entry, suggested_object_id="renpho_cloud_alex_bone"
    )
    with patch.object(RenphoCloud, "latest_measurements", AsyncMock(return_value=payload([morphoscan(T0, 95.0)]))):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert registry.async_get(old.entity_id) is None
    assert hass.states.get(f"{B}_bone_mass") is not None


async def _stats(hass: HomeAssistant, *ids: str) -> dict:
    await async_wait_recording_done(hass)
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, dt_util.utc_from_timestamp(T0 - 30 * HOUR), None, set(ids), "hour", None,
        {"mean", "min", "max"},
    )


async def test_history_backfill_then_incremental(hass: HomeAssistant) -> None:
    entry = cloud_entry(hass)
    history = [morphoscan(T0 - 26 * HOUR, 97.0), morphoscan(T0 - 2 * HOUR, 96.0), morphoscan(T0 - 2 * HOUR + 600, 95.0)]
    latest = AsyncMock(return_value=payload(history))
    with patch.object(RenphoCloud, "latest_measurements", latest):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        stats = await _stats(hass, f"{B}_weight", f"{B}_left_leg_muscle_mass", f"{B}_body_type")
        weight = stats[f"{B}_weight"]
        assert [(r["mean"], r["min"], r["max"]) for r in weight] == [(97.0, 97.0, 97.0), (95.5, 95.0, 96.0)]
        assert weight[0]["start"] == T0 - 26 * HOUR  # at the weigh-in's own hour, a day back
        assert len(stats[f"{B}_left_leg_muscle_mass"]) == 2
        assert f"{B}_body_type" not in stats  # an enum code, not a measurement
        # The backfill asked for the full history once
        assert any(call.kwargs.get("everything") for call in latest.await_args_list)

        # Next poll: one new weigh-in, imported from the regular pages only
        latest.reset_mock()
        latest.return_value = payload([*history, morphoscan(T0, 94.0)])
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        weight = (await _stats(hass, f"{B}_weight"))[f"{B}_weight"]
        assert [r["mean"] for r in weight] == [97.0, 95.5, 94.0]
        assert not any(call.kwargs.get("everything") for call in latest.await_args_list)


async def test_history_uses_display_unit_and_service_reimports(hass: HomeAssistant) -> None:
    entry = cloud_entry(hass)
    registry = er.async_get(hass)
    # The weight sensor already exists, shown in pounds
    reg = registry.async_get_or_create("sensor", DOMAIN, "cloud_1001_1001_weight", config_entry=entry,
                                       suggested_object_id="renpho_cloud_alex_weight")
    registry.async_update_entity_options(reg.entity_id, "sensor", {"unit_of_measurement": "lb"})
    data = payload([morphoscan(T0 - 5 * HOUR, 100.0)])
    with patch.object(RenphoCloud, "latest_measurements", AsyncMock(return_value=data)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        weight = (await _stats(hass, reg.entity_id))[reg.entity_id]
        assert weight[0]["mean"] == pytest.approx(220.462, rel=1e-4)

        response = await hass.services.async_call(
            DOMAIN, "import_history", {"config_entry_id": entry.entry_id}, blocking=True, return_response=True
        )
        await hass.async_block_till_done()
    assert response["hours_imported"] > 0


def test_profile_goals_normalised() -> None:
    from custom_components.renpho.cloud_api import profile_goals

    kg = profile_goals(
        {"weightGoal": 80.0, "weightGoalUnit": 1, "weightGoalDate": "2026-12-31", "bodyfatGoal": 20.0,
         "initialWeight": 100.0, "initialBodyfat": 32.0, "waistline": 95, "hip": 105, "heightUnit": 0},
        95.0,
    )
    assert kg == {"weight_goal": 80.0, "weight_goal_date": "2026-12-31", "body_fat_goal": 20.0,
                  "starting_weight": 100.0, "starting_body_fat": 32.0, "waist": 95.0, "hip": 105.0, "weight_to_goal": 15.0}
    lb = profile_goals({"weightGoal": 176.37, "weightGoalUnit": 2, "waistline": 37.4, "heightUnit": 1}, 81.0)
    assert lb["weight_goal"] == 80.0 and lb["weight_to_goal"] == 1.0 and lb["waist"] == 95.0
    # The app stores 0 for "not set" — nothing to report
    assert profile_goals({"weightGoal": 0.0, "bodyfatGoal": 0.0, "waistline": 0, "weightGoalDate": None}, 95.0) == {}
    assert profile_goals({"weightGoalDate": 1_798_675_200_000}, None) == {"weight_goal_date": "2026-12-31"}


async def test_goal_sensors_only_once_set(hass: HomeAssistant) -> None:
    entry = cloud_entry(hass)
    data = payload([morphoscan(T0, 95.0)])
    data["profiles"] = {"1001": {"weight_goal": 80.0, "weight_to_goal": 15.0, "weight_goal_date": "2026-12-31", "waist": 95.0}}
    with patch.object(RenphoCloud, "latest_measurements", AsyncMock(return_value=data)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert float(hass.states.get(f"{B}_weight_goal").state) == 80.0
    assert float(hass.states.get(f"{B}_weight_to_goal").state) == 15.0
    assert hass.states.get(f"{B}_weight_goal_date").state == "2026-12-31"
    assert hass.states.get(f"{B}_waist").attributes["unit_of_measurement"] == "cm"
    registry = er.async_get(hass)
    assert registry.async_get(f"{B}_body_fat_goal") is None  # not set in the app
    assert float(hass.states.get(f"{B}_muscle_mass_percentage").state) == 58.0


async def test_heart_rate_dropped_unless_measured(hass: HomeAssistant) -> None:
    entry = cloud_entry(hass)
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "sensor", DOMAIN, "cloud_1001_1001_heart_rate", config_entry=entry, suggested_object_id="renpho_cloud_alex_heart_rate"
    )
    sam = classic(T0, 61.0, "1002") | {"heartRate": 72}
    data = payload([morphoscan(T0, 95.0), sam], {"1001": "Alex", "1002": "Sam"})
    with patch.object(RenphoCloud, "latest_measurements", AsyncMock(return_value=data)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert registry.async_get(stale.entity_id) is None  # Alex's scale doesn't measure it
    assert float(hass.states.get("sensor.renpho_cloud_sam_heart_rate").state) == 72
