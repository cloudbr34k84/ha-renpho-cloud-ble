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

## Quality scale checks

The assessment is in `custom_components/renpho/quality_scale.yaml`. Run the
flow coverage gate with:

```bash
python -m pytest tests/test_config_flow.py --cov=custom_components.renpho.config_flow --cov-branch --cov-report=term-missing --cov-fail-under=100
```

Then run `python -m pytest -q` for the full integration suite. The manifest
must not claim a tier while any Bronze rule is `todo`. Local HACS brand
assets are included. Upstream Renpho icons provide the logo fallback required
when the icon and logo are identical; see the brands repository README.


Silver requires more than 95% statement coverage in **every** integration
module. This repository enforces the stronger target of **100% statement
coverage in every module**:

```bash
python -m pytest --cov=custom_components.renpho --cov-report=term-missing --cov-report=json:coverage.json --cov-fail-under=100
python scripts/check_module_coverage.py coverage.json
```

CI runs both the 100% config-flow statement/branch gate and the full-integration
100% statement gate. The module checker rejects any missing module or uncovered
statement, so rounding cannot hide a gap.


## Platinum strict typing

Install the pinned checker from `requirements_test.txt`, then run from the
repository root:

```bash
python -m mypy
```

`pyproject.toml` enables strict mode for every module in
`custom_components/renpho`. It reads dependency annotations without checking
third-party implementation bodies (`follow_imports = "silent"`); it does not
ignore missing imports, suppress integration errors, or disable strict checks.
`.strict-typing` records this repository's custom-component namespace, and
`custom_components/renpho/py.typed` declares the bundled clients typed. Core's
own `.strict-typing` file is maintained in the Home Assistant Core repository;
this standalone integration enforces the equivalent check in its own CI.

Normalized cloud data uses `CloudData`; variable vendor JSON fields retain
explicit `Any` values at the API boundary. Config entries, coordinator results,
BLE tasks, callbacks, options, selectors and storage containers are annotated.
The cloud client receives Home Assistant's shared aiohttp session rather than
creating or owning another connection pool.
