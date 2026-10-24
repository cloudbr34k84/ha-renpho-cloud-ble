"""LeFu ``55 AA`` GATT wire protocols (Bluetooth company ID 0x1A10).

Every frame is ``55 AA | cmd | len (u16 BE) | payload | checksum`` with
checksum = sum of the preceding bytes & 0xFF. Two command sets share it:

* **0x55aa family** (advert prefix ``00 04``: ES-CS20M, R-A01x, ES-26BB-B).
  Ported from renpho-escs20m 0.8.0 ``x55aa/protocol.py`` (MIT, (c) Ron); only
  the decode side and the documented-safe guest-profile write are kept.
* **MorphoScan family** (advert prefix ``00 12``: R-MSC04, R-MSC06). Our own
  implementation of the protocol documented, from real captures, in
  KristianP26/ble-scale-sync issues #117 and #434 (that project is GPL-3.0;
  no code is taken from it, only the wire facts).

Advertisement (manufacturer data for 0x1A10)::

    [0:2]  family prefix  00 04 (0x55aa family) / 00 12 (MorphoScan family)
    [2:4]  model id u16 BE (0x0003 basic, 0x0030/31 extended, 0x0035 R-MSC04,
           0x0041 R-MSC06)
    [4:10] MAC, forward byte order
    [10:]  trailing bytes (unattributed)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

MANUFACTURER_ID = 0x1A10

NOTIFY_UUID = "00002a10-0000-1000-8000-00805f9b34fb"
COMMAND_UUID = "00002a11-0000-1000-8000-00805f9b34fb"
INDICATE_UUID = "00002a12-0000-1000-8000-00805f9b34fb"  # MorphoScan family only
# Firmware-update services (Nordic legacy DFU, Nordic secure DFU). Never
# subscribed or written, even in capture mode.
DFU_SERVICE_UUIDS = frozenset({
    "00001530-1212-efde-1523-785feabcd123",
    "0000fe59-0000-1000-8000-00805f9b34fb",
})
BATTERY_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
FIRMWARE_UUID = "00002a26-0000-1000-8000-00805f9b34fb"

EXTENDED_MODEL_IDS = frozenset({0x0030, 0x0031})
MODEL_LABELS = {
    0x0003: "basic (ES-CS20MB1 / R-A012 / R-A016 / ES-26BB-B)",
    0x0030: "extended (ESCS20MB2 sibling)",
    0x0031: "extended (ES-CS20M ESCS20MB2 / Elis 1)",
    0x0035: "MorphoScan (R-MSC04)",
    0x0041: "MorphoScan (R-MSC06)",
}
_MODEL_NAMES = {0x0035: "R-MSC04", 0x0041: "R-MSC06"}

FAMILY_X55AA = "x55aa"
FAMILY_MSC = "morphoscan"
PREFIX_MSC = 0x0012


def model_name(model_id: int | None) -> str:
    return _MODEL_NAMES.get(model_id, "Scale")


def family(advert: Advert | None) -> str:
    return FAMILY_MSC if advert is not None and advert.prefix == PREFIX_MSC else FAMILY_X55AA


CMD_STATUS = 0x11
CMD_MEASUREMENT = 0x14
CMD_STORED = 0x15
CMD_EXT_MEASUREMENT = 0x18
CMD_EXT_STORED = 0x19
CMD_USER_PROFILE = 0x96

_MAGIC = b"\x55\xaa"
_HEADER = 5
_MAX_PAYLOAD = 64
_FRAGMENT_MARKERS = frozenset({0xAD, 0xAE, 0xAF})
GUEST_SLOT = 9


@dataclass(frozen=True)
class Advert:
    prefix: int
    model_id: int
    trailer: bytes


def parse_advert(payload: bytes, address: str) -> Advert | None:
    """Validate a 0x1A10 payload (MAC echo must match) and decode it."""
    if len(payload) < 10:
        return None
    try:
        mac = bytes(int(o, 16) for o in address.split(":"))
    except ValueError:
        mac = b""
    if len(mac) == 6 and payload[4:10] != mac:
        return None
    return Advert(
        prefix=int.from_bytes(payload[0:2], "big"),
        model_id=int.from_bytes(payload[2:4], "big"),
        trailer=bytes(payload[10:]),
    )


@dataclass(frozen=True)
class Frame:
    cmd: int
    payload: bytes


@dataclass(frozen=True)
class Reading:
    weight_kg: float
    resistance: int
    final: bool
    zero_current: bool
    scale_bmi: float | None = None
    scale_body_fat: float | None = None


def _checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def _frame(cmd: int, payload: bytes) -> bytes:
    body = _MAGIC + bytes([cmd]) + len(payload).to_bytes(2, "big") + payload
    return body + bytes([_checksum(body)])


def strip_fragment(data: bytes) -> bytes:
    if len(data) >= 3 and data[0] in _FRAGMENT_MARKERS:
        return data[3:]
    return data


def iter_frames(buffer: bytearray) -> list[Frame]:
    """Consume every complete, checksum-valid frame from ``buffer``."""
    frames: list[Frame] = []
    while True:
        start = buffer.find(_MAGIC)
        if start == -1:
            del buffer[: max(0, len(buffer) - 1)]
            return frames
        del buffer[:start]
        if len(buffer) < _HEADER:
            return frames
        length = int.from_bytes(buffer[3:5], "big")
        if length > _MAX_PAYLOAD:
            del buffer[:2]
            continue
        total = _HEADER + length + 1
        if len(buffer) < total:
            return frames
        raw = bytes(buffer[:total])
        if _checksum(raw[:-1]) != raw[-1]:
            del buffer[:2]
            continue
        del buffer[:total]
        frames.append(Frame(raw[2], raw[_HEADER:-1]))


def parse_reading(frame: Frame) -> Reading | None:
    """Decode a live 0x14 (basic) or 0x18 (extended final) measurement."""
    p = frame.payload
    if frame.cmd == CMD_MEASUREMENT and len(p) >= 7:
        return Reading(
            weight_kg=round(int.from_bytes(p[1:5], "big") / 100, 2),
            resistance=int.from_bytes(p[5:7], "big"),
            final=(p[0] & 0x0F) == 0x01,
            zero_current=bool(p[0] & 0x10),
        )
    if frame.cmd == CMD_EXT_MEASUREMENT and len(p) >= 13:
        return Reading(
            weight_kg=round(int.from_bytes(p[3:7], "big") / 100, 2),
            resistance=int.from_bytes(p[7:9], "big"),
            final=(p[0] & 0x0F) == 0x01,
            zero_current=bool(p[0] & 0x10),
            scale_bmi=round(int.from_bytes(p[9:11], "big") / 10, 1) or None,
            scale_body_fat=round(int.from_bytes(p[11:13], "big") / 10, 1) or None,
        )
    return None


def build_guest_profile(
    *,
    female: bool,
    birthday: date,
    height_cm: float,
    last_weight_kg: float,
    algorithm: int,
    athlete: bool,
    measure_impedance: bool,
) -> bytes:
    """``0x96`` guest form (slot 9, mask 0xFF) — never persisted by the scale."""
    method = {0x03: 1, 0x04: 2}[algorithm]
    flags = (0x68 if athlete else 0xA8) | method
    payload = (
        bytes([((2 if female else 1) << 4) | GUEST_SLOT])
        + birthday.year.to_bytes(2, "big")
        + bytes([birthday.month, birthday.day])
        + int(round(height_cm * 10)).to_bytes(2, "big")
        + int(round(last_weight_kg * 100)).to_bytes(4, "big")
        + bytes([flags, 0xFF, 0x05 if measure_impedance else 0x06])
    )
    return _frame(CMD_USER_PROFILE, payload)


# --------------------------------------------------------------------------
# MorphoScan family (R-MSC04 / R-MSC06)
#
# 0x2A10 notify    0x21 live weight
# 0x2A12 indicate  0x20 status, 0x22/0x23 write replies, 0x24 final weight,
#                  0x25 composition record, 0x26 stored history record
#                  (0x25/0x26 arrive in AD/AE/AF fragments)
# 0x2A11 write     0xB2 guest profile, 0xB3 clock, 0xB0 status ack
#
# Without the B2 + B3 writes right after connecting, the scale drops the
# link after ~5.5 s. Every 0x20 status is acked with B0, like the app.
# --------------------------------------------------------------------------

MSC_STATUS = 0x20
MSC_LIVE = 0x21
MSC_PROFILE_REPLY = 0x22
MSC_CLOCK_REPLY = 0x23
MSC_FINAL = 0x24
MSC_RECORD = 0x25
MSC_HISTORY = 0x26
MSC_ACK = 0xB0
MSC_PROFILE = 0xB2
MSC_CLOCK = 0xB3

MSC_STATE_MEASURING = 0x01
MSC_STATE_ENDING = 0x04
MSC_STATE_IDLE = 0x05
MSC_STATE_LOCKED = 0x09
MSC_STATE_COMPLETE = 0x11

# b2 [11] — undecoded (probably sex/age). 0xA9 is what working third-party
# clients send; the scale's own body fat is only right with the byte the
# Renpho app writes for that person.
MSC_DEFAULT_PROFILE_BYTE = 0xA9
_MSC_RECORD_LEN = 36
_MSC_MAX_ZONE_MIN = 840


@dataclass(frozen=True)
class MscStatus:
    seq: int
    state: int
    stored: int | None


@dataclass(frozen=True)
class MscRecord:
    """A decoded 0x25 record — the scale's own analysis of this weigh-in."""

    weight_kg: float
    body_fat: float
    bmi: float
    skeletal_muscle: float
    visceral_fat: int
    impedance_20khz: tuple[float, ...]  # trunk, left arm, right arm, left leg, right leg
    impedance_100khz: tuple[float, ...]


def build_msc_profile(seq: int, height_cm: float | None, last_weight_kg: float | None, profile_byte: int) -> bytes:
    """``B2`` in the guest form: slot 09, height ×10, last weight ×100, byte, FF 02."""
    height = int(round((height_cm or 0) * 10))
    if not 500 <= height <= 2500:
        height = 1700
    weight = int(round((last_weight_kg or 0) * 100))
    if not 50 <= weight <= 30000:
        weight = 7000
    payload = bytes([seq & 0xFF, GUEST_SLOT]) + height.to_bytes(2, "big") + weight.to_bytes(2, "big")
    return _frame(MSC_PROFILE, payload + bytes([profile_byte & 0xFF, 0xFF, 0x02]))


def build_msc_clock(seq: int, unix_seconds: int, utc_offset_seconds: int) -> bytes:
    """``B3``: 07 01 01, Unix seconds u32 BE, zone in minutes east of UTC (0 if west), 00."""
    zone = int(utc_offset_seconds // 60)
    if not 0 <= zone <= _MSC_MAX_ZONE_MIN:
        zone = 0
    payload = (
        bytes([seq & 0xFF, 0x07, 0x01, 0x01])
        + (int(unix_seconds) & 0xFFFFFFFF).to_bytes(4, "big")
        + zone.to_bytes(2, "big")
        + b"\x00"
    )
    return _frame(MSC_CLOCK, payload)


def build_msc_ack(status_seq: int) -> bytes:
    return _frame(MSC_ACK, bytes([status_seq & 0xFF, 0x01]))


def parse_msc_weight(frame: Frame) -> float | None:
    """0x21 live / 0x24 final: weight is the last two payload bytes, 0.01 kg."""
    if frame.cmd not in (MSC_LIVE, MSC_FINAL) or len(frame.payload) < 2:
        return None
    kg = int.from_bytes(frame.payload[-2:], "big") / 100
    return kg if 0.5 <= kg <= 300 else None


def parse_msc_status(frame: Frame) -> MscStatus | None:
    p = frame.payload
    if frame.cmd != MSC_STATUS or len(p) < 2:
        return None
    return MscStatus(seq=p[0], state=p[1], stored=p[3] if len(p) >= 5 else None)


def parse_msc_record(frame: Frame) -> MscRecord | None:
    p = frame.payload
    if frame.cmd != MSC_RECORD or len(p) != _MSC_RECORD_LEN:
        return None

    def u16(i: int) -> int:
        return int.from_bytes(p[i : i + 2], "big")

    return MscRecord(
        weight_kg=round(int.from_bytes(p[2:6], "big") / 100, 2),
        impedance_20khz=tuple(u16(7 + 2 * i) / 10 for i in range(5)),
        impedance_100khz=tuple(u16(17 + 2 * i) / 10 for i in range(5)),
        body_fat=u16(28) / 10,
        bmi=u16(30) / 10,
        skeletal_muscle=u16(32) / 10,
        visceral_fat=u16(34),
    )
