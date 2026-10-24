"""Diagnostics — for BLE this is the raw capture used to confirm the protocol.

Diagnostics end up in public GitHub issues, so names, account ids and full
MAC addresses are removed. The OUI (first three octets) is kept because it
identifies the scale family.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .ble_scale import RenphoBleScale
from .const import CONF_ALGORITHM, CONF_ATHLETE, CONF_TOLERANCE
from .coordinator import RenphoConfigEntry

TO_REDACT_CLOUD = {
    CONF_EMAIL, CONF_PASSWORD, "token", "accountName", "email", "mac", "sn", "serialNumber",
    "deviceId", "userId", "userIds", "bUserId", "nickName", "name",
}
TO_REDACT_BLE = {"profile"}


def _mask_mac(value: str | None) -> str | None:
    if not value or value.count(":") != 5:
        return value
    return value[:8] + ":XX:XX:XX"


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: RenphoConfigEntry) -> dict[str, Any]:
    if isinstance(entry.runtime_data, RenphoBleScale):
        scale = entry.runtime_data
        ble = async_redact_data(scale.diagnostics(), TO_REDACT_BLE)
        ble["address"] = _mask_mac(ble["address"])
        ble["source"] = _mask_mac(ble["source"])
        return {
            "options": dict(entry.options),
            # Matching settings only — no names, birthdays or heights
            "profiles": {
                sid: {k: sub.data.get(k) for k in (CONF_TOLERANCE, CONF_ALGORITHM, CONF_ATHLETE)}
                for sid, sub in entry.subentries.items()
            },
            "ble": ble,
            "profile_data": scale.profiles,
        }
    coordinator = entry.runtime_data
    data = coordinator.data or {}
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT_CLOUD),
        "options": dict(entry.options),
        "last_update_success": coordinator.last_update_success,
        "last_exception": repr(coordinator.last_exception) if coordinator.last_exception else None,
        "user_count": len(data.get("users", {})),
        "scales": async_redact_data(data.get("scales", []), TO_REDACT_CLOUD),
        "latest_keys": [sorted(r) for r in data.get("latest", {}).values()],
    }
