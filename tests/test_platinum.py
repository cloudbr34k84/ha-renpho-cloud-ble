"""Platinum session ownership and typed-boundary regressions."""

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .test_ble import setup_scale
from .test_silver import setup_cloud


async def test_cloud_client_reuses_home_assistant_session(hass):
    session = async_get_clientsession(hass)
    entry = await setup_cloud(hass)
    assert entry.runtime_data.api._session is session
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not session.closed  # the shared pool belongs to Home Assistant


async def test_malformed_stored_timestamp_does_not_discard_new_reading(hass, adv_callbacks):
    from custom_components.renpho import ble_protocol as proto
    from .test_ble import PERSON
    entry = await setup_scale(hass)
    scale = entry.runtime_data
    scale.scale.update(weight=82.4, measured_at="invalid timestamp")
    scale._final = proto.Reading(82.4, 500, final=True, zero_current=False)
    assert scale._finish_session()
    assert scale.scale["measured_at"] != "invalid timestamp"
    assert float(hass.states.get(f"{PERSON}_weight").state) == 82.4
