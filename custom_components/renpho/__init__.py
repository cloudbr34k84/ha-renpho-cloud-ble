"""Renpho scales — cloud (Renpho Health account) and local BLE in one integration.

Builds on DiscountDarcy/ha-renpho-health (cloud) and
ronnnnnnnnnnnnn/renpho_fitness_scale_ble + renpho-escs20m (BLE), all MIT.
"""

from __future__ import annotations

from typing import cast

import probatio as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, service, device_registry as dr, issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .ble_scale import STORE_VERSION, RenphoBleScale
from .cloud_api import RenphoCloudError
from .const import CONF_SOURCE, DOMAIN, PLATFORMS, SOURCE_BLE, SOURCE_CLOUD
from .entity import cloud_scope
from .coordinator import RenphoCloudCoordinator, RenphoConfigEntry
from .history import CloudHistory

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICE_REASSIGN = "reassign_last_measurement"
SERVICE_IMPORT_HISTORY = "import_history"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def _reassign(call: ServiceCall) -> None:
        entry: RenphoConfigEntry = service.async_get_config_entry(hass, DOMAIN, call.data["config_entry_id"])
        if entry.data.get(CONF_SOURCE) != SOURCE_BLE:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_ble_entry")
        scale = getattr(entry, "runtime_data", None)
        if not isinstance(scale, RenphoBleScale):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_not_loaded")
        if call.data["profile_id"] not in entry.subentries:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="unknown_profile")
        if scale.scale.get("weight") is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_measurement")
        scale.reassign_last(call.data["profile_id"])

    hass.services.async_register(
        DOMAIN,
        SERVICE_REASSIGN,
        _reassign,
        schema=vol.Schema({vol.Required("config_entry_id"): cv.string, vol.Required("profile_id"): cv.string}),
    )

    async def _import_history(call: ServiceCall) -> ServiceResponse:
        entry: RenphoConfigEntry = service.async_get_config_entry(hass, DOMAIN, call.data["config_entry_id"])
        if entry.data.get(CONF_SOURCE) != SOURCE_CLOUD:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_cloud_entry")
        coordinator = getattr(entry, "runtime_data", None)
        if not isinstance(coordinator, RenphoCloudCoordinator):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_not_loaded")
        if "recorder" not in hass.config.components:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="recorder_required")
        history: CloudHistory = coordinator.history
        try:
            await history.async_reset()
            return {"hours_imported": await history.async_sync()}
        except (RenphoCloudError, OSError) as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="history_import_failed", translation_placeholders={"error": str(err)}) from err

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
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        if isinstance(entry.runtime_data, RenphoBleScale):
            await entry.runtime_data.async_stop()
        raise
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


async def _reload(hass: HomeAssistant, entry: RenphoConfigEntry) -> None:
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


async def async_remove_entry(hass: HomeAssistant, entry: RenphoConfigEntry) -> None:
    """Delete the integration's own storage when an entry is removed."""
    ir.async_delete_issue(hass, DOMAIN, f"no_adapter_{entry.entry_id}")
    if entry.data.get(CONF_SOURCE) == SOURCE_BLE:
        await Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
    else:
        await Store(hass, 1, f"{DOMAIN}.history.{entry.entry_id}").async_remove()


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: RenphoConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow users to remove devices that no longer supply data."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return False
    if isinstance(runtime, RenphoCloudCoordinator):
        coordinator = runtime
        if not coordinator.last_update_success:
            return False
        current = {(DOMAIN, f"{cloud_scope(entry)}_{uid}") for uid in (coordinator.data or {}).get("latest", {})}
    else:
        address = cast(RenphoBleScale, runtime).address
        current = {(DOMAIN, address), *((DOMAIN, f"{address}_{sid}") for sid in entry.subentries)}
    return not bool(device_entry.identifiers & current)
