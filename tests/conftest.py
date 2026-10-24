"""Shared fixtures for the Renpho integration tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def adv_callbacks():
    """Capture bluetooth advertisement callbacks registered by the scale."""
    callbacks = []

    def _register(hass, cb, matcher, mode):
        callbacks.append(cb)
        return lambda: callbacks.remove(cb)

    with patch("custom_components.renpho.ble_scale.bluetooth.async_register_callback", _register):
        yield callbacks
