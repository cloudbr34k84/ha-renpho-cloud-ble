"""Actions stay registered and report invalid entry targets clearly."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import ServiceValidationError

from custom_components.renpho import async_setup
from custom_components.renpho.const import DOMAIN


@pytest.mark.parametrize("action,fields", [
    ("reassign_last_measurement", {"profile_id": "person"}),
    ("import_history", {}),
])
@pytest.mark.parametrize("target", ["missing", "foreign", "unloaded"])
async def test_invalid_action_entry(hass, action, fields, target):
    """Missing, foreign and unloaded entries fail before touching runtime data."""
    entry = MockConfigEntry(domain="other" if target == "foreign" else DOMAIN,
                            data={"source": "ble" if action.startswith("reassign") else "cloud"})
    if target != "missing":
        entry.add_to_hass(hass)
    await async_setup(hass, {})
    assert hass.services.has_service(DOMAIN, action)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, action,
            {"config_entry_id": entry.entry_id, **fields}, blocking=True)
