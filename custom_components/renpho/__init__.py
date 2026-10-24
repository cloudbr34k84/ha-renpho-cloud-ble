"""Renpho scales — cloud (Renpho Health account) and local BLE in one integration.

Builds on DiscountDarcy/ha-renpho-health (cloud) and
ronnnnnnnnnnnnn/renpho_fitness_scale_ble + renpho-escs20m (BLE), all MIT.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store

from .ble_scale import STORE_VERSION, RenphoBleScale
from .const import CONF_SOURCE, DOMAIN, PLATFORMS, SOURCE_BLE, SOURCE_CLOUD
from .coordinator import RenphoCloudCoordinator, RenphoConfigEntry
from .history import CloudHistory

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICE_REASSIGN = "reassign_last_measurement"
SERVICE_IMPORT_HISTORY = "import_history"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async def _reassign(call: ServiceCall) -> None:
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if entry is None or entry.domain != DOMAIN or entry.data.get(CONF_SOURCE) != SOURCE_BLE:
            raise ServiceValidationError("Not a Renpho Bluetooth scale entry")
        if call.data["profile_id"] not in entry.subentries:
            raise ServiceValidationError("Unknown profile id")
        entry.runtime_data.reassign_last(call.data["profile_id"])

    hass.services.async_register(
        DOMAIN,
        SERVICE_REASSIGN,
        _reassign,
        schema=vol.Schema({vol.Required("config_entry_id"): cv.string, vol.Required("profile_id"): cv.string}),
    )

    async def _import_history(call: ServiceCall) -> ServiceResponse:
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if entry is None or entry.domain != DOMAIN or entry.data.get(CONF_SOURCE) != SOURCE_CLOUD:
            raise ServiceValidationError("Not a Renpho cloud entry")
        if not isinstance(getattr(entry, "runtime_data", None), RenphoCloudCoordinator):
            raise ServiceValidationError("The Renpho cloud entry isn't loaded")
        history: CloudHistory = entry.runtime_data.history
        await history.async_reset()
        return {"hours_imported": await history.async_sync()}

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        _import_history,
        schema=vol.Schema({vol.Required("config_entry_id"): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: RenphoConfigEntry) -> bool:
    if entry.data.get(CONF_SOURCE) == SOURCE_CLOUD:
        coordinator = RenphoCloudCoordinator(hass, entry)
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator
    else:
        scale = RenphoBleScale(hass, entry)
        await scale.async_start()
        entry.runtime_data = scale
    entry.async_on_unload(entry.add_update_listener(_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if isinstance(entry.runtime_data, RenphoCloudCoordinator):
        coordinator = entry.runtime_data
        coordinator.history = CloudHistory(hass, coordinator)

        @callback
        def _sync_history() -> None:
            entry.async_create_background_task(
                hass, coordinator.history.async_sync(), f"renpho_history_{entry.entry_id}"
            )

        _sync_history()  # the sensors exist by now, so the backfill can find them
        entry.async_on_unload(coordinator.async_add_listener(_sync_history))
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options, credentials or people changed — rebuild entities from scratch.

    This listener is the only thing that reloads a loaded entry; the flows
    deliberately avoid the ``*_reload_and_abort`` helpers (HA 2026.12 rule).
    """
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RenphoConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok and isinstance(entry.runtime_data, RenphoBleScale):
        await entry.runtime_data.async_stop()
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the integration's own storage when an entry is removed."""
    if entry.data.get(CONF_SOURCE) == SOURCE_BLE:
        await Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
    else:
        await Store(hass, 1, f"{DOMAIN}.history.{entry.entry_id}").async_remove()
