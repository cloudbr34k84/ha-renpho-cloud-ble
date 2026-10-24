[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Tests](https://github.com/cloudbr34k84/ha-renpho-cloud-ble/actions/workflows/tests.yaml/badge.svg)](https://github.com/cloudbr34k84/ha-renpho-cloud-ble/actions/workflows/tests.yaml)
[![Validate with hassfest](https://github.com/cloudbr34k84/ha-renpho-cloud-ble/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/cloudbr34k84/ha-renpho-cloud-ble/actions/workflows/hassfest.yaml)
[![Validate with HACS](https://github.com/cloudbr34k84/ha-renpho-cloud-ble/actions/workflows/validate.yaml/badge.svg)](https://github.com/cloudbr34k84/ha-renpho-cloud-ble/actions/workflows/validate.yaml)

# Renpho for Home Assistant — Cloud + Bluetooth

<p align="center">
  <img src="https://raw.githubusercontent.com/cloudbr34k84/ha-renpho-cloud-ble/main/custom_components/renpho/brand/icon@2x.png" alt="Renpho Cloud + Bluetooth for Home Assistant" width="200">
</p>

One integration for Renpho body-composition scales, with two independent ways to get your data in:

| Source | How it works | Works with |
|---|---|---|
| **Cloud** | Polls your Renpho Health account for each family member's latest measurement | Any scale that syncs to the Renpho Health app |
| **Bluetooth (local)** | Connects to the scale when you step on it — directly or through an ESPHome Bluetooth proxy — and decodes the measurement | LeFu-based scales (Bluetooth company ID `0x1A10`): the 0x55aa family and the MorphoScan family (R-MSC04, R-MSC06) |

You can add either or both. They are separate config entries with separate devices, so nothing is double-counted.

No extra Python packages are installed: the cloud client uses Home Assistant's built-in `aiohttp` and `cryptography`, and the Bluetooth protocol is implemented in the integration itself.

## Status

| | |
|---|---|
| Cloud | Ported from [ha-renpho-health](https://github.com/DiscountDarcy/ha-renpho-health) / [renpho-api](https://pypi.org/project/renpho-api/). The request encryption is byte-identical to `renpho-api` (checked in the test suite). |
| Bluetooth, known 0x55aa models (ES-CS20MB1, ES-CS20MB2, R-A012, R-A016, ES-26BB-B) | Frame decoding, guest-profile handshake and body-composition math ported from [renpho-escs20m](https://pypi.org/project/renpho-escs20m/) and checked against it in the test suite. Not yet tested on hardware by this project. |
| Bluetooth, **MorphoScan family (R-MSC04, R-MSC06)** | **Experimental.** Our own implementation of the protocol that the [ble-scale-sync](https://github.com/KristianP26/ble-scale-sync) project documented from real R-MSC04 captures (issues [#117](https://github.com/KristianP26/ble-scale-sync/issues/117) and [#434](https://github.com/KristianP26/ble-scale-sync/issues/434)). It is tested against those captured frames. The R-MSC06 advertises the same `00 12` family prefix as the R-MSC04, with model `0x0041` instead of `0x0035`, but no R-MSC06 weigh-in has been decoded yet. |

If your scale isn't decoded locally yet, use the cloud source. It works today for anything the Renpho app supports.

## Installation

**HACS (custom repository):** HACS → ⋮ → *Custom repositories* → add `https://github.com/cloudbr34k84/ha-renpho-cloud-ble` as an *Integration*, install **Renpho (Cloud + Bluetooth)**, then restart Home Assistant.

**Manual:** copy `custom_components/renpho` into your `config/custom_components/` and restart.

Requires Home Assistant 2026.10 or newer.

## Setup

### Cloud

*Settings → Devices & services → Add integration → Renpho → Renpho Health account (cloud)* and enter your Renpho Health email and password. One device appears per family member on the account (`Renpho Cloud <name>`). The polling interval defaults to 30 minutes (minimum 5) and can be changed under *Configure*.

Accounts created before 2024 may need migrating to the Renpho Health app first. Very short polling intervals can hit Renpho's API rate limits.

### Bluetooth

Step on the scale to wake it. Home Assistant discovers it automatically ("Renpho R-MSC06 (60:65:…)"), or you can add it through *Add integration → Renpho → Bluetooth scale (local)*.

Then add each person who uses the scale: open the scale's entry and choose **Add person**. Enter their name, sex, birthday, height, current weight and a matching tolerance. Each person gets their own device (`Renpho Scale <name>`).

Requirements and tips:

- An adapter or proxy that can make **connections**. ESPHome proxies need `bluetooth_proxy: active: true`.
- The scale accepts one Bluetooth connection at a time. If the Renpho app is open and connected, Home Assistant can't connect, and the reverse is also true.

#### How a reading is assigned to a person

The scale doesn't know who is standing on it. Each reading goes to the person whose **last weight** (or starting weight, before their first reading) is closest, provided it's within their tolerance (default ±3 kg). Readings that match nobody update only the scale's own *Last weight* sensor and show *Last person: unassigned*.

If the wrong person was picked, call `renpho.reassign_last_measurement`. It moves the reading and restores the other person's previous one.

Body composition depends on the scale family:

- **0x55aa family:** calculated from the measured impedance and the person's profile, using the same formulas as the scale firmware (algorithm 0x04 by default, 0x03 optional, plus athlete mode). Readings without impedance, such as shoes on or a weight-only fallback, update weight and BMI only.
- **MorphoScan family:** the scale analyses the reading itself and sends body fat, visceral fat, skeletal muscle and five-segment impedances. Water, bone, muscle mass, protein and BMR are then derived from that body fat. See below for when the scale's figures are used.

#### MorphoScan scales (R-MSC04, R-MSC06)

On every connection Home Assistant writes a temporary **guest profile** (slot 9, which the scale does not keep as a user) and the **clock**, and acknowledges the scale's status messages. Without that handshake these scales hang up within a few seconds. The weight arrives as soon as it settles. The scale's own analysis follows 10–20 s later, so stay on until the display finishes.

The guest profile includes one byte that hasn't been decoded (probably sex and age). Without the byte the Renpho app writes for that person, the scale's body fat may be computed for the wrong sex or age, so **by default only weight and BMI are recorded locally.** To get the scale's body fat, visceral fat and skeletal muscle, set the person's **Scale profile byte**. It is the 12th byte of the `55 aa b2 …` frame the Renpho app writes to characteristic 0x2A11. To find it, capture a weigh-in made with the Renpho app:

- **Android:** use *Developer options → Bluetooth HCI snoop log*, then open `btsnoop_hci.log` in Wireshark.
- **iPhone:** use PacketLogger on a Mac.

The scale's figures are only used if three checks pass: it answered that person's profile before finishing, and the BMI it reports matches their configured height. Otherwise the reading falls back to weight and BMI. With several people, Home Assistant first sends the likeliest person's profile. If the settled weight matches someone else, it re-sends with theirs before the scale finishes analysing.

Without a profile byte, use the cloud source alongside Bluetooth for the full analysis. It isn't known yet whether a weigh-in that Home Assistant receives is still synced to the Renpho app afterwards.

The link sometimes drops about 5 s after connecting, before the analysis arrives. The weight is kept, and body composition is skipped for that weigh-in.

#### Options

| Option | Default | Meaning |
|---|---|---|
| Send guest profile to scale | Auto | MorphoScan scales need the guest profile and clock on every connection. "Extended" 0x55aa scales (ES-CS20MB2) only release a final reading after a guest profile is written. *Auto* writes only where needed, *Never* is fully read-only (MorphoScan scales then hang up), and *Always* also tries it on unknown 0x55aa models. |
| Capture all BLE characteristics | On | Subscribes to every notify/indicate characteristic and reads every readable one once, recording all traffic for diagnostics. |

## Entities

**Cloud person:** weight, BMI, body fat, body water, skeletal muscle (%), bone mass, BMR, visceral fat, subcutaneous fat, protein, body age, muscle mass, fat-free mass and last measurement. Heart rate appears only for scales that measure it. Weight, body fat, bone mass, BMR and muscle mass carry the app's healthy range as `normal_min` / `normal_max` / `standard` attributes.

People weighed on a body-composition analyser (8-electrode, e.g. R-MSC04/R-MSC06) also get these sensors. They appear only if the person's records contain them:

- **Whole body:** body fat mass, skeletal muscle mass and index, body score, body type (Renpho's code), waist-hip ratio, obesity degree, target weight, and weight, fat and muscle control (kg to gain or lose).
- **Each arm, each leg and the trunk:** fat mass and muscle mass in kg, plus each as a percentage of that segment's standard mass. 100 % means on standard; the standard mass is in the `standard` attribute.
- **Impedance:** 20 kHz and 100 kHz per segment, disabled by default.
- **Muscle mass percentage.**

**Goals:** from each account's profile in the Renpho Health app: weight goal, weight to goal (latest weight minus goal), goal date, body fat goal, starting weight and body fat, waist and hip. Each sensor appears once that value is set in the app. Values are converted to kg and cm if the app stores lb or inches.

**History:** every cloud measurement is written into the matching sensors' long-term statistics at the time it was taken. The graphs therefore go back as far as the Renpho account does, and weigh-ins between two polls aren't lost. The first time a sensor is seen, its full history is imported once; after that only new measurements are added. To re-import everything, call `renpho.import_history` for the cloud entry. This needs the recorder (part of the default config).

**Bluetooth person:** weight, BMI, body fat, body water, skeletal muscle, muscle mass, bone mass, fat-free mass, protein, BMR, visceral fat (MorphoScan only), impedance (0x55aa only, disabled by default) and last measurement.

**Bluetooth scale:** last weight, last person, impedance, last measurement, battery, connection status and signal strength (disabled by default).

Every Bluetooth reading also fires a `renpho_measurement` event:

```yaml
event_type: renpho_measurement
data:
  address: "60:65:F4:12:34:56"
  weight: 82.4
  resistance: 500
  profile: Alex          # null when unassigned
  profile_id: 01K...     # person subentry id
  method: final          # final | record (connected late) | plateau (settled weight only)
  reassigned: false
  scale_record:          # MorphoScan only: the scale's own analysis, or null
    body_fat: 23.7
    bmi: 27.3
    skeletal_muscle: 43.9
    visceral_fat: 8
    impedance_20khz: [22.2, 310.4, 305.5, 227.6, 234.6]   # trunk, L arm, R arm, L leg, R leg
    impedance_100khz: [17.4, 275.3, 270.4, 201.7, 208.5]
    trusted: false       # true when it was used for the person's sensors
```

## Helping add a scale

Every Bluetooth session records the scale's full GATT table (with the values of readable characteristics), every subscription result, and the raw bytes of every notification on every characteristic.

1. Leave **Capture all BLE characteristics** on.
2. Step on the scale and stay on until the display finishes, including body fat if it shows it.
3. *Settings → Devices & services → Renpho → ⋮ → Download diagnostics*.
4. Open an issue and attach the file. Mention what the display showed: weight, and body fat if shown.

Diagnostics are made to be shared publicly. They contain no account data, no names, birthdays or heights, and only the first half of each MAC address. They do include the weights the scale sent.

## Troubleshooting

| Symptom | What to check |
|---|---|
| *Connection status* shows **No connectable proxy** | No adapter or proxy that can make connections can hear the scale. ESPHome proxies need `bluetooth_proxy: active: true`. Move a proxy closer, or check *Signal strength* (enable it first). |
| Status goes *Connecting* then *Error* | The Renpho app on a phone is probably holding the scale's only connection. Close the app and try again. The status sensor's `last_error` attribute has the details. |
| Readings land on *unassigned* | Nobody's last weight is within their tolerance. Widen the person's tolerance, or call `renpho.reassign_last_measurement`. |
| Weight but no body fat | No impedance was measured (shoes on, or the scale's pregnancy mode), or the reading fell back to the settled weight (`method: plateau`). |
| MorphoScan: weight but no body fat | Expected without a *Scale profile byte* (see MorphoScan scales above), or when the link dropped before the analysis. The `renpho_measurement` event's `scale_record` shows what the scale sent. |
| MorphoScan: *Connecting* then nothing | Check that *Send guest profile* isn't set to *Never*. Download diagnostics: `ble.writes` shows whether the handshake was answered. |
| Cloud sensors go unavailable | Look for a re-authentication prompt on the Renpho entry under *Settings → Devices & services*, and check *Settings → System → Logs* for `renpho`. Rate limiting pauses polling until Renpho's `Retry-After` time passes. |

Debug logging, which logs every Bluetooth notification as hex:

```yaml
logger:
  logs:
    custom_components.renpho: debug
```

## Recovery

- **Wrong person matched repeatedly:** edit the person and correct their current weight. Matching uses their last stored reading.
- **Remove a person:** delete them from the scale's entry. Their device, entities and stored readings are removed.
- **Start over with a scale:** delete the entry. Stored readings (`.storage/renpho.<entry_id>`) are deleted with it. It is rediscovered the next time you step on the scale.
- **Changed Renpho password:** the next poll fails to log in and Home Assistant shows a re-authentication prompt on the entry. Enter the new password there.
- **Different Renpho account:** delete the cloud entry and add it again.

## Credits

Built on the work of these projects:

- [DiscountDarcy/ha-renpho-health](https://github.com/DiscountDarcy/ha-renpho-health) and [renpho-api](https://pypi.org/project/renpho-api/): the Renpho Health cloud API.
- [ronnnnnnnnnnnnn/renpho_fitness_scale_ble](https://github.com/ronnnnnnnnnnnnn/renpho_fitness_scale_ble) and [renpho-escs20m](https://pypi.org/project/renpho-escs20m/): the 0x55aa protocol, captures and body-composition formulas.
- [KristianP26/ble-scale-sync](https://github.com/KristianP26/ble-scale-sync): the MorphoScan (R-MSC04) protocol, worked out with its users from real captures. That project is GPL-3.0, so only the documented wire facts are used here; no code is copied.

This project is MIT-licensed (see [LICENSE](LICENSE)). Not affiliated with or endorsed by Renpho.
