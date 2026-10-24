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


async def test_pick_scale_none_found(hass: HomeAssistant) -> None:
    with patch("custom_components.renpho.config_flow.bluetooth.async_discovered_service_info", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pick_scale"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_devices_found"
