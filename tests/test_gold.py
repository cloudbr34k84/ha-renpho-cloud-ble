"""Gold device lifecycle, Repairs, discovery and private diagnostics."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir

from custom_components.renpho import async_remove_config_entry_device, async_remove_entry
from custom_components.renpho import ble_scale
from custom_components.renpho.const import DOMAIN
from custom_components.renpho.diagnostics import async_get_config_entry_diagnostics
from custom_components.renpho.entity import cloud_scope

from .test_ble import MAC, DEVICE, FakeClient, run_session, service_info, setup_scale, m14
from .test_cloud import LATEST
from .test_silver import setup_cloud


async def test_dynamic_people_stale_removal_and_return(hass):
    entry = await setup_cloud(hass)
    coordinator = entry.runtime_data
    registry = dr.async_get(hass)
    current = registry.async_get_device_by_identifier((DOMAIN, f"{cloud_scope(entry)}_1001"), entry.entry_id)
    assert current.entry_type == dr.DeviceEntryType.SERVICE
    assert not await async_remove_config_entry_device(hass, entry, current)
    data = {**LATEST, "users": {**LATEST["users"], "1003": "New person"},
            "latest": {**LATEST["latest"], "1003": {"weight": 55, "timeStamp": 1792814122}}}
    coordinator.async_set_updated_data(data)
    await hass.async_block_till_done()
    new_device = registry.async_get_device_by_identifier((DOMAIN, f"{cloud_scope(entry)}_1003"), entry.entry_id)
    assert new_device is not None
    assert float(hass.states.get("sensor.renpho_cloud_new_person_weight").state) == 55
    assert hass.states.get("event.renpho_cloud_new_person_measurement") is not None
    coordinator.async_set_updated_data(LATEST)
    await hass.async_block_till_done()
    assert await async_remove_config_entry_device(hass, entry, new_device)
    coordinator.last_update_success = False
    assert not await async_remove_config_entry_device(hass, entry, new_device)
    coordinator.last_update_success = True
    registry.async_remove_device(new_device.id)
    await hass.async_block_till_done()
    coordinator.async_set_updated_data(data)
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.renpho_cloud_new_person_weight").state) == 55
    assert hass.states.get("event.renpho_cloud_new_person_measurement") is not None
    # Removing one device must leave existing people and their entities intact.
    assert hass.states.get("sensor.renpho_cloud_alex_weight").state == "82.5"


async def test_ble_device_info_updates_after_first_connection(hass, adv_callbacks):
    entry = await setup_scale(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier((DOMAIN, MAC), entry.entry_id)
    assert not await async_remove_config_entry_device(hass, entry, device)
    assert not await async_remove_config_entry_device(hass, SimpleNamespace(), device)
    sid = next(iter(entry.subentries))
    profile = registry.async_get_device_by_identifier((DOMAIN, f"{MAC}_{sid}"), entry.entry_id)
    assert profile.entry_type == dr.DeviceEntryType.SERVICE
    assert not await async_remove_config_entry_device(hass, entry, profile)
    assert await async_remove_config_entry_device(hass, entry, SimpleNamespace(identifiers={(DOMAIN, f"{MAC}_removed")}))

    async def script(client):
        from custom_components.renpho import ble_protocol as proto
        client.push(proto.NOTIFY_UUID, m14(82.4, 500, final=True))

    await run_session(hass, adv_callbacks, entry, script)
    device = registry.async_get(device.id)
    assert device.sw_version == "A1.0.1"
    assert device.model_id == "0x0003"


async def test_no_adapter_repair_lifecycle(hass, adv_callbacks):
    entry = await setup_scale(hass)
    scale = entry.runtime_data
    issues = ir.async_get(hass)
    issue_id = f"no_adapter_{entry.entry_id}"
    with patch.object(ble_scale.bluetooth, "async_ble_device_from_address", return_value=None):
        await scale._session()
        await scale._session()
    issue = issues.async_get_issue(DOMAIN, issue_id)
    assert issue.translation_key == "no_connectable_adapter"
    assert issue.data["entry_id"] == entry.entry_id
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert issues.async_get_issue(DOMAIN, issue_id) is issue
    assert await hass.config_entries.async_setup(entry.entry_id)
    entry.runtime_data._cooldown_until = ble_scale.dt_util.utcnow()

    async def script(client):
        from custom_components.renpho import ble_protocol as proto
        client.push(proto.NOTIFY_UUID, m14(82.4, 500, final=True))

    await run_session(hass, adv_callbacks, entry, script)
    assert issues.async_get_issue(DOMAIN, issue_id) is None
    with patch.object(ble_scale.bluetooth, "async_ble_device_from_address", return_value=None):
        await entry.runtime_data._session()
    await async_remove_entry(hass, entry)
    assert issues.async_get_issue(DOMAIN, issue_id) is None


async def test_diagnostics_redact_opaque_payloads_and_errors(hass, adv_callbacks):
    entry = await setup_scale(hass)
    scale = entry.runtime_data
    secret = "PRIVATE-NAME-AND-SERIAL"
    scale.last_error = secret
    scale.raw_log.append({"char": "2a00", "hex": secret.encode().hex()})
    scale.frame_log.append({"cmd": "0xee", "payload": secret.encode().hex()})
    scale.writes.append({"what": "write", "result": secret, "hex": secret.encode().hex()})
    scale.subscriptions["test"] = secret
    scale.gatt_table = [{"service": "test", "characteristics": [{"uuid": "2a25", "value": secret.encode().hex()}]}]
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert secret not in json.dumps(diag)
    assert secret.encode().hex() not in json.dumps(diag)
    assert scale.raw_log[0]["hex"] == secret.encode().hex()  # redaction cannot mutate runtime buffers
    cloud = await setup_cloud(hass)
    cloud.runtime_data.last_exception = RuntimeError(secret)
    cloud.runtime_data.data = {**cloud.runtime_data.data, "scales": [{"firstName": secret, "lastName": secret, "serialNumber": secret}]}
    diag = await async_get_config_entry_diagnostics(hass, cloud)
    assert secret not in json.dumps(diag)
    assert diag["last_exception"] == "RuntimeError"


def test_entity_names_icons_and_diagnostic_metadata():
    """Every descriptor has a translatable name and diagnostic defaults stay explicit."""
    from pathlib import Path
    from homeassistant.const import EntityCategory
    from custom_components.renpho.sensor import ALL_CLOUD_SENSORS, PROFILE_SENSORS, SCALE_SENSORS
    root = Path(__file__).resolve().parents[1] / "custom_components/renpho"
    strings = json.loads((root / "strings.json").read_text())
    icons = json.loads((root / "icons.json").read_text())
    for desc in (*ALL_CLOUD_SENSORS, *PROFILE_SENSORS, *SCALE_SENSORS):
        assert desc.translation_key in strings["entity"]["sensor"]
        assert desc.icon is None
    assert strings["entity"]["event"]["measurement"]["name"]
    assert set(icons["entity"]["sensor"]) <= set(strings["entity"]["sensor"])
    for desc in (*ALL_CLOUD_SENSORS, *PROFILE_SENSORS, *SCALE_SENSORS):
        if desc.key.startswith("impedance"):
            assert desc.entity_category is EntityCategory.DIAGNOSTIC
            assert desc.entity_registry_enabled_default is False


async def test_loaded_rediscovery_reloads_once(hass, adv_callbacks, caplog):
    entry = await setup_scale(hass)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
        )
        await hass.async_block_till_done()
    assert result["reason"] == "already_configured"
    assert entry.data["model_id"] == 0x0041
    reload.assert_called_once_with(entry.entry_id)
    assert "should use it for scheduling a reload" not in caplog.text
