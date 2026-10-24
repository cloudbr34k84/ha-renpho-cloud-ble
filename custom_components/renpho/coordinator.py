"""Cloud polling coordinator."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ble_scale import RenphoBleScale
from .cloud_api import RenphoAuthError, RenphoCloud, RenphoCloudError, RenphoRateLimited
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MIN, DOMAIN

_LOGGER = logging.getLogger(__name__)


class RenphoCloudCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the Renpho cloud for each person's newest measurement."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_cloud",
            update_interval=timedelta(
                minutes=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MIN)
            ),
        )
        self.history: Any = None  # CloudHistory, attached once the platform is set up
        self.api = RenphoCloud(
            async_get_clientsession(hass), entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD]
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            try:
                return await self.api.latest_measurements()
            except RenphoRateLimited:
                raise
            except RenphoCloudError:
                # Token expiry has no documented error code, so any failure
                # gets one fresh login (latest_measurements logs in) first
                self.api.token = None
                return await self.api.latest_measurements()
        except RenphoRateLimited as err:
            raise UpdateFailed(str(err), retry_after=err.retry_after) from err
        except RenphoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except RenphoCloudError as err:
            raise UpdateFailed(str(err)) from err


type RenphoConfigEntry = ConfigEntry[RenphoCloudCoordinator | RenphoBleScale]
