"""Silver behavior: failed actions, availability, recovery and cleanup."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, get_system_health_info

from homeassistant.setup import async_setup_component
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.renpho import async_remove_entry
from custom_components.renpho.cloud_api import RenphoCloud, RenphoCloudError
from custom_components.renpho.const import DOMAIN
from custom_components.renpho import ble_scale

from .test_ble import setup_scale, service_info, run_session, DEVICE, PERSON, MAC
from .test_cloud import LATEST


async def setup_cloud(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="cloud_1001",
        data={"source": "cloud", "email": "a@b.c", "password": "pw"})
    entry.add_to_hass(hass)
    with patch.object(RenphoCloud, "latest_measurements", AsyncMock(return_value=LATEST)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_action_validation(hass, adv_callbacks):
    ble = await setup_scale(hass)
    cloud = await setup_cloud(hass)
    sid = next(iter(ble.subentries))
    for action, entry, fields, key in [
        ("reassign_last_measurement", cloud, {"profile_id": sid}, "not_ble_entry"),
        ("import_history", ble, {}, "not_cloud_entry"),
        ("reassign_last_measurement", ble, {"profile_id": "missing"}, "unknown_profile"),
        ("reassign_last_measurement", ble, {"profile_id": sid}, "no_measurement"),
        ("import_history", cloud, {}, "recorder_required"),
    ]:
        with pytest.raises(ServiceValidationError) as error:
            await hass.services.async_call(DOMAIN, action, {"config_entry_id": entry.entry_id, **fields}, blocking=True)
        assert error.value.translation_key == key
        assert error.value.translation_domain == DOMAIN
    # A loaded entry with inconsistent runtime data still produces a user error.
    runtime = cloud.runtime_data
    cloud.runtime_data = None
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(DOMAIN, "import_history", {"config_entry_id": cloud.entry_id}, blocking=True)
    assert error.value.translation_key == "entry_not_loaded"
    cloud.runtime_data = runtime


@pytest.mark.parametrize("error", [RenphoCloudError("offline"), OSError("disk full")])
async def test_history_action_failure(hass, error):
    entry = await setup_cloud(hass)
    hass.config.components.add("recorder")
    try:
        with patch.object(entry.runtime_data.history, "async_reset", AsyncMock()), patch.object(entry.runtime_data.history, "async_sync", AsyncMock(side_effect=error)):
            with pytest.raises(HomeAssistantError) as raised:
                await hass.services.async_call(DOMAIN, "import_history", {"config_entry_id": entry.entry_id}, blocking=True)
        assert raised.value.translation_key == "history_import_failed"
        assert raised.value.translation_placeholders == {"error": str(error)}
    finally:
        hass.config.components.remove("recorder")


async def test_cloud_unavailable_recovers_once(hass, caplog):
    entry = await setup_cloud(hass)
    coordinator = entry.runtime_data
    with patch.object(RenphoCloud, "latest_measurements", AsyncMock(side_effect=RenphoCloudError("offline"))):
        await coordinator.async_refresh()
        await coordinator.async_refresh()
    assert hass.states.get("sensor.renpho_cloud_alex_weight").state == "unavailable"
    assert caplog.text.count("Error fetching renpho_cloud data") == 1
    with patch.object(RenphoCloud, "latest_measurements", AsyncMock(return_value=LATEST)):
        await coordinator.async_refresh()
    assert hass.states.get("sensor.renpho_cloud_alex_weight").state == "82.5"
    assert caplog.text.count("Fetching renpho_cloud data recovered") == 1
    # Missing one field stays unknown without marking every sensor unavailable.
    missing = {**LATEST, "latest": {"1001": {"weight": 82.5}}}
    coordinator.async_set_updated_data(missing)
    assert hass.states.get("sensor.renpho_cloud_alex_body_fat").state == "unknown"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not coordinator._listeners


async def test_ble_transport_failure_and_recovery(hass, adv_callbacks, caplog):
    entry = await setup_scale(hass)
    scale = entry.runtime_data
    with patch.object(ble_scale.bluetooth, "async_ble_device_from_address", return_value=None):
        await scale._session()
        await scale._session()
    assert hass.states.get(f"{DEVICE}_last_weight").state == "unavailable"
    assert hass.states.get(f"{DEVICE}_connection_status").state == "no_connectable_adapter"
    assert caplog.text.count(f"Renpho {MAC} is unavailable") == 1
    scale._cooldown_until = ble_scale.dt_util.utcnow()

    async def script(client):
        from .test_ble import m14
        from custom_components.renpho import ble_protocol as proto
        client.push(proto.NOTIFY_UUID, m14(82.4, 500, final=True))

    await run_session(hass, adv_callbacks, entry, script)
    assert hass.states.get(f"{PERSON}_weight").state == "82.4"
    assert caplog.text.count(f"Renpho {MAC} is available again") == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert adv_callbacks == []
    assert scale._cloud_timers == []


async def test_unload_cancels_active_ble_session(hass, adv_callbacks):
    entry = await setup_scale(hass)
    scale = entry.runtime_data
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def connect(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with patch.object(ble_scale, "establish_connection", connect), patch.object(ble_scale.bluetooth, "async_ble_device_from_address", return_value=object()):
        adv_callbacks[0](service_info(), None)
        await started.wait()
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert cancelled.is_set()
    assert adv_callbacks == []
    assert scale._task is None


async def test_failed_ble_platform_setup_cleans_subscription(hass, adv_callbacks):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data={"source": "ble", "address": MAC})
    entry.add_to_hass(hass)
    with patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(side_effect=RuntimeError("platform failed"))):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert adv_callbacks == []


async def test_cloud_health_and_storage_removal(hass):
    entry = await setup_cloud(hass)
    assert await async_setup_component(hass, "system_health", {})
    with patch("homeassistant.components.system_health.async_check_can_reach_url", AsyncMock(return_value="ok")):
        info = await get_system_health_info(hass, DOMAIN)
    assert info["cloud_accounts"] == 1
    assert await info["can_reach_server"] == "ok"
    with patch("custom_components.renpho.Store.async_remove", AsyncMock()) as remove:
        await async_remove_entry(hass, entry)
    remove.assert_awaited_once()


async def test_ble_gatt_failures_are_diagnostic(hass, adv_callbacks):
    """Unsupported subscriptions and reads do not abort an otherwise valid session."""
    from .test_ble import FakeClient
    entry = await setup_scale(hass)
    client = FakeClient()
    with patch.object(client, "start_notify", AsyncMock(side_effect=OSError("notify rejected"))), patch.object(client, "read_gatt_char", AsyncMock(side_effect=OSError("read rejected"))):
        await entry.runtime_data._prepare(client)
    assert all("OSError" in value for value in entry.runtime_data.subscriptions.values())
    assert any("value_error" in c for svc in entry.runtime_data.gatt_table for c in svc["characteristics"])
    assert "battery" not in entry.runtime_data.scale
    # Transport failures are handled as unavailable, without an unhandled task error.
    with patch.object(ble_scale.bluetooth, "async_ble_device_from_address", return_value=object()), patch.object(ble_scale, "establish_connection", AsyncMock(side_effect=OSError("connect rejected"))):
        await entry.runtime_data._session()
    assert hass.states.get(f"{DEVICE}_last_weight").state == "unavailable"
    assert "connect rejected" in entry.runtime_data.last_error


async def test_ble_bad_frames_and_rejected_guest_write(hass, adv_callbacks):
    from .test_ble import FakeClient, frame, m14
    from custom_components.renpho import ble_protocol as proto
    entry = await setup_scale(hass, options={"allow_writes": "always", "capture_all_characteristics": True})
    client = FakeClient()

    async def script(client):
        client.push(proto.NOTIFY_UUID, frame(0xEE, b"unknown"))
        client.push(proto.NOTIFY_UUID, frame(0xEE, b"unknown"))
        client.push(proto.NOTIFY_UUID, m14(1))
        for _ in range(5):
            client.push(proto.NOTIFY_UUID, m14(82.4))
        await asyncio.sleep(0.02)
        client.push(proto.NOTIFY_UUID, m14(82.4, 500, final=True))

    with patch.object(client, "write_gatt_char", AsyncMock(side_effect=OSError("write rejected"))), patch.object(ble_scale, "PROFILE_GRACE", 0):
        await run_session(hass, adv_callbacks, entry, script, client=client)
    assert entry.runtime_data.unknown_cmds == {0xEE}
    assert entry.runtime_data.writes[-1]["result"] == "OSError: write rejected"
    assert hass.states.get(f"{PERSON}_weight").state == "82.4"


async def test_ble_missing_measurement_characteristic(hass, adv_callbacks, caplog):
    from .test_ble import FakeClient, FakeServices
    entry = await setup_scale(hass)
    client = FakeClient()
    client.services = FakeServices([])
    await entry.runtime_data._prepare(client)
    await entry.runtime_data._prepare(client)
    assert caplog.text.count("has no 0x2A10 characteristic") == 1


async def test_reassign_rejects_missing_runtime(hass, adv_callbacks):
    entry = await setup_scale(hass)
    scale = entry.runtime_data
    entry.runtime_data = None
    try:
        with pytest.raises(ServiceValidationError) as error:
            await hass.services.async_call(DOMAIN, "reassign_last_measurement", {
                "config_entry_id": entry.entry_id, "profile_id": next(iter(entry.subentries))}, blocking=True)
        assert error.value.translation_key == "entry_not_loaded"
    finally:
        entry.runtime_data = scale
