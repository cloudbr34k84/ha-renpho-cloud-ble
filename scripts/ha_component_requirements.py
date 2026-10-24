"""Print the requirements of the HA components the tests load, as pinned by the installed HA.

pytest-homeassistant-custom-component installs Home Assistant core but not the
requirements of individual components; the Bluetooth stack (and USB, which
Bluetooth imports) has to be added with the exact versions this HA release pins.
"""

import json
from pathlib import Path

import homeassistant

COMPONENTS = ("bluetooth", "bluetooth_adapters", "usb", "recorder")

base = Path(homeassistant.__file__).parent / "components"
requirements: list[str] = []
for component in COMPONENTS:
    for req in json.loads((base / component / "manifest.json").read_text()).get("requirements", []):
        if req not in requirements:
            requirements.append(req)
print("\n".join(requirements))
