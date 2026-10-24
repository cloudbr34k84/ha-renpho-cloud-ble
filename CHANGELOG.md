# Changelog

All notable changes to the Renpho (Cloud + Bluetooth) Home Assistant integration are documented here.

---

## [v0.1.0] - 2026-10-24

### Added
- **Cloud body-composition-analyser sensors** (8-electrode / MorphoScan accounts): segmental fat and muscle mass for each arm, each leg and the trunk (kg, and % of standard), skeletal muscle mass/index, body score, body type, waist-hip ratio, obesity degree, target weight and weight/fat/muscle control, segment impedances (disabled by default), and healthy-range attributes. Created only for people whose records carry them.
- **Cloud status sensors** (low / normal / high against Renpho's own healthy ranges) for weight, body fat, body fat mass, muscle mass, skeletal muscle mass, bone mass, BMR, body water and protein.
- **Measurement event entity** per cloud person: fires once per new weigh-in (none on startup, catches up after downtime).
- **Scale wake-up refreshes the cloud** (checks ~1½ and 5 minutes after the Bluetooth scale wakes), so the app-synced analysis arrives within minutes.
- **Cloud goal sensors** from the account profile (weight goal, weight to goal, goal date, body fat goal, starting weight/body fat, waist, hip; created once set in the app) and **muscle mass percentage**.
- **Cloud history import** into long-term statistics (hourly mean/min/max at each measurement's own time). A one-off full backfill per sensor, then incremental; `renpho.import_history` re-imports.
- Cloud now reads and merges both record stores (classic and body composition), attributes records by `bUserId`, and skips records flagged invalid.

### Fixed
- Cloud *Heart rate* is only created for scales that measure it; existing empty heart-rate sensors are removed.
- Cloud `bone` is kg, not % (sensor replaced by *Bone mass*; the old *Bone* entity is removed). `sinew` is renamed *Muscle mass* and `muscle` *Skeletal muscle*, matching the units confirmed against a live account.
- **Cloud source**: async Renpho Health client (login, family members, latest body-composition record per person), with reauth. No PyPI requirements; encryption verified byte-identical to `renpho-api`.
- **Bluetooth source** for LeFu 0x55aa scales (company ID `0x1A10`): auto-discovery, connection via any connectable adapter or ESPHome proxy, frame decoding (`0x14` / `0x18`), optional guest-profile handshake for extended models, and a weight-plateau fallback.
- **People as subentries**: readings are matched to a person by weight, and body composition is derived from impedance (ported from `renpho-escs20m`, verified against it).
- `renpho.reassign_last_measurement` service and `renpho_measurement` event.
- Diagnostics with full GATT capture (table, readable values, subscriptions, raw notifications) to support new models.
- **MorphoScan family (R-MSC04 / R-MSC06, advert prefix `00 12`)**, experimental: guest-profile + clock handshake, status acks, live/final weight, the scale's 0x25 analysis (body fat, visceral fat, skeletal muscle, segmental impedance). Optional per-person *Scale profile byte*, with the scale's figures used only once it's verified (answered before completion, BMI matches the height). Implemented from the protocol facts documented by ble-scale-sync (#117, #434); no code copied.
- System health (cloud reachability, configured sources), icon translations (`icons.json`), privacy-safe diagnostics (names removed, MACs masked to the OUI).
- Cloud: the login request now carries the `appVersion`/`platform` headers. Renpho's gateway picks its decryption key from `appVersion`, so without them every login failed with `-114` "Decryption failed" (same fix as danvaneijck/renpho-api PR #13). The gateway's `message` text is now reported in errors.
- Cloud: HTTP 429 handling with `Retry-After` back-off; one fresh login on any failure, since token expiry has no documented code; finds the newest record whatever the server's sort order.
