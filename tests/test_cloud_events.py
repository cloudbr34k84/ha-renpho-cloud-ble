"""Cloud: the per-person Measurement event, status sensors, and the scale-wake refresh."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.renpho import ble_scale
from custom_components.renpho.cloud_api import RenphoCloud
from custom_components.renpho.const import DOMAIN
from custom_components.renpho.sensor import STATUS_SENSORS

from .test_ble import service_info, setup_scale
from .test_cloud_history import HOUR, T0, classic, cloud_entry, morphoscan, payload

EVENT = "event.renpho_cloud_alex_measurement"
A = "sensor.renpho_cloud_alex"


async def _setup(hass: HomeAssistant, records: list[dict]):
    entry = cloud_entry(hass)
    latest = AsyncMock(return_value=payload(records))
    patcher = patch.object(RenphoCloud, "latest_measurements", latest)
    patcher.start()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, latest, patcher


async def test_event_fires_once_per_new_measurement(hass: HomeAssistant) -> None:
    first = morphoscan(T0, 95.0)
    entry, latest, patcher = await _setup(hass, [first])
    try:
        assert hass.states.get(EVENT).state == "unknown"  # existing reading isn't news

        changes = async_capture_events(hass, EVENT_STATE_CHANGED)
        new = [morphoscan(T0 + HOUR, 94.5), morphoscan(T0 + 2 * HOUR, 94.0)]
        latest.return_value = payload([first, *new])
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        fired = [e.data["new_state"] for e in changes if e.data["entity_id"] == EVENT]
        weights = [s.attributes["weight"] for s in fired]
        assert weights[:2] == [94.5, 94.0]  # one per weigh-in, oldest first
        state = hass.states.get(EVENT)
        assert state.attributes["event_type"] == "measurement"
        assert state.attributes["body_fat"] == 30.0 and state.attributes["time_stamp"] == T0 + 2 * HOUR

        # Same data again: nothing new to fire
        changes.clear()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert not [e for e in changes if e.data["entity_id"] == EVENT]
    finally:
        patcher.stop()


async def test_event_catches_up_after_restart(hass: HomeAssistant) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [(
            State(EVENT, dt_util.utc_from_timestamp(T0).isoformat(), {"event_type": "measurement"}),
            {"last_event_type": "measurement", "last_event_attributes": {"time_stamp": T0, "weight": 95.0}},
        )],
    )
    # A weigh-in happened while HA was down
    entry, latest, patcher = await _setup(hass, [morphoscan(T0, 95.0), morphoscan(T0 + HOUR, 94.2)])
    try:
        state = hass.states.get(EVENT)
        assert state.attributes["weight"] == 94.2 and state.attributes["time_stamp"] == T0 + HOUR
    finally:
        patcher.stop()


async def test_status_sensors(hass: HomeAssistant) -> None:
    sam = classic(T0, 61.0, "1002")  # no ranges in a classic record
    entry = cloud_entry(hass)
    with patch.object(RenphoCloud, "latest_measurements",
                      AsyncMock(return_value=payload([morphoscan(T0, 95.0), sam], {"1001": "Alex", "1002": "Sam"}))):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    expected = {
        "weight_status": "high",  # 95 vs 60–80
        "body_fat_status": "high",  # 30 % vs 10–20
        "body_fat_mass_status": "high",  # 28.5 kg vs 8–16
        "muscle_mass_status": "normal",  # 55 vs 45–58
        "skeletal_muscle_mass_status": "normal",  # 33 vs 28–35
        "bone_mass_status": "normal",  # 3.9 vs 3.4–4.2
        "bmr_status": "normal",  # 1700 vs 1500–1800
        "body_water_status": "high",  # 50 % of 95 kg = 47.5 vs 38–47
        "protein_status": "high",  # 14 % of 95 kg = 13.3 vs 10–13
    }
    assert {d.key for d in STATUS_SENSORS} == set(expected)
    registry = er.async_get(hass)
    for key, value in expected.items():
        state = hass.states.get(registry.async_get_entity_id("sensor", DOMAIN, f"cloud_1001_1001_{key}"))
        assert state.state == value, key
        assert state.attributes["options"] == ["low", "normal", "high"]
    assert registry.async_get("sensor.renpho_cloud_sam_weight_status") is None


def test_status_boundaries() -> None:
    weight = next(d for d in STATUS_SENSORS if d.key == "weight_status")
    assert weight.value_fn({"weight": 59.9, "weightMin": 60, "weightMax": 80}) == "low"
    assert weight.value_fn({"weight": 60.0, "weightMin": 60, "weightMax": 80}) == "normal"  # inclusive
    assert weight.value_fn({"weight": 80.0, "weightMin": 60, "weightMax": 80}) == "normal"
    assert weight.value_fn({"weight": 80.1, "weightMin": 60, "weightMax": 80}) == "high"
    assert weight.value_fn({"weight": 70.0, "weightMin": 0, "weightMax": 0}) is None  # no range sent


async def test_scale_wake_refreshes_cloud(hass: HomeAssistant, adv_callbacks) -> None:
    entry, latest, patcher = await _setup(hass, [morphoscan(T0, 95.0)])
    try:
        await setup_scale(hass, model_id=0x0041)
        calls = latest.await_count
        with patch.object(ble_scale.bluetooth, "async_ble_device_from_address", return_value=None):
            adv_callbacks[0](service_info(), None)  # someone steps on the scale
            await hass.async_block_till_done()
            adv_callbacks[0](service_info(), None)  # still awake: not a new wake-up
            start = dt_util.utcnow()
            async_fire_time_changed(hass, start + timedelta(seconds=95))
            await hass.async_block_till_done()
            assert latest.await_count == calls + 1
            async_fire_time_changed(hass, start + timedelta(seconds=305))
            await hass.async_block_till_done()
            assert latest.await_count == calls + 2
            async_fire_time_changed(hass, start + timedelta(seconds=900))
            await hass.async_block_till_done()
            assert latest.await_count == calls + 2  # nothing else scheduled
    finally:
        patcher.stop()
