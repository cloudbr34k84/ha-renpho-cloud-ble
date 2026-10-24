"""Sensors for Renpho cloud users and local BLE scale profiles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfLength,
    UnitOfMass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import ble_protocol as proto
from .ble_scale import RenphoBleScale
from .const import CONF_MODEL_ID, CONF_NAME, CONF_SOURCE, DOMAIN, SIGNAL_BLE_UPDATE, SOURCE_CLOUD, SUBENTRY_PROFILE
from .coordinator import RenphoCloudCoordinator, RenphoConfigEntry

# Read-only sensors fed by a coordinator / dispatcher — no update throttling needed
PARALLEL_UPDATES = 0

M = SensorStateClass.MEASUREMENT
KCAL = "kcal/d"
OHM = "Ω"


@dataclass(frozen=True, kw_only=True)
class RenphoSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any] = lambda d: None
    # Record fields with the app's healthy range / standard for this metric
    range_keys: tuple[str | None, str | None, str | None] | None = None
    # Created only once a person's record actually carries the metric
    optional: bool = False
    # Mirror into long-term statistics when importing the cloud history
    history: bool = True
    # Read from the person's account profile (goals) instead of the latest record
    from_profile: bool = False


def _key(name: str, zero_ok: bool = False) -> Callable[[dict[str, Any]], Any]:
    def fn(d: dict[str, Any]) -> Any:
        v = d.get(name)
        if isinstance(v, bool) or v in (None, ""):
            return None
        return v if zero_ok or v not in (0, 0.0) else None

    return fn


def _ts(name: str) -> Callable[[dict[str, Any]], datetime | None]:
    def fn(d: dict[str, Any]) -> datetime | None:
        v = d.get(name)
        if v in (None, "", 0):
            return None
        if isinstance(v, (int, float)):
            return dt_util.utc_from_timestamp(v / 1000 if v > 1e11 else v)
        return dt_util.parse_datetime(v)

    return fn


def _d(key, unit=None, dc=None, src=None, prec=1, zero_ok=False, translation_key=None, **kw) -> RenphoSensorDescription:
    return RenphoSensorDescription(
        key=key,
        translation_key=translation_key or key,
        native_unit_of_measurement=unit,
        device_class=dc,
        state_class=M,
        suggested_display_precision=prec,
        value_fn=_key(src or key, zero_ok),
        **kw,
    )


def _fat_mass(d: dict[str, Any]) -> float | None:
    weight, fat = d.get("weight"), d.get("bodyfat")
    if not isinstance(weight, (int, float)) or not isinstance(fat, (int, float)) or not fat:
        return None
    return round(weight * fat / 100, 2)


WEIGHT = SensorDeviceClass.WEIGHT
KG = UnitOfMass.KILOGRAMS

# Field names and units confirmed against a live Renpho Health account
# (R-MSC06, Oct 2026): ``bone`` and ``sinew`` are kg, ``muscle`` is the
# skeletal-muscle percentage.
CLOUD_SENSORS: tuple[RenphoSensorDescription, ...] = (
    _d("weight", KG, WEIGHT, src="weight", prec=2, range_keys=("weightMin", "weightMax", "weightStd")),
    _d("bmi", src="bmi"),
    _d("body_fat", PERCENTAGE, src="bodyfat", range_keys=("bfpMin", "bfpMax", "bfpStd")),
    _d("body_water", PERCENTAGE, src="water"),
    _d("muscle_percent", PERCENTAGE, src="muscle"),
    _d("bone_mass", KG, WEIGHT, src="bone", prec=2, range_keys=("boneMin", "boneMax", None)),
    _d("bmr", KCAL, src="bmr", prec=0, range_keys=("bmrMin", "bmrMax", "bmrStd")),
    _d("visceral_fat", src="visfat", prec=0),
    _d("subcutaneous_fat", PERCENTAGE, src="subfat"),
    _d("protein", PERCENTAGE, src="protein"),
    _d("body_age", "a", src="bodyage", prec=0),
    _d("lean_body_mass", KG, WEIGHT, src="sinew", prec=2, range_keys=("muscleMassMin", "muscleMassMax", None)),
    _d("fat_free_mass", KG, WEIGHT, src="fatFreeWeight", prec=2),
    _d("heart_rate", "bpm", src="heartRate", prec=0, optional=True),  # few scales measure it
    RenphoSensorDescription(
        key="last_measurement", translation_key="last_measurement",
        device_class=SensorDeviceClass.TIMESTAMP, value_fn=_ts("timeStamp"), history=False,
    ),
)

# Body-composition-analyser (8-electrode, MorphoScan) metrics. Segment ``…Pct``
# and ``…Muscle`` fields are percentages of the segment's standard mass
# (``…Std``), not of the segment: 2.36 kg fat on a 0.69 kg standard is 342 %.
_SEGMENTS = (("la", "left_arm"), ("ra", "right_arm"), ("t", "trunk"), ("ll", "left_leg"), ("rl", "right_leg"))
_IMPEDANCE = (("Body", "trunk"), ("HandL", "left_arm"), ("HandR", "right_arm"), ("FootL", "left_leg"), ("FootR", "right_leg"))
SQM = "kg/m²"

MORPHOSCAN_SENSORS: tuple[RenphoSensorDescription, ...] = (
    RenphoSensorDescription(
        key="body_fat_mass", translation_key="body_fat_mass", native_unit_of_measurement=KG,
        device_class=WEIGHT, state_class=M, suggested_display_precision=2, value_fn=_fat_mass,
        range_keys=("bfmMin", "bfmMax", "bfmStd"), optional=True,
    ),
    _d("skeletal_muscle_mass", KG, WEIGHT, src="smmMass", prec=2, range_keys=("smmMin", "smmMax", "smmStd"), optional=True),
    _d("skeletal_muscle_index", SQM, src="smi", optional=True),
    _d("body_score", src="bodyScore", prec=0, optional=True),
    RenphoSensorDescription(
        key="body_type", translation_key="body_type", value_fn=_key("bodyType"), optional=True, history=False,
    ),
    _d("waist_hip_ratio", src="whr", prec=2, optional=True),
    _d("muscle_mass_percent", PERCENTAGE, src="sinewRatio", optional=True),
    _d("obesity_degree", PERCENTAGE, src="obesityDegree", prec=0, optional=True),
    _d("target_weight", KG, WEIGHT, src="weightTarget", prec=2, optional=True),
    _d("weight_control", KG, WEIGHT, src="weightControl", prec=2, zero_ok=True, optional=True),
    _d("fat_control", KG, WEIGHT, src="bfmCtrl", prec=2, zero_ok=True, optional=True),
    _d("muscle_control", KG, WEIGHT, src="ffmCtrl", prec=2, zero_ok=True, optional=True),
    *(
        desc
        for prefix, seg in _SEGMENTS
        for desc in (
            _d(f"{seg}_fat_mass", KG, WEIGHT, src=f"{prefix}BodyFatMass", prec=2,
               range_keys=(None, None, f"{prefix}BodyFatStd"), optional=True),
            _d(f"{seg}_fat_vs_standard", PERCENTAGE, src=f"{prefix}BodyFatPct", prec=0, optional=True),
            _d(f"{seg}_muscle_mass", KG, WEIGHT, src=f"{prefix}MuscleMass", prec=2,
               range_keys=(None, None, f"{prefix}MuscleStd"), optional=True),
            _d(f"{seg}_muscle_vs_standard", PERCENTAGE, src=f"{prefix}Muscle", prec=0, optional=True),
        )
    ),
    *(
        _d(f"impedance_{freq}khz_{seg}", OHM, src=f"z{freq}{field}", optional=True,
           entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False, history=False)
        for freq in (20, 100)
        for field, seg in _IMPEDANCE
    ),
)

STATUS_OPTIONS = ["low", "normal", "high"]


def _num(d: dict[str, Any], key: str) -> float | None:
    v = d.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _status(value_fn: Callable[[dict[str, Any]], float | None], low_key: str, high_key: str) -> Callable[[dict[str, Any]], str | None]:
    """Where a metric sits against the healthy range Renpho sends with the record."""

    def fn(d: dict[str, Any]) -> str | None:
        value, low, high = value_fn(d), _num(d, low_key), _num(d, high_key)
        if value is None or low is None or high is None or not 0 < low <= high:
            return None
        return "low" if value < low else "high" if value > high else "normal"

    return fn


def _mass_of(pct_key: str) -> Callable[[dict[str, Any]], float | None]:
    """A percentage field as kg of body weight (water and protein ranges are in kg)."""

    def fn(d: dict[str, Any]) -> float | None:
        pct, weight = _num(d, pct_key), _num(d, "weight")
        return pct * weight / 100 if pct and weight else None

    return fn


def _status_desc(key: str, value_fn, low_key: str, high_key: str) -> RenphoSensorDescription:
    return RenphoSensorDescription(
        key=key, translation_key=key, device_class=SensorDeviceClass.ENUM, options=STATUS_OPTIONS,
        value_fn=_status(value_fn, low_key, high_key), optional=True, history=False,
    )


# Low / normal / high against the record's own healthy ranges. BMI is left
# out: its range fields hold category cut-offs, not a healthy range.
STATUS_SENSORS: tuple[RenphoSensorDescription, ...] = (
    _status_desc("weight_status", lambda d: _num(d, "weight"), "weightMin", "weightMax"),
    _status_desc("body_fat_status", lambda d: _num(d, "bodyfat"), "bfpMin", "bfpMax"),
    _status_desc("body_fat_mass_status", _fat_mass, "bfmMin", "bfmMax"),
    _status_desc("muscle_mass_status", lambda d: _num(d, "sinew"), "muscleMassMin", "muscleMassMax"),
    _status_desc("skeletal_muscle_mass_status", lambda d: _num(d, "smmMass"), "smmMin", "smmMax"),
    _status_desc("bone_mass_status", lambda d: _num(d, "bone"), "boneMin", "boneMax"),
    _status_desc("bmr_status", lambda d: _num(d, "bmr"), "bmrMin", "bmrMax"),
    _status_desc("body_water_status", _mass_of("water"), "waterMassMin", "waterMassMax"),
    _status_desc("protein_status", _mass_of("protein"), "proteinMassMin", "proteinMassMax"),
)

# Goals and body measurements from each account's profile (set in the Renpho
# app; created once set). Normalised to kg / cm by cloud_api.profile_goals.
CM = UnitOfLength.CENTIMETERS
GOAL_SENSORS: tuple[RenphoSensorDescription, ...] = (
    _d("weight_goal", KG, WEIGHT, prec=2, optional=True, from_profile=True, history=False),
    _d("weight_to_goal", KG, WEIGHT, prec=2, zero_ok=True, optional=True, from_profile=True, history=False),
    RenphoSensorDescription(
        key="weight_goal_date", translation_key="weight_goal_date", device_class=SensorDeviceClass.DATE,
        value_fn=lambda d: dt_util.parse_date(d["weight_goal_date"]) if d.get("weight_goal_date") else None,
        optional=True, from_profile=True, history=False,
    ),
    _d("body_fat_goal", PERCENTAGE, optional=True, from_profile=True, history=False),
    _d("starting_weight", KG, WEIGHT, prec=2, optional=True, from_profile=True, history=False),
    _d("starting_body_fat", PERCENTAGE, optional=True, from_profile=True, history=False),
    _d("waist", CM, SensorDeviceClass.DISTANCE, optional=True, from_profile=True, history=False),
    _d("hip", CM, SensorDeviceClass.DISTANCE, optional=True, from_profile=True, history=False),
)

ALL_CLOUD_SENSORS = CLOUD_SENSORS + MORPHOSCAN_SENSORS + STATUS_SENSORS + GOAL_SENSORS
# Keys of entities earlier versions created that no longer exist
RETIRED_CLOUD_KEYS = ("bone_percent",)
# Created unconditionally by earlier versions; removed where the data is absent
RETIRED_UNLESS_PRESENT = ("heart_rate",)


PROFILE_SENSORS: tuple[RenphoSensorDescription, ...] = (
    _d("weight", KG, WEIGHT, prec=2),
    _d("bmi"),
    _d("body_fat", PERCENTAGE),
    _d("body_water", PERCENTAGE),
    _d("skeletal_muscle", PERCENTAGE),
    _d("muscle_mass", KG, WEIGHT, prec=2),
    _d("bone_mass", KG, WEIGHT, prec=2),
    _d("fat_free_mass", KG, WEIGHT, prec=2),
    _d("protein", PERCENTAGE),
    _d("bmr", KCAL, prec=0),
    _d("visceral_fat", prec=0),
    _d("impedance", OHM, src="resistance", prec=0, entity_registry_enabled_default=False),
    RenphoSensorDescription(
        key="last_measurement", translation_key="last_measurement",
        device_class=SensorDeviceClass.TIMESTAMP, value_fn=_ts("measured_at"),
    ),
)

SCALE_SENSORS: tuple[RenphoSensorDescription, ...] = (
    _d("last_weight", KG, WEIGHT, src="weight", prec=2),
    RenphoSensorDescription(
        key="last_profile", translation_key="last_profile",
        value_fn=lambda d: d.get("profile") or ("unassigned" if d.get("weight") else None),
    ),
    _d("impedance", OHM, src="resistance", prec=0),
    RenphoSensorDescription(
        key="last_measurement", translation_key="last_measurement",
        device_class=SensorDeviceClass.TIMESTAMP, value_fn=_ts("measured_at"),
    ),
    _d("battery", PERCENTAGE, SensorDeviceClass.BATTERY, prec=0, entity_category=EntityCategory.DIAGNOSTIC),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: RenphoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    if entry.data.get(CONF_SOURCE) == SOURCE_CLOUD:
        coordinator: RenphoCloudCoordinator = entry.runtime_data
        known: set[tuple[str, str]] = set()
        registry = er.async_get(hass)
        by_key = {desc.key: desc for desc in ALL_CLOUD_SENSORS}
        for uid, record in (coordinator.data or {}).get("latest", {}).items():
            retire = [*RETIRED_CLOUD_KEYS, *(k for k in RETIRED_UNLESS_PRESENT if by_key[k].value_fn(record) is None)]
            for key in retire:
                if entity_id := registry.async_get_entity_id("sensor", DOMAIN, cloud_unique_id(entry, uid, key)):
                    registry.async_remove(entity_id)

        @callback
        def _add_entities() -> None:
            new: list[RenphoCloudSensor] = []
            data = coordinator.data or {}
            for uid, record in data.get("latest", {}).items():
                profile = data.get("profiles", {}).get(uid, {})
                for desc in ALL_CLOUD_SENSORS:
                    if (uid, desc.key) in known:
                        continue
                    if desc.optional and desc.value_fn(profile if desc.from_profile else record) is None:
                        continue  # e.g. segment metrics on a 4-electrode scale
                    known.add((uid, desc.key))
                    new.append(RenphoCloudSensor(coordinator, uid, desc))
            if new:
                async_add_entities(new)

        _add_entities()
        entry.async_on_unload(coordinator.async_add_listener(_add_entities))
        return

    scale: RenphoBleScale = entry.runtime_data
    async_add_entities(
        [RenphoScaleSensor(scale, None, d) for d in SCALE_SENSORS]
        + [RenphoScaleStatusSensor(scale), RenphoScaleRssiSensor(scale)]
    )
    for sid, sub in entry.subentries.items():
        if sub.subentry_type == SUBENTRY_PROFILE:
            async_add_entities(
                [RenphoScaleSensor(scale, sid, d) for d in PROFILE_SENSORS],
                config_subentry_id=sid,
            )


def cloud_scope(entry: RenphoConfigEntry) -> str:
    # Scoped to the account: two family accounts each list the other's members
    return entry.unique_id or entry.entry_id


def cloud_unique_id(entry: RenphoConfigEntry, uid: str, key: str) -> str:
    return f"{cloud_scope(entry)}_{uid}_{key}"


def cloud_device_info(coordinator: RenphoCloudCoordinator, uid: str) -> DeviceInfo:
    """The person's device; every platform describes it in full (any may create it)."""
    name = (coordinator.data or {}).get("users", {}).get(uid, uid)
    return DeviceInfo(
        identifiers={(DOMAIN, f"{cloud_scope(coordinator.config_entry)}_{uid}")},
        name=f"Renpho Cloud {name}",
        manufacturer="Renpho",
        model="Renpho Health account",
    )


class RenphoCloudSensor(CoordinatorEntity[RenphoCloudCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: RenphoSensorDescription

    def __init__(self, coordinator: RenphoCloudCoordinator, uid: str, desc: RenphoSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = desc
        self._uid = uid
        self._attr_unique_id = cloud_unique_id(coordinator.config_entry, uid, desc.key)
        self._attr_device_info = cloud_device_info(coordinator, uid)

    @property
    def _record(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        if self.entity_description.from_profile:
            return data.get("profiles", {}).get(self._uid, {})
        return data.get("latest", {}).get(self._uid, {})

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self._record)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        r = self._record
        if self.entity_description.key == "last_measurement":
            return {"scale": r.get("scaleName") or r.get("internalModel")}
        if not (keys := self.entity_description.range_keys):
            return None
        attrs = {
            name: r[key]
            for name, key in zip(("normal_min", "normal_max", "standard"), keys, strict=True)
            if key and isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)
        }
        return attrs or None


class _BleEntity(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, scale: RenphoBleScale, subentry_id: str | None) -> None:
        self._scale = scale
        self._sid = subentry_id
        scale_ident = (DOMAIN, scale.address)
        if subentry_id is None:
            model = proto.model_name(scale.entry.data.get(CONF_MODEL_ID))
            self._attr_device_info = DeviceInfo(
                identifiers={scale_ident},
                connections={(CONNECTION_BLUETOOTH, scale.address)},
                name=f"Renpho {model}",
                manufacturer="Renpho",
                model=model,
                sw_version=scale.scale.get("firmware"),
            )
        else:
            sub = scale.entry.subentries[subentry_id]
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"{scale.address}_{subentry_id}")},
                name=f"Renpho Scale {sub.data[CONF_NAME]}",
                manufacturer="Renpho",
                model="Scale profile",
                via_device=scale_ident,
            )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_BLE_UPDATE.format(self._scale.entry.entry_id), self.async_write_ha_state
            )
        )


class RenphoScaleSensor(_BleEntity):
    entity_description: RenphoSensorDescription

    def __init__(self, scale: RenphoBleScale, sid: str | None, desc: RenphoSensorDescription) -> None:
        super().__init__(scale, sid)
        self.entity_description = desc
        self._attr_unique_id = f"{scale.address}_{sid or 'scale'}_{desc.key}"

    @property
    def native_value(self) -> Any:
        data = self._scale.scale if self._sid is None else self._scale.profiles.get(self._sid, {})
        return self.entity_description.value_fn(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key not in ("last_measurement", "last_profile"):
            return None
        data = self._scale.scale if self._sid is None else self._scale.profiles.get(self._sid, {})
        attrs = {"method": data.get("method")}
        if self._sid is None:
            attrs["profile_id"] = data.get("profile_id")
        return attrs


class RenphoScaleStatusSensor(_BleEntity):
    _attr_translation_key = "status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "connecting", "connected", "no_connectable_adapter", "error"]

    def __init__(self, scale: RenphoBleScale) -> None:
        super().__init__(scale, None)
        self._attr_unique_id = f"{scale.address}_status"

    @property
    def native_value(self) -> str:
        return self._scale.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self._scale
        return {
            "last_error": s.last_error,
            "last_seen": s.last_seen.isoformat() if s.last_seen else None,
            "via": s.source,
            "sessions": s.sessions,
            "model_id": f"0x{s.model_id:04x}" if s.model_id is not None else None,
            "protocol": s.family,
            "model_label": proto.MODEL_LABELS.get(s.model_id, "unknown") if s.model_id is not None else None,
            "live_weight": s.scale.get("live_weight"),
            "unknown_frames": [f"0x{c:02x}" for c in sorted(s.unknown_cmds)],
        }


class RenphoScaleRssiSensor(_BleEntity):
    _attr_translation_key = "rssi"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = M
    _attr_entity_registry_enabled_default = False

    def __init__(self, scale: RenphoBleScale) -> None:
        super().__init__(scale, None)
        self._attr_unique_id = f"{scale.address}_rssi"

    @property
    def native_value(self) -> int | None:
        return self._scale.rssi
