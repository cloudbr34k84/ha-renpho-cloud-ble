"""Config flow for Renpho (cloud account or local Bluetooth scale)."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import ble_protocol as proto
from .ble_scale import WRITES_ALWAYS, WRITES_AUTO, WRITES_NEVER
from .cloud_api import RenphoAuthError, RenphoCloud, RenphoCloudError
from .const import (
    ALGO_ALT,
    ALGO_DEFAULT,
    CONF_ADDRESS,
    CONF_ALGORITHM,
    CONF_ALLOW_WRITES,
    CONF_ATHLETE,
    CONF_BIRTHDAY,
    CONF_CAPTURE,
    CONF_HEIGHT_CM,
    CONF_MODEL_ID,
    CONF_NAME,
    CONF_PROFILE_BYTE,
    CONF_SCAN_INTERVAL,
    CONF_SEX,
    CONF_SOURCE,
    CONF_START_WEIGHT,
    CONF_TOLERANCE,
    DEFAULT_SCAN_INTERVAL_MIN,
    DEFAULT_TOLERANCE_KG,
    DOMAIN,
    MIN_SCAN_INTERVAL_MIN,
    SEX_FEMALE,
    SEX_MALE,
    SOURCE_BLE,
    SOURCE_CLOUD,
    SUBENTRY_PROFILE,
)

_LOGGER = logging.getLogger(__name__)

CLOUD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): selector.TextSelector(selector.TextSelectorConfig(type="email")),
        vol.Required(CONF_PASSWORD): selector.TextSelector(selector.TextSelectorConfig(type="password")),
    }
)


def _scale_title(model_id: int | None, address: str) -> str:
    return f"Renpho {proto.model_name(model_id)} ({address})"


class RenphoConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._error_detail = ""
        self._discovered: dict[str, int] = {}
        self._address: str | None = None
        self._model_id: int | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RenphoOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        if config_entry.data.get(CONF_SOURCE) == SOURCE_BLE:
            return {SUBENTRY_PROFILE: ProfileSubentryFlow}
        return {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["cloud", "pick_scale"])

    # ---------------------------------------------------------------- cloud
    async def _validate_cloud(self, data: Mapping[str, Any]) -> tuple[str | None, RenphoCloud | None]:
        api = RenphoCloud(async_get_clientsession(self.hass), data[CONF_EMAIL], data[CONF_PASSWORD])
        try:
            await api.login()
        except RenphoAuthError as err:
            # Renpho's result code is the only clue to why; it carries no credentials
            _LOGGER.warning("Renpho cloud login rejected (Renpho code %s)", err.code)
            self._error_detail = str(err.code)
            return "invalid_auth", None
        except RenphoCloudError as err:
            _LOGGER.warning("Renpho cloud login failed: %s", err)
            self._error_detail = str(err)
            return "cannot_connect", None
        return None, api

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            error, api = await self._validate_cloud(user_input)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(f"cloud_{api.user_id}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Renpho Cloud ({user_input[CONF_EMAIL]})",
                    data={CONF_SOURCE: SOURCE_CLOUD, **user_input},
                )
        return self.async_show_form(
            step_id="cloud",
            data_schema=self.add_suggested_values_to_schema(
                CLOUD_SCHEMA, {CONF_EMAIL: user_input[CONF_EMAIL]} if user_input else {}
            ),
            errors=errors,
            description_placeholders={"detail": self._error_detail},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**entry.data, **user_input}
            error, _ = await self._validate_cloud(data)
            if error:
                errors["base"] = error
            else:
                self.hass.config_entries.async_update_entry(entry, data=data)
                if entry.state is not ConfigEntryState.LOADED:
                    # Failed setup leaves no update listener to do the reload
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): CLOUD_SCHEMA.schema[CONF_PASSWORD]}),
            errors=errors,
            description_placeholders={"detail": self._error_detail},
        )

    # ------------------------------------------------------------ bluetooth
    async def async_step_bluetooth(self, discovery_info: bluetooth.BluetoothServiceInfoBleak) -> ConfigFlowResult:
        payload = discovery_info.manufacturer_data.get(proto.MANUFACTURER_ID)
        advert = proto.parse_advert(bytes(payload), discovery_info.address) if payload else None
        if advert is None:
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(discovery_info.address.upper())
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address.upper()
        self._model_id = advert.model_id
        self.context["title_placeholders"] = {"name": _scale_title(advert.model_id, self._address)}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._create_scale()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": _scale_title(self._model_id, self._address)},
        )

    async def async_step_pick_scale(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._address = user_input[CONF_ADDRESS].upper()
            self._model_id = self._discovered.get(self._address)
            await self.async_set_unique_id(self._address)
            self._abort_if_unique_id_configured()
            return self._create_scale()
        configured = self._async_current_ids(include_ignore=False)
        for info in bluetooth.async_discovered_service_info(self.hass, connectable=False):
            payload = info.manufacturer_data.get(proto.MANUFACTURER_ID)
            if payload is None or info.address.upper() in configured:
                continue
            advert = proto.parse_advert(bytes(payload), info.address)
            if advert:
                self._discovered[info.address.upper()] = advert.model_id
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="pick_scale",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {addr: _scale_title(mid, addr) for addr, mid in self._discovered.items()}
                    )
                }
            ),
        )

    def _create_scale(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title=_scale_title(self._model_id, self._address),
            data={CONF_SOURCE: SOURCE_BLE, CONF_ADDRESS: self._address, CONF_MODEL_ID: self._model_id},
            options={CONF_ALLOW_WRITES: WRITES_AUTO, CONF_CAPTURE: True},
        )


class RenphoOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        if self.config_entry.data.get(CONF_SOURCE) == SOURCE_CLOUD:
            schema = vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MIN)): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=MIN_SCAN_INTERVAL_MIN, max=1440, step=5, unit_of_measurement="min", mode="box")
                    ),
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Required(CONF_ALLOW_WRITES, default=opts.get(CONF_ALLOW_WRITES, WRITES_AUTO)): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=[WRITES_AUTO, WRITES_NEVER, WRITES_ALWAYS], translation_key=CONF_ALLOW_WRITES)
                    ),
                    vol.Required(CONF_CAPTURE, default=opts.get(CONF_CAPTURE, True)): bool,
                }
            )
        return self.async_show_form(step_id="init", data_schema=schema)


def _profile_schema(d: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=d.get(CONF_NAME, vol.UNDEFINED)): str,
            vol.Required(CONF_SEX, default=d.get(CONF_SEX, SEX_MALE)): selector.SelectSelector(
                selector.SelectSelectorConfig(options=[SEX_MALE, SEX_FEMALE], translation_key=CONF_SEX)
            ),
            vol.Required(CONF_BIRTHDAY, default=d.get(CONF_BIRTHDAY, vol.UNDEFINED)): selector.DateSelector(),
            vol.Required(CONF_HEIGHT_CM, default=d.get(CONF_HEIGHT_CM, vol.UNDEFINED)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=50, max=250, step=0.5, unit_of_measurement="cm", mode="box")
            ),
            vol.Required(CONF_START_WEIGHT, default=d.get(CONF_START_WEIGHT, vol.UNDEFINED)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=10, max=300, step=0.1, unit_of_measurement="kg", mode="box")
            ),
            vol.Required(CONF_TOLERANCE, default=d.get(CONF_TOLERANCE, DEFAULT_TOLERANCE_KG)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0.5, max=15, step=0.5, unit_of_measurement="kg", mode="box")
            ),
            vol.Required(CONF_ALGORITHM, default=d.get(CONF_ALGORITHM, ALGO_DEFAULT)): selector.SelectSelector(
                selector.SelectSelectorConfig(options=[ALGO_DEFAULT, ALGO_ALT], translation_key=CONF_ALGORITHM)
            ),
            vol.Required(CONF_ATHLETE, default=d.get(CONF_ATHLETE, False)): bool,
            vol.Optional(
                CONF_PROFILE_BYTE, description={"suggested_value": d.get(CONF_PROFILE_BYTE)}
            ): selector.TextSelector(),
        }
    )


def _profile_errors(user_input: dict[str, Any]) -> dict[str, str]:
    byte = (user_input.get(CONF_PROFILE_BYTE) or "").strip()
    if byte and not re.fullmatch(r"[0-9a-fA-F]{2}", byte):
        return {CONF_PROFILE_BYTE: "invalid_profile_byte"}
    return {}


def _clean_profile(user_input: dict[str, Any]) -> dict[str, Any]:
    data = dict(user_input)
    byte = (data.pop(CONF_PROFILE_BYTE, None) or "").strip()
    if byte:
        data[CONF_PROFILE_BYTE] = byte.upper()
    return data


class ProfileSubentryFlow(ConfigSubentryFlow):
    """A person who uses the scale; readings are matched to them by weight."""

    def _name_taken(self, name: str, exclude: str | None = None) -> bool:
        key = name.strip().casefold()
        return any(
            sid != exclude and key in (sub.unique_id, sub.title.strip().casefold())
            for sid, sub in self._get_entry().subentries.items()
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _profile_errors(user_input)
            if self._name_taken(user_input[CONF_NAME]):
                errors[CONF_NAME] = "name_exists"
            if not errors:
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data=_clean_profile(user_input),
                    unique_id=user_input[CONF_NAME].strip().casefold(),
                )
        return self.async_show_form(
            step_id="user", data_schema=_profile_schema(user_input or {}), errors=errors
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        sub = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _profile_errors(user_input)
            if self._name_taken(user_input[CONF_NAME], exclude=sub.subentry_id):
                errors[CONF_NAME] = "name_exists"
            if not errors:
                return self.async_update_and_abort(
                    self._get_entry(),
                    sub,
                    title=user_input[CONF_NAME],
                    data=_clean_profile(user_input),
                    unique_id=user_input[CONF_NAME].strip().casefold(),
                )
        return self.async_show_form(
            step_id="reconfigure", data_schema=_profile_schema(user_input or sub.data), errors=errors
        )
