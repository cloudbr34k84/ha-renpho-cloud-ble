"""BLE scale: setup, a full simulated GATT session, matching and services."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch

from bleak.backends.device import BLEDevice
from habluetooth import BluetoothServiceInfoBleak
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    get_system_health_info,
)

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.renpho import ble_protocol as proto, ble_scale
from custom_components.renpho.const import DOMAIN
from custom_components.renpho.diagnostics import async_get_config_entry_diagnostics

MAC = "60:65:F4:12:34:56"
# A basic-flavor 0x55aa scale (prefix 00 04, model 0x0003) at the same address
ADVERT = bytes.fromhex("000400036065f41234560101")
# The R-MSC06's real advertisement (MorphoScan family, prefix 00 12)
MSC_ADVERT = bytes.fromhex("001200416065f41234560101")
ALEX = {
    "name": "Alex", "sex": "male", "birthday": "1990-05-17", "height_cm": 180,
    "start_weight": 83.0, "tolerance": 3.0, "algorithm": "0x04", "athlete": False,
}
SAM = {**ALEX, "name": "Sam", "sex": "female", "height_cm": 165, "start_weight": 62.0}
DEVICE = "sensor.renpho_scale"
MSC_DEVICE = "sensor.renpho_r_msc06"
PERSON = "sensor.renpho_scale_alex"


def frame(cmd: int, payload: bytes) -> bytes:
    body = b"\x55\xaa" + bytes([cmd]) + len(payload).to_bytes(2, "big") + payload
    return body + bytes([sum(body) & 0xFF])


def m14(weight: float, resistance: int = 0, final: bool = False) -> bytes:
    return frame(0x14, bytes([1 if final else 0]) + int(round(weight * 100)).to_bytes(4, "big") + resistance.to_bytes(2, "big"))


class FakeChar:
    def __init__(self, uuid, handle, props):
        self.uuid, self.handle, self.properties = uuid, handle, props


class FakeService:
    def __init__(self, uuid, chars):
        self.uuid, self.characteristics = uuid, chars


class FakeServices(list):
    def get_characteristic(self, uuid):
        for svc in self:
            for ch in svc.characteristics:
                if ch.uuid == uuid:
                    return ch
        return None


class FakeClient:
    """Stands in for the bleak client: LeFu 0x1A10 service + battery + DIS + a vendor char."""

    def __init__(self):
        self.services = FakeServices([
            FakeService("00001a10-0000-1000-8000-00805f9b34fb", [
                FakeChar(proto.NOTIFY_UUID, 10, ["notify"]),
                FakeChar(proto.COMMAND_UUID, 12, ["write"]),
            ]),
            FakeService("0000180f-0000-1000-8000-00805f9b34fb", [FakeChar(proto.BATTERY_UUID, 20, ["read", "notify"])]),
            FakeService("0000180a-0000-1000-8000-00805f9b34fb", [FakeChar(proto.FIRMWARE_UUID, 30, ["read"])]),
        ])
        self.handlers = {}
        self.writes = []
        self.is_connected = True

    async def read_gatt_char(self, ch):
        return {proto.BATTERY_UUID: bytearray([87]), proto.FIRMWARE_UUID: bytearray(b"A1.0.1")}[ch.uuid]

    async def start_notify(self, ch, cb):
        self.handlers[ch.uuid] = cb

    async def write_gatt_char(self, ch, data, response=False):
        self.writes.append(bytes(data))

    async def disconnect(self):
        self.is_connected = False

    def push(self, uuid, data: bytes):
        self.handlers[uuid](None, bytearray(data))

    def ready(self) -> bool:
        return proto.NOTIFY_UUID in self.handlers


def service_info(advert: bytes = MSC_ADVERT) -> BluetoothServiceInfoBleak:
    return BluetoothServiceInfoBleak(
        name="R-MSC06", address=MAC, rssi=-73, manufacturer_data={0x1A10: advert},
        service_data={}, service_uuids=[], source="F4:12:FA:9E:1F:AA",
        device=BLEDevice(MAC, "R-MSC06", None), advertisement=None, connectable=True,
        time=time.monotonic(), tx_power=None,
    )


async def setup_scale(
    hass: HomeAssistant, options=None, profiles=(ALEX, SAM), model_id: int = 0x0003
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title=f"Renpho {proto.model_name(model_id)} ({MAC})", unique_id=MAC,
        data={"source": "ble", "address": MAC, "model_id": model_id},
        options=options or {"allow_writes": "auto", "capture_all_characteristics": True},
        subentries_data=[
            {"data": p, "subentry_type": "profile", "title": p["name"], "unique_id": None} for p in profiles
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def run_session(hass, callbacks, entry, script, client=None, advert: bytes = ADVERT):
    """Advertise, let the session connect, run ``script(client)``, wait for the session to end."""
    client = client or FakeClient()

    async def fake_connect(*args, **kwargs):
        client.disconnected_callback = kwargs.get("disconnected_callback")
        return client

    with (
        patch.object(ble_scale, "establish_connection", fake_connect),
        patch.object(ble_scale.bluetooth, "async_ble_device_from_address", return_value=BLEDevice(MAC, "R-MSC06", None)),
    ):
        callbacks[0](service_info(advert), None)
        scale = entry.runtime_data
        for _ in range(100):
            if client.ready():
                break
            await asyncio.sleep(0.01)
        await script(client)
        for _ in range(300):
            if scale._task is None:
                break
            await asyncio.sleep(0.01)
    await hass.async_block_till_done()
    return client


async def test_setup_creates_entities(hass: HomeAssistant, adv_callbacks) -> None:
    await setup_scale(hass)
    assert len(adv_callbacks) == 1
    assert hass.states.get(f"{DEVICE}_last_weight") is not None
    assert hass.states.get(f"{DEVICE}_connection_status").state == "idle"
    assert hass.states.get(f"{PERSON}_weight") is not None
    assert hass.states.get("sensor.renpho_scale_sam_body_fat") is not None


async def test_weigh_in_final_frame(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await setup_scale(hass)
    events = async_capture_events(hass, f"{DOMAIN}_measurement")

    async def script(client):
        for w in (40.0, 82.3, 82.4, 82.4, 82.4):
            client.push(proto.NOTIFY_UUID, m14(w))
        client.push(proto.BATTERY_UUID, b"\x57")  # other characteristics are captured, not parsed
        client.push(proto.NOTIFY_UUID, m14(82.4, 500, final=True))

    client = await run_session(hass, adv_callbacks, entry, script)
    assert client.writes == []  # unknown model in auto mode never writes
    assert not client.is_connected
    assert float(hass.states.get(f"{PERSON}_weight").state) == 82.4
    assert hass.states.get(f"{PERSON}_impedance") is None  # disabled by default
    assert next(iter(entry.runtime_data.profiles.values()))["resistance"] == 500
    bf = float(hass.states.get(f"{PERSON}_body_fat").state)
    assert 5 < bf < 40
    assert hass.states.get(f"{PERSON}_basal_metabolic_rate").state not in ("unknown", "unavailable")
    assert hass.states.get(f"{DEVICE}_last_person").state == "Alex"
    assert float(hass.states.get(f"{DEVICE}_battery").state) == 87
    assert hass.states.get("sensor.renpho_scale_sam_weight").state == "unknown"
    assert len(events) == 1 and events[0].data["profile"] == "Alex" and events[0].data["method"] == "final"

    diag = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(diag)
    assert "Alex" not in dumped and "Sam" not in dumped and "1990-05-17" not in dumped
    assert MAC not in dumped and "F4:12:FA:9E:1F:AA" not in dumped
    ble = diag["ble"]
    assert ble["address"] == "60:65:F4:XX:XX:XX" and ble["source"] == "F4:12:FA:XX:XX:XX"
    assert ble["scale"]["profile"] == "**REDACTED**"
    assert ble["advert"]["model_id"] == "0x0003" and ble["advert"]["prefix"] == "0x0004"
    assert ble["family"] == "x55aa"
    assert ble["sessions"] == 1
    assert {c["uuid"] for s in ble["gatt_table"] for c in s["characteristics"]} >= {proto.NOTIFY_UUID, proto.COMMAND_UUID}
    fw = next(c for s in ble["gatt_table"] for c in s["characteristics"] if c["uuid"] == proto.FIRMWARE_UUID)
    assert bytes.fromhex(fw["value"]) == b"A1.0.1"
    assert all(v == "ok" for v in ble["subscriptions"].values()) and len(ble["subscriptions"]) == 2
    assert any(r["char"] == "2a19" for r in ble["raw_log"])
    assert sum(1 for f in ble["frame_log"] if f["cmd"] == "0x14") == 6


async def test_duplicate_session_ignored(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await setup_scale(hass)
    events = async_capture_events(hass, f"{DOMAIN}_measurement")

    async def script(client):
        client.push(proto.NOTIFY_UUID, m14(82.4, 500, final=True))

    await run_session(hass, adv_callbacks, entry, script)
    entry.runtime_data._cooldown_until = ble_scale.dt_util.utcnow()
    await run_session(hass, adv_callbacks, entry, script)
    assert len(events) == 1


async def test_unassigned_and_reassign(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await setup_scale(hass)

    async def script(client):
        client.push(proto.NOTIFY_UUID, m14(73.0, 480, final=True))

    await run_session(hass, adv_callbacks, entry, script)
    assert hass.states.get(f"{DEVICE}_last_person").state == "unassigned"
    assert float(hass.states.get(f"{DEVICE}_last_weight").state) == 73.0
    assert hass.states.get(f"{PERSON}_weight").state == "unknown"

    alex_id = next(sid for sid, s in entry.subentries.items() if s.title == "Alex")
    await hass.services.async_call(
        DOMAIN, "reassign_last_measurement",
        {"config_entry_id": entry.entry_id, "profile_id": alex_id}, blocking=True,
    )
    await hass.async_block_till_done()
    assert float(hass.states.get(f"{PERSON}_weight").state) == 73.0
    assert hass.states.get(f"{DEVICE}_last_person").state == "Alex"
    # Alex's reference weight is now 73 kg, so a 73.5 kg reading matches him next time
    assert entry.runtime_data._match_profile(73.5)[0] == alex_id


async def test_plateau_with_guest_profile(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await setup_scale(hass, options={"allow_writes": "always", "capture_all_characteristics": False})

    async def script(client):
        for w in (61.8, 61.8, 61.8, 61.8):
            client.push(proto.NOTIFY_UUID, m14(w))
        # no final ever arrives; the session must time out and keep the plateau weight

    with patch.object(ble_scale, "SESSION_TIMEOUT", 0.5), patch.object(ble_scale, "PROFILE_GRACE", 0.01):
        client = await run_session(hass, adv_callbacks, entry, script)
    assert len(client.writes) == 1
    sent = client.writes[0]
    assert sent[:3] == b"\x55\xaa\x96" and sent[5] & 0x0F == proto.GUEST_SLOT and sent[5] >> 4 == 2  # female guest
    assert set(client.handlers) == {proto.NOTIFY_UUID}  # capture disabled: only 2A10 subscribed
    assert float(hass.states.get("sensor.renpho_scale_sam_weight").state) == 61.8
    assert hass.states.get("sensor.renpho_scale_sam_body_fat").state == "unknown"  # no impedance
    attrs = hass.states.get("sensor.renpho_scale_sam_last_measurement").attributes
    assert attrs["method"] == "plateau"


async def test_no_connectable_path(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await setup_scale(hass)
    with patch.object(ble_scale.bluetooth, "async_ble_device_from_address", return_value=None):
        adv_callbacks[0](service_info(), None)
        for _ in range(100):
            if entry.runtime_data._task is None:
                break
            await asyncio.sleep(0.01)
    await hass.async_block_till_done()
    assert hass.states.get(f"{DEVICE}_connection_status").state == "no_connectable_adapter"


async def test_measurement_survives_reload(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await setup_scale(hass)

    async def script(client):
        client.push(proto.NOTIFY_UUID, m14(82.0, 510, final=True))

    await run_session(hass, adv_callbacks, entry, script)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert float(hass.states.get(f"{PERSON}_weight").state) == 82.0


async def test_adding_person_reloads(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await setup_scale(hass, profiles=(ALEX,))
    assert hass.states.get("sensor.renpho_scale_kid_weight") is None
    result = await hass.config_entries.subentries.async_init((entry.entry_id, "profile"), context={"source": "user"})
    assert result["type"] == "form"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**ALEX, "name": "Kid", "height_cm": 120, "start_weight": 25.0}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert hass.states.get("sensor.renpho_scale_kid_weight") is not None


@pytest.mark.parametrize("mode", ["auto", "never", "always"])
async def test_options_flow(hass: HomeAssistant, adv_callbacks, mode) -> None:
    entry = await setup_scale(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"allow_writes": mode, "capture_all_characteristics": False}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["allow_writes"] == mode


async def test_edit_person_reloads(hass: HomeAssistant, adv_callbacks, caplog) -> None:
    entry = await setup_scale(hass, profiles=(ALEX,))
    before = entry.runtime_data
    alex_id = next(iter(entry.subentries))
    result = await entry.start_subentry_reconfigure_flow(hass, alex_id)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**ALEX, "height_cm": 182}
    )
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.runtime_data is not before
    assert entry.subentries[alex_id].data["height_cm"] == 182
    assert hass.states.get(f"{PERSON}_weight") is not None
    assert "update listener" not in caplog.text


async def test_reassign_restores_previous_reading(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await setup_scale(hass)
    events = async_capture_events(hass, f"{DOMAIN}_measurement")
    for weight in (82.4, 81.0):  # both within Alex's tolerance; the second was really Sam holding a bag
        async def script(client, w=weight):
            client.push(proto.NOTIFY_UUID, m14(w, 500, final=True))

        await run_session(hass, adv_callbacks, entry, script)
        entry.runtime_data._cooldown_until = ble_scale.dt_util.utcnow()
    assert float(hass.states.get(f"{PERSON}_weight").state) == 81.0

    sam_id = next(sid for sid, s in entry.subentries.items() if s.title == "Sam")
    await hass.services.async_call(
        DOMAIN, "reassign_last_measurement",
        {"config_entry_id": entry.entry_id, "profile_id": sam_id}, blocking=True,
    )
    await hass.async_block_till_done()
    assert float(hass.states.get(f"{PERSON}_weight").state) == 82.4
    assert float(hass.states.get("sensor.renpho_scale_sam_weight").state) == 81.0
    assert hass.states.get(f"{DEVICE}_last_person").state == "Sam"
    assert [e.data["reassigned"] for e in events] == [False, False, True]


async def test_status_sensor_is_enum(hass: HomeAssistant, adv_callbacks) -> None:
    await setup_scale(hass)
    state = hass.states.get(f"{DEVICE}_connection_status")
    assert state.attributes["device_class"] == "enum"
    assert "no_connectable_adapter" in state.attributes["options"]


async def test_duplicate_person_name_rejected(hass: HomeAssistant, adv_callbacks) -> None:
    entry = await setup_scale(hass, profiles=(ALEX,))
    result = await hass.config_entries.subentries.async_init((entry.entry_id, "profile"), context={"source": "user"})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {**ALEX, "name": " alex "})
    assert result["type"] == "form" and result["errors"] == {"name": "name_exists"}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {**ALEX, "name": "Kid"})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    kid = next(s for s in entry.subentries.values() if s.title == "Kid")
    assert kid.unique_id == "kid"


async def test_remove_person_cleans_up(hass: HomeAssistant, adv_callbacks, hass_storage) -> None:
    entry = await setup_scale(hass)

    async def script(client):
        client.push(proto.NOTIFY_UUID, m14(82.4, 500, final=True))

    await run_session(hass, adv_callbacks, entry, script)
    alex_id = next(sid for sid, s in entry.subentries.items() if s.title == "Alex")
    assert alex_id in entry.runtime_data.profiles
    assert hass.config_entries.async_remove_subentry(entry, alex_id)
    await hass.async_block_till_done()
    assert hass.states.get(f"{PERSON}_weight") is None
    assert alex_id not in entry.runtime_data.profiles  # pruned on the reload
    assert hass.states.get("sensor.renpho_scale_sam_weight") is not None


async def test_remove_entry_deletes_store(hass: HomeAssistant, adv_callbacks, hass_storage) -> None:
    entry = await setup_scale(hass)

    async def script(client):
        client.push(proto.NOTIFY_UUID, m14(82.4, 500, final=True))

    await run_session(hass, adv_callbacks, entry, script)
    assert await hass.config_entries.async_unload(entry.entry_id)
    key = f"{DOMAIN}.{entry.entry_id}"
    assert key in hass_storage
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_system_health(hass: HomeAssistant, adv_callbacks) -> None:
    await setup_scale(hass)
    assert await async_setup_component(hass, "system_health", {})
    info = await get_system_health_info(hass, DOMAIN)
    assert info == {"cloud_accounts": 0, "bluetooth_scales": 1}
