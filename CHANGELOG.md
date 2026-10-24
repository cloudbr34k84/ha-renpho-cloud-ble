# Changelog

All notable changes to the Renpho (Cloud + Bluetooth) Home Assistant integration are documented here.

---

## [v0.1.0] - 2026-10-24

First release: one integration for Renpho scales with two independent sources, the Renpho Health cloud and local Bluetooth.

### Added

**Cloud (Renpho Health account)**
- Async Renpho Health client with no extra Python requirements. Encryption is verified byte-identical to `renpho-api`, and the login sends the `appVersion` header Renpho's gateway now needs, avoiding the `-114` "Decryption failed" error other clients hit.
- Reads and merges both record stores (classic 4-electrode and 8-electrode body composition). Records are attributed to their owner, invalid ones are skipped, and family members are named from current app fields.
- Sensors per person: weight, BMI, body fat, body water, skeletal muscle, muscle mass, bone mass, BMR, visceral and subcutaneous fat, protein, body age, fat-free mass and last measurement. Healthy-range attributes where Renpho provides them.
- Body-composition-analyser sensors (8-electrode, e.g. R-MSC04/R-MSC06): fat and muscle mass for each arm, each leg and the trunk (kg, and % of standard), skeletal muscle mass and index, body score, body type, waist-hip ratio, obesity degree, target weight, weight/fat/muscle control, muscle mass percentage, and segment impedances (disabled by default).
- Status sensors (low / normal / high against Renpho's healthy ranges) for weight, body fat, body fat mass, muscle mass, skeletal muscle mass, bone mass, BMR, body water and protein.
- Goal sensors from the account profile: weight goal, weight to goal, goal date, body fat goal, starting weight and body fat, waist and hip. Each appears once set in the Renpho app.
- A Measurement event entity per person that fires once for every new weigh-in, catching up on any made while Home Assistant was down.
- History import into long-term statistics at each measurement's own time: a full backfill once, then incremental. `renpho.import_history` re-imports everything.
- Reauthentication, HTTP 429 back-off, and one fresh login on any failure. Heart rate appears only for scales that measure it.

**Bluetooth (local)**
- Auto-discovery of LeFu-based scales (company ID `0x1A10`), connecting through any connectable adapter or ESPHome Bluetooth proxy.
- 0x55aa family (ES-CS20M, R-A01x, ES-26BB-B): weight and impedance, with body composition derived from the person's profile (ported from `renpho-escs20m` and verified against it), plus the guest-profile handshake for extended models.
- MorphoScan family (R-MSC04 / R-MSC06), experimental: guest-profile and clock handshake, live and final weight, and the scale's own analysis (body fat, visceral fat, skeletal muscle, segmental impedance). An optional per-person *Scale profile byte* lets the scale's figures be used once they're verified. Implemented from the protocol facts documented by ble-scale-sync (#117, #434); no code copied.
- People as subentries, matched by weight, with `renpho.reassign_last_measurement` and a `renpho_measurement` event.
- Waking the scale triggers cloud checks about 1½ and 5 minutes later, so the app-synced analysis arrives within minutes.

**General**
- Diagnostics with a full GATT capture for adding new models; names are removed and MACs masked to the manufacturer prefix.
- System health, icon translations, brand icons, and HACS packaging.
