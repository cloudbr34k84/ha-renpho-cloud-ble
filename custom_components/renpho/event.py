"""A "Measurement" event per cloud person, fired once for every new weigh-in.

Readings already in the account when Home Assistant starts don't fire. The
newest reading that did fire is remembered (via the event's own restored
data), so weigh-ins made while Home Assistant was down still fire, in order.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .cloud_api import _ts
from .const import CONF_SOURCE, SOURCE_CLOUD
from .coordinator import RenphoCloudCoordinator, RenphoConfigEntry
from .sensor import cloud_device_info, cloud_unique_id

PARALLEL_UPDATES = 0
EVENT_MEASUREMENT = "measurement"

# Record field -> event attribute. Only numbers; other fields stay out.
_EVENT_FIELDS = {
    "weight": "weight",
    "bmi": "bmi",
    "bodyfat": "body_fat",
    "water": "body_water",
    "muscle": "skeletal_muscle",
    "sinew": "muscle_mass",
    "bone": "bone_mass",
    "visfat": "visceral_fat",
    "bmr": "bmr",
    "smmMass": "skeletal_muscle_mass",
    "bodyScore": "body_score",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: RenphoConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    if entry.data.get(CONF_SOURCE) != SOURCE_CLOUD:
        return
    coordinator: RenphoCloudCoordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_people() -> None:
        new = [uid for uid in (coordinator.data or {}).get("latest", {}) if uid not in known]
        known.update(new)
        if new:
            async_add_entities(RenphoMeasurementEvent(coordinator, uid) for uid in new)

    _add_people()
    entry.async_on_unload(coordinator.async_add_listener(_add_people))


class RenphoMeasurementEvent(CoordinatorEntity[RenphoCloudCoordinator], EventEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "measurement"
    _attr_event_types = [EVENT_MEASUREMENT]

    def __init__(self, coordinator: RenphoCloudCoordinator, uid: str) -> None:
        super().__init__(coordinator)
        self._uid = uid
        entry = coordinator.config_entry
        self._attr_unique_id = cloud_unique_id(entry, uid, "measurement_event")
        self._attr_device_info = cloud_device_info(coordinator, uid)
        self._fired_until: int | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_event_data()) and last.last_event_attributes:
            self._fired_until = last.last_event_attributes.get("time_stamp")
        if self._fired_until is None:
            # First run: what's already in the account is history, not news
            latest = (self.coordinator.data or {}).get("latest", {}).get(self._uid)
            self._fired_until = _ts(latest) if latest else 0
        self._fire_new()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._fire_new()
        super()._handle_coordinator_update()

    def _fire_new(self) -> None:
        records = (self.coordinator.data or {}).get("records", [])
        new = [r for r in records if r.get("userId") == self._uid and _ts(r) > (self._fired_until or 0)]
        for record in sorted(new, key=_ts):
            self._trigger_event(EVENT_MEASUREMENT, _event_data(record))
            self._fired_until = _ts(record)
            self.async_write_ha_state()


def _event_data(record: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "time_stamp": _ts(record),
        "measured_at": dt_util.utc_from_timestamp(_ts(record)).isoformat(),
    }
    for field, name in _EVENT_FIELDS.items():
        value = record.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value:
            data[name] = value
    return data
