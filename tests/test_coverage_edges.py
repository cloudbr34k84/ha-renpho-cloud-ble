"""Defensive protocol paths and uncommon BLE lifecycle scenarios."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.renpho import ble_protocol as proto, ble_scale
from custom_components.renpho.cloud_api import RenphoAuthError, RenphoRateLimited
from custom_components.renpho.diagnostics import _mask_mac

from .test_ble import ALEX, ADVERT, MAC, DEVICE, PERSON, FakeClient, frame, m14, run_session, service_info, setup_scale
from .test_morphoscan import FakeMscClient, MSC_ADVERT
from .test_silver import setup_cloud


def test_protocol_rejects_malformed_inputs():
    # A malformed address cannot be compared to the advertisement's MAC.
    advert = proto.parse_advert(ADVERT, "zz:zz:zz:zz:zz:zz")
    assert advert is not None and advert.model_id == 3
    good = m14(82.4, 500, final=True)
    buffer = bytearray(b"\x55\xaa\x14\xff\xff" + good)
    assert list(proto.iter_frames(buffer)) == [proto.Frame(proto.CMD_MEASUREMENT, good[5:-1])]
    assert buffer == bytearray()
    assert proto.parse_msc_weight(proto.Frame(proto.MSC_FINAL, b"\x01")) is None
    assert proto.parse_msc_status(proto.Frame(proto.MSC_STATUS, b"\x01")) is None
    assert proto.parse_msc_record(proto.Frame(proto.MSC_RECORD, b"\x01")) is None
    assert _mask_mac(None) is None
    assert _mask_mac("proxy-name") == "proxy-name"
    assert ble_scale._profile_byte({"scale_profile_byte": "not hex"}) is None


@pytest.mark.parametrize("error", [RenphoAuthError("expired"), RenphoRateLimited(900)])
async def test_device_discovery_propagates_auth_and_rate_limit(hass, error):
    entry = await setup_cloud(hass)
    api = entry.runtime_data.api
    api.token = "token"
    with patch.object(api, "_call", AsyncMock(side_effect=error)) as call:
        with pytest.raises(type(error)):
            await api.latest_measurements()
    call.assert_awaited_once()  # these errors must not trigger an empty-body retry


async def test_morphoscan_invalid_frames_and_hold_timer(hass, adv_callbacks, caplog):
    entry = await setup_scale(hass, model_id=0x0041)
    client = FakeMscClient()

    async def script(client):
        for cmd in (proto.MSC_STATUS, proto.MSC_FINAL, proto.MSC_RECORD):
            client.push(proto.NOTIFY_UUID, frame(cmd, b"\x01"))
        client.push(proto.NOTIFY_UUID, frame(0xEF, b"unknown"))
        client.push(proto.NOTIFY_UUID, frame(0xEF, b"unknown"))
        client.push(proto.NOTIFY_UUID, frame(proto.MSC_FINAL, (8240).to_bytes(2, "big")))
        client.push(proto.NOTIFY_UUID, frame(proto.MSC_FINAL, (8240).to_bytes(2, "big")))

    with patch.object(ble_scale, "MSC_RECORD_HOLD", 0):
        await run_session(hass, adv_callbacks, entry, script, client=client, advert=MSC_ADVERT)
    assert entry.runtime_data.unknown_cmds == {0xEF}
    assert caplog.text.count("unhandled frame 0xef") == 1
    assert entry.runtime_data.scale["weight"] == 82.4
    assert entry.runtime_data._done.is_set()


async def test_guest_profile_without_matching_person(hass, adv_callbacks):
    entry = await setup_scale(hass, options={"allow_writes": "always"})
    client = FakeClient()
    written = asyncio.Event()
    original_write = client.write_gatt_char

    async def write(*args, **kwargs):
        await original_write(*args, **kwargs)
        written.set()

    async def script(client):
        for _ in range(5):
            client.push(proto.NOTIFY_UUID, m14(200))
        await written.wait()
        client.push(proto.NOTIFY_UUID, m14(200, 0, final=True))

    with patch.object(client, "write_gatt_char", write), patch.object(ble_scale, "PROFILE_GRACE", 0):
        await run_session(hass, adv_callbacks, entry, script, client=client)
    assert client.writes[-1][-2] == 0x06  # guest fallback disables impedance
    assert entry.runtime_data.scale["profile_id"] is None
    assert hass.states.get(f"{DEVICE}_last_person").state == "unassigned"


async def test_scale_composition_used_for_confirmed_guest(hass, adv_callbacks):
    entry = await setup_scale(hass, options={"allow_writes": "always"}, profiles=(ALEX,))
    client = FakeClient()
    written = asyncio.Event()
    original_write = client.write_gatt_char

    async def write(*args, **kwargs):
        await original_write(*args, **kwargs)
        written.set()

    async def script(client):
        for _ in range(5):
            client.push(proto.NOTIFY_UUID, m14(82.4))
        await written.wait()
        payload = b"\x01\x00\x00" + (8240).to_bytes(4, "big") + (500).to_bytes(2, "big") + (255).to_bytes(2, "big") + (227).to_bytes(2, "big")
        client.push(proto.NOTIFY_UUID, frame(proto.CMD_EXT_MEASUREMENT, payload))

    with patch.object(client, "write_gatt_char", write), patch.object(ble_scale, "PROFILE_GRACE", 0):
        await run_session(hass, adv_callbacks, entry, script, client=client)
    assert float(hass.states.get(f"{PERSON}_body_fat").state) == 22.7


async def test_ble_defensive_helpers_and_orphan_disconnect(hass, adv_callbacks):
    entry = await setup_scale(hass, profiles=({**ALEX, "start_weight": None},))
    scale = entry.runtime_data
    sid = next(iter(entry.subentries))
    assert scale._match_profile(82.4) == (None, None)
    scale.reassign_last(sid)
    assert scale.profiles == {}
    assert not await scale._write(b"test", "disconnected")
    with patch.object(ble_scale, "PROFILE_GRACE", 0):
        await scale._send_profile_after_grace()
    assert list(scale.writes) == []
    # A client left connected outside an active session still gets closed on unload.
    client = FakeClient()
    scale._client = client
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not client.is_connected
    assert adv_callbacks == []


async def test_reassignment_removes_wrong_person_first_reading(hass, adv_callbacks):
    entry = await setup_scale(hass)
    scale = entry.runtime_data
    alex, sam = entry.subentries
    scale.record_measurement(82.4, 500, "final")
    assert scale.profiles[alex]["previous"] is None
    await hass.services.async_call("renpho", "reassign_last_measurement",
        {"config_entry_id": entry.entry_id, "profile_id": sam}, blocking=True)
    assert alex not in scale.profiles
    assert scale.profiles[sam]["weight"] == 82.4


async def test_rssi_diagnostic_can_be_enabled(hass, adv_callbacks):
    entry = await setup_scale(hass)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", "renpho", f"{MAC}_rssi")
    assert registry.async_get(entity_id).disabled
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    scale = entry.runtime_data
    scale._cooldown_until = ble_scale.dt_util.utcnow() + timedelta(hours=1)
    adv_callbacks[0](service_info(), None)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "-73"
