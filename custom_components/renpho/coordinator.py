"""Cloud polling coordinator."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ble_scale import RenphoBleScale
from .cloud_api import CloudData, RenphoAuthError, RenphoCloud, RenphoCloudError, RenphoRateLimited
from .const import DEFAULT_SCAN_INTERVAL_MIN, DOMAIN

if TYPE_CHECKING:
    from .history import CloudHistory

_LOGGER = logging.getLogger(__name__)


class RenphoCloudCoordinator(DataUpdateCoordinator[CloudData]):
    """Polls the Renpho cloud for each person's newest measurement."""

    config_entry: RenphoConfigEntry

    def __init__(self, hass: HomeAssistant, entry: RenphoConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_cloud",
            update_interval=timedelta(minutes=DEFAULT_SCAN_INTERVAL_MIN),
        )
        self.history: CloudHistory  # attached once the platform is set up
        self.api = RenphoCloud(
            async_get_clientsession(hass), entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD]
        )

    async def _async_update_data(self) -> CloudData:
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
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="rate_limited", translation_placeholders={"seconds": str(err.retry_after)}, retry_after=err.retry_after) from err
        except RenphoAuthError as err:
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="invalid_auth") from err
        except RenphoCloudError as err:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="cloud_update_failed", translation_placeholders={"error": str(err)}) from err


type RenphoConfigEntry = ConfigEntry[RenphoCloudCoordinator | RenphoBleScale]
