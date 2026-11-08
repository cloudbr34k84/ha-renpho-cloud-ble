"""Config flows: menu, cloud login, bluetooth discovery and manual pick."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.renpho.cloud_api import RenphoAuthError, RenphoCloud, RenphoCloudError
from custom_components.renpho.const import DOMAIN

from .test_ble import MAC, service_info


@pytest.fixture(autouse=True)
def no_setup():
    with patch("custom_components.renpho.async_setup_entry", return_value=True):
        yield


async def test_menu(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["cloud", "pick_scale"]


async def test_cloud_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "cloud"})
    assert result["step_id"] == "cloud"

    async def login(self):
        self.user_id, self.token = "1001", "t"

    with patch.object(RenphoCloud, "login", AsyncMock(side_effect=RenphoAuthError("bad"))):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "a@b.c", "password": "x"})
    assert result["errors"] == {"base": "invalid_auth"}
    with patch.object(RenphoCloud, "login", AsyncMock(side_effect=RenphoCloudError("down"))):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "a@b.c", "password": "x"})
    assert result["errors"] == {"base": "cannot_connect"}
    with patch.object(RenphoCloud, "login", login):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "a@b.c", "password": "pw"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"source": "cloud", "email": "a@b.c", "password": "pw"}
    assert result["result"].unique_id == "cloud_1001"


async def test_bluetooth_discovery(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info())
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "bluetooth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Renpho R-MSC06 ({MAC})"
    assert result["data"] == {"source": "ble", "address": MAC, "model_id": 0x0041}
    assert result["result"].unique_id == MAC
    # a second discovery of the same scale aborts
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info())
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_bluetooth_discovery_rejects_foreign_mac(hass: HomeAssistant) -> None:
    info = service_info()
    info.manufacturer_data = {0x1A10: bytes.fromhex("00120041aabbccddeeff0101")}
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=info)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "not_supported"


async def test_pick_scale(hass: HomeAssistant) -> None:
    with patch("custom_components.renpho.config_flow.bluetooth.async_discovered_service_info", return_value=[service_info()]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pick_scale"})
        assert result["step_id"] == "pick_scale"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"address": MAC})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["model_id"] == 0x0041
    assert result["result"].unique_id == MAC


async def test_pick_scale_none_found(hass: HomeAssistant) -> None:
    with patch("custom_components.renpho.config_flow.bluetooth.async_discovered_service_info", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pick_scale"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_devices_found"


async def test_cloud_duplicate(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain=DOMAIN, unique_id="cloud_1001", data={"source": "cloud"})
    entry.add_to_hass(hass)

    async def login(self):
        self.user_id = "1001"

    with patch.object(RenphoCloud, "login", login):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "cloud"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "a@b.c", "password": "pw"})
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize("source", ["cloud", "ble"])
async def test_options(hass, source):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain=DOMAIN, data={"source": source})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    options = {} if source == "cloud" else {"allow_writes": "never", "capture_all_characteristics": False}
    result = await hass.config_entries.options.async_configure(result["flow_id"], options)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == options


@pytest.mark.parametrize("exception,error", [(RenphoAuthError("bad"), "invalid_auth"), (RenphoCloudError("offline"), "cannot_connect")])
async def test_reauth_recovery(hass, exception, error):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain=DOMAIN, unique_id="cloud_1001", data={"source": "cloud", "email": "a@b.c", "password": "old"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data)
    with patch.object(RenphoCloud, "login", AsyncMock(side_effect=exception)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "new"})
    assert result["errors"] == {"base": error}

    async def login(self):
        self.user_id = "1001"

    with patch.object(RenphoCloud, "login", login), patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "new"})
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new"
    assert entry.unique_id == "cloud_1001"
    reload.assert_called_once_with(entry.entry_id)


async def test_profile_flows(hass):
    """Exercise profile validation, duplicate names, cleaning and reconfiguration."""
    from types import SimpleNamespace
    from custom_components.renpho.config_flow import ProfileSubentryFlow, RenphoConfigFlow

    profile = {"name": "Alex", "sex": "male", "birthday": "1990-01-01", "height_cm": 180,
               "start_weight": 80, "tolerance": 3, "algorithm": "0x04", "athlete": False,
               "scale_profile_byte": " a9 "}
    sub = SimpleNamespace(subentry_id="person", unique_id="alex", title="Alex", data=profile)
    entry = SimpleNamespace(data={"source": "ble"}, subentries={"person": sub})
    assert RenphoConfigFlow.async_get_supported_subentry_types(entry) == {"profile": ProfileSubentryFlow}
    assert RenphoConfigFlow.async_get_supported_subentry_types(SimpleNamespace(data={"source": "cloud"})) == {}
    flow = ProfileSubentryFlow()
    flow.hass = hass
    flow.context = {"source": SOURCE_USER}
    with patch.object(flow, "_get_entry", return_value=entry):
        result = await flow.async_step_user()
        assert result["type"] is FlowResultType.FORM
        result = await flow.async_step_user({**profile, "scale_profile_byte": "invalid"})
        assert result["errors"] == {"name": "name_exists", "scale_profile_byte": "invalid_profile_byte"}
        entry.subentries = {}
        result = await flow.async_step_user(profile)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["unique_id"] == "alex"
        assert result["data"]["scale_profile_byte"] == "A9"
        result = await flow.async_step_user({**profile, "scale_profile_byte": ""})
        assert result["unique_id"] == "alex"
        assert "scale_profile_byte" not in result["data"]

        other = SimpleNamespace(subentry_id="other", unique_id="sam", title="Sam")
        entry.subentries = {"person": sub, "other": other}
        with patch.object(flow, "_get_reconfigure_subentry", return_value=sub):
            result = await flow.async_step_reconfigure()
            assert result["type"] is FlowResultType.FORM
            result = await flow.async_step_reconfigure({**profile, "name": "Sam", "scale_profile_byte": "oops"})
            assert result["errors"] == {"name": "name_exists", "scale_profile_byte": "invalid_profile_byte"}
            with patch.object(flow, "async_update_and_abort", return_value={"type": FlowResultType.ABORT}) as update:
                result = await flow.async_step_reconfigure(profile)
            assert result["type"] is FlowResultType.ABORT
            assert update.call_args.kwargs["unique_id"] == "alex"
            assert update.call_args.kwargs["data"]["scale_profile_byte"] == "A9"


async def test_reauth_wrong_account(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain=DOMAIN, unique_id="cloud_1001", data={"source": "cloud", "email": "a@b.c", "password": "old"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data)

    async def login(self):
        self.user_id = "different"

    with patch.object(RenphoCloud, "login", login):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "new"})
    assert result["reason"] == "wrong_account"
    assert entry.data["password"] == "old"


async def test_pick_ignores_unusable_advertisements(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data={"source": "ble", "address": MAC})
    entry.add_to_hass(hass)
    absent = service_info()
    absent.address = "11:22:33:44:55:66"
    absent.manufacturer_data = {}
    invalid = service_info()
    invalid.address = "22:33:44:55:66:77"
    invalid.manufacturer_data = {0x1A10: b"invalid"}
    with patch("custom_components.renpho.config_flow.bluetooth.async_discovered_service_info", return_value=[absent, service_info(), invalid]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pick_scale"})
    assert result["reason"] == "no_devices_found"


async def test_loaded_entry_reauth_uses_update_listener(hass):
    from homeassistant.config_entries import ConfigEntryState
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain=DOMAIN, unique_id="cloud_1001", data={"source": "cloud", "email": "a@b.c", "password": "old"})
    entry.add_to_hass(hass)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data)

    async def login(self):
        self.user_id = "1001"

    with patch.object(RenphoCloud, "login", login), patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "new"})
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == "cloud_1001"
    reload.assert_not_called()


async def test_rediscovery_updates_model_information(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data={"source": "ble", "address": MAC, "model_id": 3})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info())
    assert result["reason"] == "already_configured"
    assert entry.data["model_id"] == 0x0041
    assert entry.unique_id == MAC


async def test_manual_setup_finishes_pending_discovery(hass):
    await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info())
    with patch("custom_components.renpho.config_flow.bluetooth.async_discovered_service_info", return_value=[service_info()]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pick_scale"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"address": MAC})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == MAC


@pytest.mark.parametrize("exception,error", [(RenphoAuthError("bad"), "invalid_auth"), (RenphoCloudError("offline"), "cannot_connect")])
async def test_reauth_updates_changed_email_for_same_account(hass, exception, error):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain=DOMAIN, unique_id="cloud_1001", data={"source": "cloud", "email": "old@b.c", "password": "old"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data)

    with patch.object(RenphoCloud, "login", AsyncMock(side_effect=exception)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "new@b.c", "password": "new"})
    assert result["errors"] == {"base": error}
    assert result["data_schema"]({"password": "new"})["email"] == "new@b.c"

    async def login(self):
        assert self._email == "new@b.c"
        self.user_id = "1001"

    with patch.object(RenphoCloud, "login", login), patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "new"})
    assert result["reason"] == "reauth_successful"
    assert entry.data["email"] == "new@b.c"
    assert entry.unique_id == "cloud_1001"
    reload.assert_called_once_with(entry.entry_id)
