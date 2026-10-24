"""Constants for the Renpho integration (cloud + local BLE)."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "renpho"
PLATFORMS: Final = ["event", "sensor"]

# Entry types
CONF_SOURCE: Final = "source"
SOURCE_CLOUD: Final = "cloud"
SOURCE_BLE: Final = "ble"

# Cloud
CONF_SCAN_INTERVAL: Final = "scan_interval"
DEFAULT_SCAN_INTERVAL_MIN: Final = 30
MIN_SCAN_INTERVAL_MIN: Final = 5

# BLE entry
CONF_ADDRESS: Final = "address"
CONF_MODEL_ID: Final = "model_id"
CONF_ALLOW_WRITES: Final = "allow_writes"
CONF_CAPTURE: Final = "capture_all_characteristics"

# Profile subentry
SUBENTRY_PROFILE: Final = "profile"
CONF_NAME: Final = "name"
CONF_SEX: Final = "sex"
CONF_BIRTHDAY: Final = "birthday"
CONF_HEIGHT_CM: Final = "height_cm"
CONF_START_WEIGHT: Final = "start_weight"
CONF_TOLERANCE: Final = "tolerance"
CONF_ATHLETE: Final = "athlete"
CONF_ALGORITHM: Final = "algorithm"
CONF_PROFILE_BYTE: Final = "scale_profile_byte"
SEX_MALE: Final = "male"
SEX_FEMALE: Final = "female"
DEFAULT_TOLERANCE_KG: Final = 3.0
ALGO_DEFAULT: Final = "0x04"
ALGO_ALT: Final = "0x03"

# Dispatcher signal, formatted with the entry id
SIGNAL_BLE_UPDATE: Final = "renpho_ble_update_{}"
