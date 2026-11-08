# Final integration quality audit

Reference: [All rules.md](../ha-integration-dev/HA%20Core%20and%20integrations/HA%20Integration%20quality%20scale/All%20rules.md).

Scope: the standalone HACS integration. README supplies user documentation; repository CI supplies strict typing and coverage checks.

**54 rules accounted for: 51 done, 3 exempt.** The exemptions are custom conditions, custom device triggers, and entry reconfiguration. Their reasons are recorded below and in `quality_scale.yaml`.

## Repairs from this audit

- Reauthentication now accepts a changed email for the same account, preserves the submitted email after validation errors, and keeps account/entity identity.
- Family endpoint authentication and rate-limit failures reach the coordinator rather than being hidden as an empty optional result.
- Invalid HTTP JSON, invalid encrypted responses and malformed login objects become recoverable cloud errors.
- Bluetooth action targets are checked for valid runtime data before accessing measurements.
- Checklist coverage evidence no longer contains obsolete test or statement counts.

## Validation

- Full suite: **164 passed**; **1,748/1,748 statements covered (100%)**, with 100% in each of 14 modules.
- Config flow: **100% statement and branch coverage**.
- Strict mypy: **no issues in 14 modules**.
- Per-module coverage checker, translation parity, README YAML, exact 54-rule membership and `git diff --check`: **passed**.
- Validation environment: Home Assistant **2026.10.4**, mypy **2.4.1**.

```bash
python -m mypy
python -m pytest tests/test_config_flow.py --cov=custom_components.renpho.config_flow --cov-branch --cov-fail-under=100
python -m pytest --cov=custom_components.renpho --cov-report=term-missing --cov-report=json:coverage.json --cov-fail-under=100
python scripts/check_module_coverage.py coverage.json
git diff --check
```

## Bronze

| Rule | Status | Code, tests or documentation |
|---|---|---|
| `action-setup` | done | [__init__.py](custom_components/renpho/__init__.py), [tests/test_actions.py](tests/test_actions.py), [tests/test_silver.py](tests/test_silver.py) |
| `appropriate-polling` | done | [coordinator.py](custom_components/renpho/coordinator.py), [README.md#data-updates](README.md#data-updates) |
| `brands` | done | [brand/icon.png](custom_components/renpho/brand/icon.png), [brand/logo.png](custom_components/renpho/brand/logo.png), [DEV_SETUP.md#quality-scale-checks](DEV_SETUP.md#quality-scale-checks) |
| `common-modules` | done | [coordinator.py](custom_components/renpho/coordinator.py), [entity.py](custom_components/renpho/entity.py) |
| `config-flow` | done | [config_flow.py](custom_components/renpho/config_flow.py), [strings.json](custom_components/renpho/strings.json), [tests/test_config_flow.py](tests/test_config_flow.py) |
| `config-flow-test-coverage` | done | [tests/test_config_flow.py](tests/test_config_flow.py), [.github/workflows/tests.yaml](.github/workflows/tests.yaml) |
| `dependency-transparency` | done | [manifest.json](custom_components/renpho/manifest.json), [NOTICE](NOTICE), [LICENSE](LICENSE) |
| `docs-actions` | done | [README.md#action-reference](README.md#action-reference), [services.yaml](custom_components/renpho/services.yaml) |
| `docs-conditions` | exempt | [README.md#action-reference](README.md#action-reference) |
| `docs-high-level-description` | done | [README.md](README.md) |
| `docs-installation-instructions` | done | [README.md#installation](README.md#installation), [README.md#setup](README.md#setup) |
| `docs-removal-instructions` | done | [README.md#removing-the-integration](README.md#removing-the-integration) |
| `docs-triggers` | exempt | [README.md#entities](README.md#entities), [README.md#use-cases-and-automation-examples](README.md#use-cases-and-automation-examples) |
| `entity-event-setup` | done | [entity.py](custom_components/renpho/entity.py), [tests/test_silver.py](tests/test_silver.py) |
| `entity-unique-id` | done | [entity.py](custom_components/renpho/entity.py), [sensor.py](custom_components/renpho/sensor.py), [event.py](custom_components/renpho/event.py), [tests/test_cloud.py](tests/test_cloud.py) |
| `has-entity-name` | done | [entity.py](custom_components/renpho/entity.py), [sensor.py](custom_components/renpho/sensor.py), [event.py](custom_components/renpho/event.py) |
| `runtime-data` | done | [coordinator.py](custom_components/renpho/coordinator.py), [__init__.py](custom_components/renpho/__init__.py) |
| `test-before-configure` | done | [config_flow.py](custom_components/renpho/config_flow.py), [cloud_api.py](custom_components/renpho/cloud_api.py), [tests/test_config_flow.py](tests/test_config_flow.py) |
| `test-before-setup` | done | [__init__.py](custom_components/renpho/__init__.py), [coordinator.py](custom_components/renpho/coordinator.py), [tests/test_cloud.py](tests/test_cloud.py) |
| `unique-config-entry` | done | [config_flow.py](custom_components/renpho/config_flow.py), [tests/test_config_flow.py](tests/test_config_flow.py) |

## Silver

| Rule | Status | Code, tests or documentation |
|---|---|---|
| `action-exceptions` | done | [__init__.py](custom_components/renpho/__init__.py), [tests/test_silver.py](tests/test_silver.py) |
| `config-entry-unloading` | done | [__init__.py](custom_components/renpho/__init__.py), [ble_scale.py](custom_components/renpho/ble_scale.py), [tests/test_silver.py](tests/test_silver.py) |
| `docs-configuration-parameters` | done | [README.md#setup-fields-and-options](README.md#setup-fields-and-options) |
| `docs-installation-parameters` | done | [README.md#setup-fields-and-options](README.md#setup-fields-and-options) |
| `entity-unavailable` | done | [entity.py](custom_components/renpho/entity.py), [coordinator.py](custom_components/renpho/coordinator.py), [tests/test_silver.py](tests/test_silver.py) |
| `integration-owner` | done | [manifest.json](custom_components/renpho/manifest.json), [.github/CODEOWNERS](.github/CODEOWNERS) |
| `log-when-unavailable` | done | [ble_scale.py](custom_components/renpho/ble_scale.py), [coordinator.py](custom_components/renpho/coordinator.py), [tests/test_silver.py](tests/test_silver.py) |
| `parallel-updates` | done | [sensor.py](custom_components/renpho/sensor.py), [event.py](custom_components/renpho/event.py) |
| `reauthentication-flow` | done | [config_flow.py](custom_components/renpho/config_flow.py), [tests/test_config_flow.py](tests/test_config_flow.py), [tests/test_cloud.py](tests/test_cloud.py) |
| `test-coverage` | done | [.github/workflows/tests.yaml](.github/workflows/tests.yaml), [scripts/check_module_coverage.py](scripts/check_module_coverage.py) |

## Gold

| Rule | Status | Code, tests or documentation |
|---|---|---|
| `devices` | done | [entity.py](custom_components/renpho/entity.py), [ble_scale.py](custom_components/renpho/ble_scale.py), [tests/test_gold.py](tests/test_gold.py) |
| `diagnostics` | done | [diagnostics.py](custom_components/renpho/diagnostics.py), [tests/test_gold.py](tests/test_gold.py), [tests/test_morphoscan.py](tests/test_morphoscan.py) |
| `discovery` | done | [manifest.json](custom_components/renpho/manifest.json), [config_flow.py](custom_components/renpho/config_flow.py), [tests/test_config_flow.py](tests/test_config_flow.py) |
| `discovery-update-info` | done | [config_flow.py](custom_components/renpho/config_flow.py), [ble_scale.py](custom_components/renpho/ble_scale.py), [tests/test_gold.py](tests/test_gold.py) |
| `docs-data-update` | done | [README.md#data-updates](README.md#data-updates) |
| `docs-examples` | done | [README.md#use-cases-and-automation-examples](README.md#use-cases-and-automation-examples) |
| `docs-known-limitations` | done | [README.md#known-limitations](README.md#known-limitations) |
| `docs-supported-devices` | done | [README.md#status](README.md#status), [README.md#supported-and-unsupported-devices](README.md#supported-and-unsupported-devices) |
| `docs-supported-functions` | done | [README.md#entities](README.md#entities), [README.md#action-reference](README.md#action-reference) |
| `docs-troubleshooting` | done | [README.md#troubleshooting](README.md#troubleshooting), [README.md#recovery](README.md#recovery) |
| `docs-use-cases` | done | [README.md#use-cases-and-automation-examples](README.md#use-cases-and-automation-examples) |
| `dynamic-devices` | done | [sensor.py](custom_components/renpho/sensor.py), [event.py](custom_components/renpho/event.py), [entity.py](custom_components/renpho/entity.py), [tests/test_gold.py](tests/test_gold.py) |
| `entity-category` | done | [sensor.py](custom_components/renpho/sensor.py), [tests/test_gold.py](tests/test_gold.py) |
| `entity-device-class` | done | [sensor.py](custom_components/renpho/sensor.py), [tests/test_gold.py](tests/test_gold.py) |
| `entity-disabled-by-default` | done | [sensor.py](custom_components/renpho/sensor.py), [tests/test_gold.py](tests/test_gold.py), [tests/test_coverage_edges.py](tests/test_coverage_edges.py) |
| `entity-translations` | done | [strings.json](custom_components/renpho/strings.json), [sensor.py](custom_components/renpho/sensor.py), [event.py](custom_components/renpho/event.py), [tests/test_gold.py](tests/test_gold.py) |
| `exception-translations` | done | [strings.json](custom_components/renpho/strings.json), [__init__.py](custom_components/renpho/__init__.py), [coordinator.py](custom_components/renpho/coordinator.py), [tests/test_silver.py](tests/test_silver.py) |
| `icon-translations` | done | [icons.json](custom_components/renpho/icons.json), [tests/test_gold.py](tests/test_gold.py) |
| `reconfiguration-flow` | exempt | [config_flow.py](custom_components/renpho/config_flow.py), [README.md#device-changes-and-repairs](README.md#device-changes-and-repairs) |
| `repair-issues` | done | [ble_scale.py](custom_components/renpho/ble_scale.py), [__init__.py](custom_components/renpho/__init__.py), [strings.json](custom_components/renpho/strings.json), [tests/test_gold.py](tests/test_gold.py) |
| `stale-devices` | done | [__init__.py](custom_components/renpho/__init__.py), [entity.py](custom_components/renpho/entity.py), [tests/test_gold.py](tests/test_gold.py) |

## Platinum

| Rule | Status | Code, tests or documentation |
|---|---|---|
| `async-dependency` | done | [cloud_api.py](custom_components/renpho/cloud_api.py), [ble_scale.py](custom_components/renpho/ble_scale.py), [history.py](custom_components/renpho/history.py) |
| `inject-websession` | done | [cloud_api.py](custom_components/renpho/cloud_api.py), [config_flow.py](custom_components/renpho/config_flow.py), [coordinator.py](custom_components/renpho/coordinator.py), [tests/test_platinum.py](tests/test_platinum.py) |
| `strict-typing` | done | [pyproject.toml](pyproject.toml), [.strict-typing](.strict-typing), [py.typed](custom_components/renpho/py.typed), [.github/workflows/tests.yaml](.github/workflows/tests.yaml) |

## Exemption rationale

- `docs-conditions`: The integration provides no custom conditions or device triggers; measurement events are documented in README.
- `docs-triggers`: The integration provides no custom conditions or device triggers; measurement events are documented in README.
- `reconfiguration-flow`: Cloud setup contains only account credentials handled by reauthentication. Changing the stable Bluetooth hardware identity requires a new entry. Person subentries already support reconfiguration.

## Integration lifecycle and evidence limits

- Bluetooth devices use stable MAC identities; rediscovery refreshes model information and resolves adapter/proxy routing at runtime. There is no mutable host/IP setting.
- Cloud inventory is not guaranteed complete, so stale-device deletion is manual and protected during failed updates. Returning people recreate their entities.
- Existing user registry enable/disable choices are retained. New RSSI and impedance diagnostics default to disabled.
- The local assessment does not add this custom integration to the Core repository. Core-specific generated files and documentation are represented by equivalent checks and documentation here.
- The repository retains HACS and Hassfest workflows. This local audit records locally executed checks; hosted workflow results become available when the changes are pushed.
- Protocol and simulated-device test coverage does not change the hardware verification levels documented in README.
