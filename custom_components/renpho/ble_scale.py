"""Local BLE session manager for LeFu-based Renpho scales (company ID 0x1A10).

Flow per weigh-in: the scale wakes and advertises → we connect through any
connectable adapter or proxy → subscribe → decode → assign the reading to the
closest person by weight → body composition → disconnect.

Two protocol families (see ble_protocol):

* 0x55aa family: weight + impedance on 2A10; body composition is derived
  here from impedance and the person's profile.
* MorphoScan family (R-MSC04 / R-MSC06): a guest profile + clock handshake on
  connect, live weight on 2A10, final weight and the scale's own 0x25 analysis
  on 2A12. The scale's body fat is used only when it provably computed it for
  the matched person (their profile byte, answered before completion, BMI
  consistent with their height).

Every notification on every characteristic is also kept in a ring buffer for
diagnostics, so unknown models can be worked out from real data.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, datetime, timedelta
import logging
from typing import Any

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import ble_protocol as proto
from .body_metrics import FEMALE, MALE, age_on, calculate_body_fat, derive_metrics
from .const import (
    ALGO_ALT,
    CONF_ADDRESS,
    CONF_ALGORITHM,
    CONF_ALLOW_WRITES,
    CONF_ATHLETE,
    CONF_BIRTHDAY,
    CONF_CAPTURE,
    CONF_HEIGHT_CM,
    CONF_NAME,
    CONF_PROFILE_BYTE,
    CONF_SEX,
    CONF_START_WEIGHT,
    CONF_TOLERANCE,
    DEFAULT_TOLERANCE_KG,
    DOMAIN,
    SEX_FEMALE,
    SIGNAL_BLE_UPDATE,
    SUBENTRY_PROFILE,
)

_LOGGER = logging.getLogger(__name__)

SESSION_TIMEOUT = 75.0  # s — scale display stays on ~30-60 s
PROFILE_GRACE = 3.0  # s after a weight plateau before writing a guest profile
PLATEAU_FRAMES = 3
PLATEAU_DELTA = 0.05
MIN_WEIGHT = 10.0
DUPLICATE_WINDOW = timedelta(minutes=2)
ADV_NOTIFY_INTERVAL = timedelta(seconds=5)
STORE_VERSION = 1
MSC_RECORD_HOLD = 30.0  # s after the final weight to wait for the 0x25 record
MSC_RECORD_WEIGHT_TOL = 0.5  # kg between the final weight and the record's
MSC_BMI_TOLERANCE = 0.1 + 1e-9  # record BMI vs weight / sent height², 0.1 rounding
WRITES_AUTO = "auto"
WRITES_NEVER = "never"
WRITES_ALWAYS = "always"


class RenphoBleScale:
    """Owns the advertisement subscription and GATT sessions for one scale."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS].upper()
        self._store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.scale: dict[str, Any] = {}
        self.profiles: dict[str, dict[str, Any]] = {}
        self.status = "idle"
        self.last_error: str | None = None
        self.advert: proto.Advert | None = None
        self.rssi: int | None = None
        self.last_seen: datetime | None = None
        self.source: str | None = None
        self.gatt_table: list[dict[str, Any]] = []
        self.subscriptions: dict[str, str] = {}
        self._warned_no_notify = False
        self.raw_log: deque[dict[str, Any]] = deque(maxlen=300)
        self.frame_log: deque[dict[str, Any]] = deque(maxlen=100)
        self.unknown_cmds: set[int] = set()
        self.writes: deque[dict[str, Any]] = deque(maxlen=50)
        self.history_records = 0
        self.sessions = 0
        self._task: asyncio.Task | None = None
        self._cooldown_until: datetime = dt_util.utcnow()
        self._last_adv_notify: datetime = datetime.min.replace(tzinfo=dt_util.UTC)
        self._unsub: list = []
        # per-session state
        self._buffers: dict[str, bytearray] = {}
        self._done = asyncio.Event()
        self._final: proto.Reading | None = None
        self._plateau: float | None = None
        self._last_settling: float | None = None
        self._run = 0
        self._profile_task: asyncio.Task | None = None
        self._profile_sent_for: str | None = None
        self._cmd_char: BleakGATTCharacteristic | None = None
        self._client: BleakClient | None = None
        self._write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        # MorphoScan family
        self._msc_seq = 0
        self._msc_state: int | None = None
        self._msc_stored_at_start: int | None = None
        self._msc_complete_seen = False
        self._msc_profile: dict[str, Any] | None = None
        self._msc_reprofiled = False
        self._msc_record: proto.MscRecord | None = None
        self._hold_task: asyncio.Task | None = None

    # ---------------------------------------------------------------- setup
    @property
    def options(self) -> dict[str, Any]:
        return self.entry.options

    @property
    def model_id(self) -> int | None:
        return self.advert.model_id if self.advert else None

    @property
    def family(self) -> str:
        return proto.family(self.advert)

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        self.scale = stored.get("scale", {})
        # People removed since the last run take their readings with them
        self.profiles = {
            sid: data for sid, data in stored.get("profiles", {}).items() if sid in self.entry.subentries
        }
        self._unsub.append(
            bluetooth.async_register_callback(
                self.hass,
                self._on_advertisement,
                bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=False),
                bluetooth.BluetoothScanningMode.ACTIVE,
            )
        )

    async def async_stop(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if self._task:
            self._task.cancel()
        if self._client and self._client.is_connected:
            await self._client.disconnect()
        await self._store.async_save(self._snapshot())

    def _snapshot(self) -> dict[str, Any]:
        return {"scale": self.scale, "profiles": self.profiles}

    def _notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_BLE_UPDATE.format(self.entry.entry_id))

    # -------------------------------------------------------- advertisements
    @callback
    def _on_advertisement(
        self, info: bluetooth.BluetoothServiceInfoBleak, change: bluetooth.BluetoothChange
    ) -> None:
        payload = info.manufacturer_data.get(proto.MANUFACTURER_ID)
        if payload is not None:
            advert = proto.parse_advert(bytes(payload), self.address)
            if advert is not None:
                if self.advert is None or advert.model_id != self.advert.model_id:
                    _LOGGER.debug(
                        "Renpho %s advertises prefix 0x%04x model 0x%04x (%s)",
                        self.address,
                        advert.prefix,
                        advert.model_id,
                        proto.MODEL_LABELS.get(advert.model_id, "unknown"),
                    )
                self.advert = advert
        now = dt_util.utcnow()
        self.rssi = info.rssi
        self.last_seen = now
        self.source = info.source
        if self._task is None and now >= self._cooldown_until:
            self._task = self.entry.async_create_background_task(
                self.hass, self._session(), f"renpho_ble_session_{self.address}"
            )
        elif now - self._last_adv_notify < ADV_NOTIFY_INTERVAL:
            return
        self._last_adv_notify = now
        self._notify()

    # -------------------------------------------------------------- session
    def _reset_session(self) -> None:
        self._buffers = {}
        self._done = asyncio.Event()
        self._final = None
        self._plateau = None
        self._last_settling = None
        self._run = 0
        self._profile_sent_for = None
        self._cmd_char = None
        self._msc_seq = 0
        self._msc_state = None
        self._msc_stored_at_start = None
        self._msc_complete_seen = False
        self._msc_profile = None
        self._msc_reprofiled = False
        self._msc_record = None
        self._hold_task = None

    async def _session(self) -> None:
        delivered = False
        try:
            device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
            if device is None:
                self.status = "no_connectable_adapter"
                self.last_error = "No connectable adapter/proxy can reach the scale"
                return
            self._reset_session()
            self.status = "connecting"
            self._notify()
            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                f"Renpho {self.address}",
                disconnected_callback=lambda _c: self._done.set(),
                max_attempts=3,
            )
            self._client = client
            self.sessions += 1
            self.status = "connected"
            self.last_error = None
            self._notify()
            try:
                await self._prepare(client)
                try:
                    await asyncio.wait_for(self._done.wait(), SESSION_TIMEOUT)
                except TimeoutError:
                    _LOGGER.debug("Renpho %s session timed out", self.address)
            finally:
                for task in (self._profile_task, self._hold_task, *self._tasks):
                    if task:
                        task.cancel()
                self._tasks.clear()
                if client.is_connected:
                    await client.disconnect()
                self._client = None
            delivered = self._finish_session()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - BLE stacks raise anything
            self.status = "error"
            self.last_error = f"{type(err).__name__}: {err}"
            _LOGGER.debug("Renpho %s session failed", self.address, exc_info=True)
        finally:
            if self.status in ("connecting", "connected"):
                self.status = "idle"
            self._cooldown_until = dt_util.utcnow() + timedelta(seconds=45 if delivered else 8)
            self._task = None
            self._notify()

    async def _prepare(self, client: BleakClient) -> None:
        # Subscribe first: settling frames start as soon as the scale wakes
        capture = self.options.get(CONF_CAPTURE, True)
        msc = self.family == proto.FAMILY_MSC
        main_uuids = {proto.NOTIFY_UUID, proto.INDICATE_UUID} if msc else {proto.NOTIFY_UUID}
        notify = client.services.get_characteristic(proto.NOTIFY_UUID)
        self._cmd_char = client.services.get_characteristic(proto.COMMAND_UUID)
        self.subscriptions = {}
        for svc in client.services:
            if svc.uuid in proto.DFU_SERVICE_UUIDS:
                continue
            for ch in svc.characteristics:
                if not {"notify", "indicate"} & set(ch.properties):
                    continue
                is_main = ch.uuid in main_uuids
                if not (is_main or capture):
                    continue
                try:
                    await client.start_notify(ch, self._make_handler(ch.uuid, is_main))
                    self.subscriptions[f"{ch.uuid}@{ch.handle}"] = "ok"
                except Exception as err:  # noqa: BLE001
                    self.subscriptions[f"{ch.uuid}@{ch.handle}"] = f"{type(err).__name__}: {err}"
                    _LOGGER.debug("Renpho %s: notify on %s failed: %s", self.address, ch.uuid, err)

        if msc and self._writes_enabled() and self._cmd_char is not None:
            # Without these two the scale hangs up ~5.5 s into the connection
            await self._msc_send_profile(self._initial_msc_person())
            now = dt_util.now()
            offset = now.utcoffset()
            await self._write(
                proto.build_msc_clock(
                    self._next_seq(), int(now.timestamp()), int(offset.total_seconds()) if offset else 0
                ),
                "b3 clock",
            )

        for uuid, key in ((proto.BATTERY_UUID, "battery"), (proto.FIRMWARE_UUID, "firmware")):
            char = client.services.get_characteristic(uuid)
            if char is None:
                continue
            try:
                value = await client.read_gatt_char(char)
            except Exception:  # noqa: BLE001
                continue
            self.scale[key] = value[0] if key == "battery" and value else value.decode(errors="replace").strip("\x00 ")

        # GATT table for diagnostics; readable values are fetched once per HA run
        read_values = capture and not self.gatt_table
        known = {
            c["handle"]: c for svc in self.gatt_table for c in svc["characteristics"]
        }
        table: list[dict[str, Any]] = []
        for svc in client.services:
            chars = []
            for ch in svc.characteristics:
                entry: dict[str, Any] = {"uuid": ch.uuid, "handle": ch.handle, "properties": list(ch.properties)}
                for k in ("value", "value_error"):
                    if k in known.get(ch.handle, {}):
                        entry[k] = known[ch.handle][k]
                if read_values and "read" in ch.properties:
                    try:
                        entry["value"] = bytes(await client.read_gatt_char(ch)).hex()
                    except Exception as err:  # noqa: BLE001
                        entry["value_error"] = type(err).__name__
                chars.append(entry)
            table.append({"service": svc.uuid, "characteristics": chars})
        self.gatt_table = table
        if notify is None and not self._warned_no_notify:
            self._warned_no_notify = True
            _LOGGER.warning(
                "Renpho %s has no 0x2A10 characteristic, so it may not be a 0x55aa scale. "
                "With 'Capture all BLE characteristics' on, download diagnostics after a "
                "weigh-in and share them",
                self.address,
            )

    def _make_handler(self, uuid: str, is_main: bool):
        def handler(_char: BleakGATTCharacteristic, data: bytearray) -> None:
            self.raw_log.append({"t": dt_util.utcnow().isoformat(), "char": uuid[4:8], "hex": data.hex()})
            _LOGGER.debug("Renpho %s %s <- %s", self.address, uuid[4:8], data.hex())
            if is_main:
                buffer = self._buffers.setdefault(uuid, bytearray())
                buffer.extend(proto.strip_fragment(bytes(data)))
                for frame in proto.iter_frames(buffer):
                    if self.family == proto.FAMILY_MSC:
                        self._on_msc_frame(frame)
                    else:
                        self._on_frame(frame)

        return handler

    @callback
    def _on_frame(self, frame: proto.Frame) -> None:
        self.frame_log.append({"t": dt_util.utcnow().isoformat(), "cmd": f"0x{frame.cmd:02x}", "payload": frame.payload.hex()})
        reading = proto.parse_reading(frame)
        if reading is None:
            if frame.cmd not in (proto.CMD_STATUS, 0x10, 0x12, 0x16, 0x17, proto.CMD_STORED, proto.CMD_EXT_STORED) and frame.cmd not in self.unknown_cmds:
                self.unknown_cmds.add(frame.cmd)
                _LOGGER.info("Renpho %s: unhandled frame 0x%02x %s", self.address, frame.cmd, frame.payload.hex())
            return
        if reading.final:
            if reading.weight_kg >= MIN_WEIGHT and self._final is None:
                self._final = reading
                self._done.set()
            return
        if self._track_settling(reading.weight_kg) and self._writes_enabled() and self._cmd_char is not None:
            self._profile_task = self.hass.async_create_task(self._send_profile_after_grace())

    def _track_settling(self, weight: float) -> bool:
        """Follow the settling weight; True the first time it holds steady."""
        if weight < MIN_WEIGHT:
            self._run = 0
            return False
        if self._last_settling is not None and abs(weight - self._last_settling) <= PLATEAU_DELTA:
            self._run += 1
        else:
            self._run = 1
        self._last_settling = weight
        self.scale["live_weight"] = weight
        self._notify()
        if self._run >= PLATEAU_FRAMES and self._plateau is None:
            self._plateau = weight
            return True
        return False

    @callback
    def _on_msc_frame(self, frame: proto.Frame) -> None:
        self.frame_log.append({"t": dt_util.utcnow().isoformat(), "cmd": f"0x{frame.cmd:02x}", "payload": frame.payload.hex()})
        cmd = frame.cmd
        if cmd == proto.MSC_STATUS:
            status = proto.parse_msc_status(frame)
            if status is None:
                return
            self._msc_state = status.state
            if status.stored is not None and self._msc_stored_at_start is None:
                self._msc_stored_at_start = status.stored
            if status.state == proto.MSC_STATE_COMPLETE:
                self._msc_complete_seen = True
            if self._writes_enabled() and len(frame.payload) == 5:
                self._queue_write(proto.build_msc_ack(status.seq), "b0 ack")
            if status.state == proto.MSC_STATE_ENDING:
                self._done.set()
        elif cmd == proto.MSC_PROFILE_REPLY:
            sent = self._msc_profile
            if sent and frame.payload[:1] == bytes([sent["seq"]]):
                sent["answered"] = True
                sent["answered_before_complete"] = not self._msc_complete_seen
        elif cmd == proto.MSC_LIVE:
            weight = proto.parse_msc_weight(frame)
            if weight is not None and self._final is None and self._track_settling(weight):
                self._msc_reprofile(weight)
        elif cmd == proto.MSC_FINAL:
            weight = proto.parse_msc_weight(frame)
            if weight is None or weight < MIN_WEIGHT or self._final is not None:
                return
            self._final = proto.Reading(weight, 0, final=True, zero_current=False)
            self._msc_reprofile(weight)
            # Body composition is measured after the weight locks; the record
            # follows 10-20 s later, if the link survives
            self._hold_task = self.hass.async_create_task(self._release_after(MSC_RECORD_HOLD))
        elif cmd == proto.MSC_RECORD:
            rec = proto.parse_msc_record(frame)
            if rec is None:
                return
            if self._final is not None:
                ours = abs(rec.weight_kg - self._final.weight_kg) <= MSC_RECORD_WEIGHT_TOL
            else:
                # Connected after the weight locked: only a record that directly
                # follows "complete" on a scale with no stored backlog is this weigh-in
                ours = self._msc_state == proto.MSC_STATE_COMPLETE and self._msc_stored_at_start == 0
            if ours:
                self._msc_record = rec
                self._done.set()
        elif cmd == proto.MSC_HISTORY:
            self.history_records += 1
        elif cmd != proto.MSC_CLOCK_REPLY and cmd not in self.unknown_cmds:
            self.unknown_cmds.add(cmd)
            _LOGGER.info("Renpho %s: unhandled frame 0x%02x %s", self.address, cmd, frame.payload.hex())

    async def _release_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._done.set()

    def _next_seq(self) -> int:
        seq = self._msc_seq
        self._msc_seq = (seq + 1) & 0xFF
        return seq

    def _initial_msc_person(self) -> str | None:
        """Best guess at who is stepping on before any weight is known."""
        people = self._profile_subentries()
        if len(people) == 1:
            return next(iter(people))
        last = self.scale.get("profile_id")
        return last if last in people else None

    async def _msc_send_profile(self, sid: str | None) -> None:
        prof = self._profile_subentries().get(sid) if sid else None
        seq = self._next_seq()
        height = float(prof[CONF_HEIGHT_CM]) if prof else None
        last_weight = (self.profiles.get(sid, {}).get("weight") or prof.get(CONF_START_WEIGHT)) if prof else None
        own_byte = _profile_byte(prof)
        self._msc_profile = {
            "seq": seq,
            "sid": sid,
            "height_cm": height or 170.0,
            "own_byte": own_byte is not None,
            "answered": False,
            "answered_before_complete": False,
        }
        frame = proto.build_msc_profile(
            seq, height, last_weight, own_byte if own_byte is not None else proto.MSC_DEFAULT_PROFILE_BYTE
        )
        await self._write(frame, "b2 profile", log_hex=False)

    @callback
    def _msc_reprofile(self, weight: float) -> None:
        """Once the weight shows who it is, put that person's profile on the scale."""
        if self._msc_reprofiled or self._msc_complete_seen or not self._writes_enabled():
            return
        sid, _ = self._match_profile(weight)
        if sid is None or (self._msc_profile and self._msc_profile["sid"] == sid):
            return
        self._msc_reprofiled = True
        task = self.hass.async_create_task(self._msc_send_profile(sid))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _queue_write(self, frame: bytes, what: str) -> None:
        task = self.hass.async_create_task(self._write(frame, what))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write(self, frame: bytes, what: str, *, log_hex: bool = True) -> bool:
        """Write to 2A11 (writes are serialised so the scale sees them in order)."""
        client, char = self._client, self._cmd_char
        if client is None or char is None:
            return False
        async with self._write_lock:
            try:
                await client.write_gatt_char(char, frame, response=True)
                result = "ok"
            except Exception as err:  # noqa: BLE001
                result = f"{type(err).__name__}: {err}"
        entry = {"t": dt_util.utcnow().isoformat(), "what": what, "result": result}
        if log_hex:  # profile frames carry a height and weight, so they stay out
            entry["hex"] = frame.hex()
        self.writes.append(entry)
        _LOGGER.debug("Renpho %s: wrote %s (%s)", self.address, what, result)
        return result == "ok"

    def _writes_enabled(self) -> bool:
        mode = self.options.get(CONF_ALLOW_WRITES, WRITES_AUTO)
        if mode == WRITES_AUTO:
            # MorphoScan scales need the handshake; extended 0x55aa ones need
            # a guest profile before they release a final
            return self.family == proto.FAMILY_MSC or self.model_id in proto.EXTENDED_MODEL_IDS
        return mode == WRITES_ALWAYS

    async def _send_profile_after_grace(self) -> None:
        """Extended-flavor scales only release a final once a guest profile is on them."""
        await asyncio.sleep(PROFILE_GRACE)
        if self._final is not None or self._client is None or self._plateau is None:
            return
        sid, profile = self._match_profile(self._plateau)
        if profile:
            frame = proto.build_guest_profile(
                female=profile[CONF_SEX] == SEX_FEMALE,
                birthday=date.fromisoformat(profile[CONF_BIRTHDAY]),
                height_cm=float(profile[CONF_HEIGHT_CM]),
                last_weight_kg=self._plateau,
                algorithm=0x03 if profile.get(CONF_ALGORITHM) == ALGO_ALT else 0x04,
                athlete=bool(profile.get(CONF_ATHLETE)),
                measure_impedance=True,
            )
        else:
            frame = proto.build_guest_profile(
                female=False, birthday=date(1990, 1, 1), height_cm=170, last_weight_kg=self._plateau,
                algorithm=0x04, athlete=False, measure_impedance=False,
            )
        if await self._write(frame, "0x96 guest profile", log_hex=False):
            self._profile_sent_for = sid

    # ------------------------------------------------------------ measuring
    def _profile_subentries(self) -> dict[str, dict[str, Any]]:
        return {
            sid: dict(sub.data)
            for sid, sub in self.entry.subentries.items()
            if sub.subentry_type == SUBENTRY_PROFILE
        }

    def _match_profile(self, weight: float) -> tuple[str | None, dict[str, Any] | None]:
        best: tuple[float, str, dict] | None = None
        for sid, prof in self._profile_subentries().items():
            ref = self.profiles.get(sid, {}).get("weight") or prof.get(CONF_START_WEIGHT)
            if ref is None:
                continue
            delta = abs(float(ref) - weight)
            if delta <= float(prof.get(CONF_TOLERANCE, DEFAULT_TOLERANCE_KG)) and (best is None or delta < best[0]):
                best = (delta, sid, prof)
        return (best[1], best[2]) if best else (None, None)

    def _finish_session(self) -> bool:
        reading = self._final
        method = "final"
        record = self._msc_record
        if reading is None and record is not None:
            reading = proto.Reading(record.weight_kg, 0, final=True, zero_current=False)
            method = "record"
        if reading is None and self._plateau is not None:
            reading = proto.Reading(self._plateau, 0, final=False, zero_current=False)
            method = "plateau"
        self.scale.pop("live_weight", None)
        if reading is None:
            return False
        last = self.scale.get("measured_at")
        if (
            last
            and abs(self.scale.get("weight", 0) - reading.weight_kg) < 0.05
            and dt_util.utcnow() - dt_util.parse_datetime(last) < DUPLICATE_WINDOW
        ):
            return True
        self.record_measurement(reading.weight_kg, reading.resistance, method, reading, msc_record=record)
        return True

    def record_measurement(
        self,
        weight: float,
        resistance: int,
        method: str,
        reading: proto.Reading | None = None,
        force_profile: str | None = None,
        msc_record: proto.MscRecord | None = None,
    ) -> None:
        now = dt_util.utcnow().isoformat()
        if force_profile is not None:
            sid, prof = force_profile, self._profile_subentries().get(force_profile)
        else:
            sid, prof = self._match_profile(weight)
        self.scale.update(
            weight=weight,
            resistance=resistance or None,
            measured_at=now,
            method=method,
            profile=prof[CONF_NAME] if prof else None,
            profile_id=sid,
        )
        if prof and sid:
            sex = FEMALE if prof[CONF_SEX] == SEX_FEMALE else MALE
            height_m = float(prof[CONF_HEIGHT_CM]) / 100
            age = age_on(date.fromisoformat(prof[CONF_BIRTHDAY]), dt_util.now().date())
            body_fat = None
            trusted = self._trusted_msc_composition(sid, msc_record)
            if trusted:
                body_fat = trusted["body_fat"]
            elif reading and reading.scale_body_fat and self._profile_sent_for == sid:
                body_fat = reading.scale_body_fat
            elif resistance:
                body_fat = calculate_body_fat(
                    weight, height_m, age, sex, resistance,
                    algorithm=0x03 if prof.get(CONF_ALGORITHM) == ALGO_ALT else 0x04,
                    athlete=bool(prof.get(CONF_ATHLETE)),
                )
            metrics = derive_metrics(weight, height_m, sex, body_fat)
            if trusted:
                metrics["skeletal_muscle"] = trusted["skeletal_muscle"]
                metrics["visceral_fat"] = trusted["visceral_fat"]
            prior = {k: v for k, v in self.profiles.get(sid, {}).items() if k != "previous"}
            self.profiles[sid] = {
                "weight": weight,
                "resistance": resistance or None,
                "measured_at": now,
                "method": method,
                **metrics,
                "previous": prior or None,
            }
        _LOGGER.debug(
            "Renpho %s: %.2f kg (%s, R=%s) -> %s", self.address, weight, method, resistance, prof[CONF_NAME] if prof else "unassigned"
        )
        self.hass.bus.async_fire(
            f"{DOMAIN}_measurement",
            {
                "address": self.address,
                "weight": weight,
                "resistance": resistance,
                "profile": prof[CONF_NAME] if prof else None,
                "profile_id": sid,
                "method": method,
                "reassigned": force_profile is not None,
                "scale_record": _record_dict(msc_record, trusted=bool(prof and sid and self._trusted_msc_composition(sid, msc_record))),
            },
        )
        self._store.async_delay_save(self._snapshot, 5)
        self._notify()

    def _trusted_msc_composition(self, sid: str | None, rec: proto.MscRecord | None) -> dict[str, Any] | None:
        """The scale's analysis, if it provably was computed for this person."""
        sent = self._msc_profile
        if rec is None or sent is None or sid is None or sent["sid"] != sid:
            return None
        if not (sent["own_byte"] and sent["answered_before_complete"] and rec.body_fat):
            return None
        height_m = sent["height_cm"] / 100
        if abs(rec.bmi - rec.weight_kg / (height_m * height_m)) > MSC_BMI_TOLERANCE:
            return None
        return {"body_fat": rec.body_fat, "skeletal_muscle": rec.skeletal_muscle, "visceral_fat": rec.visceral_fat}

    def reassign_last(self, subentry_id: str) -> None:
        """Move the last scale reading onto a different profile."""
        weight = self.scale.get("weight")
        if weight is None:
            return
        old = self.scale.get("profile_id")
        if old and old != subentry_id and self.profiles.get(old, {}).get("measured_at") == self.scale.get("measured_at"):
            # The reading wasn't theirs: fall back to their previous one
            if previous := self.profiles[old].get("previous"):
                self.profiles[old] = previous
            else:
                self.profiles.pop(old)
        self.record_measurement(weight, self.scale.get("resistance") or 0, self.scale.get("method", "manual"), force_profile=subentry_id)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "advert": {
                "prefix": f"0x{self.advert.prefix:04x}",
                "model_id": f"0x{self.advert.model_id:04x}",
                "label": proto.MODEL_LABELS.get(self.advert.model_id, "unknown"),
                "trailer": self.advert.trailer.hex(),
            } if self.advert else None,
            "rssi": self.rssi,
            "source": self.source,
            "status": self.status,
            "last_error": self.last_error,
            "sessions": self.sessions,
            "family": self.family,
            "writes_enabled": self._writes_enabled(),
            "writes": list(self.writes),
            "morphoscan": {
                "last_state": f"0x{self._msc_state:02x}" if self._msc_state is not None else None,
                "stored_at_start": self._msc_stored_at_start,
                "history_records_seen": self.history_records,
                "profile_sent": {k: v for k, v in self._msc_profile.items() if k != "height_cm"} if self._msc_profile else None,
                "record": _record_dict(self._msc_record),
            },
            "gatt_table": self.gatt_table,
            "subscriptions": self.subscriptions,
            "unknown_cmds": [f"0x{c:02x}" for c in sorted(self.unknown_cmds)],
            "frame_log": list(self.frame_log),
            "raw_log": list(self.raw_log),
            "scale": self.scale,
        }


def _profile_byte(prof: dict[str, Any] | None) -> int | None:
    raw = (prof or {}).get(CONF_PROFILE_BYTE)
    try:
        return int(str(raw).strip(), 16) if raw not in (None, "") else None
    except ValueError:
        return None


def _record_dict(rec: proto.MscRecord | None, **extra: Any) -> dict[str, Any] | None:
    if rec is None:
        return None
    return {
        "weight": rec.weight_kg,
        "body_fat": rec.body_fat,
        "bmi": rec.bmi,
        "skeletal_muscle": rec.skeletal_muscle,
        "visceral_fat": rec.visceral_fat,
        "impedance_20khz": list(rec.impedance_20khz),
        "impedance_100khz": list(rec.impedance_100khz),
        **extra,
    }
