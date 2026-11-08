"""Shared Renpho entity identities, device information and subscriptions."""

from typing import TypeVar

from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo, DeviceEntryType
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ble_protocol as proto
from .ble_scale import RenphoBleScale
from .const import CONF_MODEL_ID, CONF_NAME, DOMAIN, SIGNAL_BLE_UPDATE
from .coordinator import RenphoCloudCoordinator, RenphoConfigEntry


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
        entry_type=DeviceEntryType.SERVICE,
    )


class RenphoCloudEntity(CoordinatorEntity[RenphoCloudCoordinator]):
    """Common cloud person identity across platforms."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RenphoCloudCoordinator, uid: str, key: str) -> None:
        super().__init__(coordinator)
        self._uid = uid
        self._attr_unique_id = cloud_unique_id(coordinator.config_entry, uid, key)
        self._attr_device_info = cloud_device_info(coordinator, uid)


class RenphoBleEntity(Entity):
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
                entry_type=DeviceEntryType.SERVICE,
                via_device_id=scale.device_id,
            )

    @property
    def available(self) -> bool:
        """A known transport failure invalidates readings until recovery."""
        return self._scale.connection_available

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_BLE_UPDATE.format(self._scale.entry.entry_id), self.async_write_ha_state
            )
        )


_KeyT = TypeVar("_KeyT")
_EntityT = TypeVar("_EntityT", bound=Entity)


@callback
def track_entity_removal(hass: HomeAssistant, entry: RenphoConfigEntry, known: dict[_KeyT, _EntityT]) -> None:
    """Forget removed entities so a returning person can be discovered again."""
    @callback
    def removed(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        if event.data["action"] == "remove":
            for key, entity in list(known.items()):
                if entity.entity_id == event.data["entity_id"]:
                    del known[key]

    entry.async_on_unload(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, removed))
