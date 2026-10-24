"""Mirror Renpho cloud measurements into Home Assistant long-term statistics.

Each cloud sensor with a numeric metric gets every past measurement written
into its own statistics (hourly mean/min/max, like the recorder makes from
states), so its history graph goes back as far as the Renpho account does,
and weigh-ins that happened between two polls still appear at their own time.

Progress is kept per statistic. A statistic seen for the first time triggers
one full-history fetch; after that only records newer than the last imported
one are written, from the pages every poll already fetches.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
import logging
from typing import Any

from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    STATISTIC_UNIT_TO_UNIT_CONVERTER,
    async_import_statistics,
)
from homeassistant.components.sensor import UNIT_CONVERTERS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .cloud_api import _ts
from .const import DOMAIN
from .coordinator import RenphoCloudCoordinator
from .sensor import ALL_CLOUD_SENSORS, RenphoSensorDescription, cloud_unique_id

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1


def _unit_class(desc: RenphoSensorDescription, unit: str | None) -> str | None:
    """The unit class the sensor recorder would give this statistic."""
    if desc.device_class and (conv := UNIT_CONVERTERS.get(desc.device_class)) and unit in conv.VALID_UNITS:
        return conv.UNIT_CLASS
    if conv := STATISTIC_UNIT_TO_UNIT_CONVERTER.get(unit):
        return conv.UNIT_CLASS
    return None


class CloudHistory:
    """Owns the import progress for one cloud entry."""

    def __init__(self, hass: HomeAssistant, coordinator: RenphoCloudCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        entry = coordinator.config_entry
        self._store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.history.{entry.entry_id}")
        self._progress: dict[str, int] | None = None  # statistic_id -> newest imported timestamp
        self._lock = asyncio.Lock()

    async def async_reset(self) -> None:
        async with self._lock:
            self._progress = {}
            await self._store.async_save({"progress": {}})

    async def async_remove(self) -> None:
        await self._store.async_remove()

    async def async_sync(self) -> int:
        """Import whatever hasn't been imported yet; returns statistics rows written."""
        if "recorder" not in self.hass.config.components:
            return 0
        async with self._lock:
            return await self._sync()

    async def _sync(self) -> int:
        if self._progress is None:
            self._progress = (await self._store.async_load() or {}).get("progress", {})
        targets = self._targets()
        if not targets:
            return 0
        if any(stat_id not in self._progress for stat_id, *_ in targets):
            # A metric with no history imported yet: fetch everything once
            records = (await self.coordinator.api.latest_measurements(everything=True))["records"]
        else:
            records = (self.coordinator.data or {}).get("records", [])
        written = 0
        for stat_id, uid, desc in targets:
            written += self._import(stat_id, desc, [r for r in records if r.get("userId") == uid])
        await self._store.async_save({"progress": self._progress})
        return written

    def _targets(self) -> list[tuple[str, str, RenphoSensorDescription]]:
        registry = er.async_get(self.hass)
        entry = self.coordinator.config_entry
        out = []
        for uid in (self.coordinator.data or {}).get("latest", {}):
            for desc in ALL_CLOUD_SENSORS:
                if not desc.history or desc.state_class is None:
                    continue
                entity_id = registry.async_get_entity_id("sensor", DOMAIN, cloud_unique_id(entry, uid, desc.key))
                entity = registry.async_get(entity_id) if entity_id else None
                if entity is None or entity.disabled:
                    continue
                out.append((entity_id, uid, desc))
        return out

    def _import(self, stat_id: str, desc: RenphoSensorDescription, records: list[dict[str, Any]]) -> int:
        assert self._progress is not None
        done = self._progress.get(stat_id, 0)
        native = desc.native_unit_of_measurement
        state = self.hass.states.get(stat_id)
        unit = state.attributes.get("unit_of_measurement", native) if state else native
        converter = UNIT_CONVERTERS.get(desc.device_class) if desc.device_class else None

        hours: dict[datetime, list[float]] = defaultdict(list)
        newest = done
        for record in records:
            ts = _ts(record)
            value = desc.value_fn(record)
            if ts <= done or not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            if converter and unit != native and unit in converter.VALID_UNITS:
                value = converter.convert(value, native, unit)
            hour = dt_util.utc_from_timestamp(ts).replace(minute=0, second=0, microsecond=0)
            hours[hour].append(float(value))
            newest = max(newest, ts)
        self._progress[stat_id] = newest
        if not hours:
            return 0

        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.ARITHMETIC,
            has_sum=False,
            name=None,
            source="recorder",
            statistic_id=stat_id,
            unit_class=_unit_class(desc, unit),
            unit_of_measurement=unit,
        )
        async_import_statistics(
            self.hass,
            metadata,
            [
                StatisticData(start=hour, mean=sum(v) / len(v), min=min(v), max=max(v))
                for hour, v in sorted(hours.items())
            ],
        )
        _LOGGER.debug("Imported %s hour(s) of Renpho history into %s", len(hours), stat_id)
        return len(hours)
