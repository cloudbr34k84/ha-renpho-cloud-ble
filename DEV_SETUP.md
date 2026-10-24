# Local Development Setup

## How It Works

The repo uses the standard HACS layout, with the integration in `custom_components/renpho/`. Home Assistant loads integrations from its own `custom_components/` directory, so a symlink connects the two.

```
/homeassistant/                         (/config inside the HA container)
├── brads_dev/
│   └── ha-renpho-cloud-ble/            ← git repo (edit files here)
│       ├── custom_components/
│       │   └── renpho/                 ← actual integration source
│       ├── tests/
│       └── ...
└── custom_components/
    └── renpho -> ../brads_dev/ha-renpho-cloud-ble/custom_components/renpho
                                        ← relative symlink (HA loads from here)
```

The symlink is **relative** so it resolves both from the SSH add-on (`/homeassistant`) and inside the HA container (`/config`). An absolute `/homeassistant/...` link would break inside the container.

## Recreating the Symlink

```bash
cd /homeassistant/custom_components
ln -s ../brads_dev/ha-renpho-cloud-ble/custom_components/renpho renpho
```

## After Making Changes

| Change type | What to do |
|---|---|
| Python logic, sensors | Reload the Renpho entry (or restart HA) |
| `manifest.json`, new files | Full HA restart |
| `strings.json` / translations | Full HA restart (keep `translations/en.json` identical to `strings.json`) |

## Running the Tests

```bash
cd /homeassistant/brads_dev/ha-renpho-cloud-ble
python3 -m venv /tmp/renpho-venv
/tmp/renpho-venv/bin/pip install -r requirements_test.txt
/tmp/renpho-venv/bin/python scripts/ha_component_requirements.py > /tmp/ha-reqs.txt
/tmp/renpho-venv/bin/pip install -r /tmp/ha-reqs.txt
/tmp/renpho-venv/bin/python -m pytest -q
```

`tests/test_protocol.py` cross-checks the ported code against the upstream libraries (`renpho-escs20m`, `renpho-api`). Those are test-only dependencies; the integration itself has no requirements.

## Debug Logging

```yaml
logger:
  logs:
    custom_components.renpho: debug
```

Debug level logs every BLE notification as hex (`Renpho <mac> 2a10 <- 55aa14...`).

## Git Workflow

```bash
cd /homeassistant/brads_dev/ha-renpho-cloud-ble

git pull          # get latest from GitHub
# ... make changes ...
git add .
git commit -m "your message"
git push
```
