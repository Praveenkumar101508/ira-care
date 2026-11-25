# Sensor evaluation plan for the IRA CARE watch (requirement H1–H4.c)

Written 25 November 2026. A bench plan for the development board and the engineering prototype.
Nothing has been bought, built or measured; every threshold below is a proposed pass criterion
for the hardware engineer and the Clinical Safety Officer to confirm. It builds on the sensor
contract (`SENSOR_HAL.md`), the candidate parts (`COMPONENTS.md`) and the blueprint's
manufacturing route (`BLUEPRINT.md` section 6).

## Who runs it, where, with what

- **When:** the development-board stage (the candidate sensors on a daughter board), then repeated
  on the engineering prototype in its case. A sensor passes only in the case, because the case
  changes optics, thermal paths and the accelerometer's coupling to the wrist.
- **Who:** the hardware engineer, with a physiotherapist for the fall work.
- **Software:** the watch app's `aosp` flavour, unchanged, with the debug readings export. Every
  reading carries its own timestamp. The reference instruments are time-aligned to the watch's
  clock at the start of each session by a tap event both record.

## 1. Motion (accelerometer and gyroscope; BMI270 or the SoC's sensor)

| Test | Method | Reference | Pass (proposed) |
| --- | --- | --- | --- |
| Scale and bias | The watch held still in six orientations, 30 s each | Gravity (±1 g on each axis) | Each axis within ±0.05 g of ±1 g after factory calibration |
| Noise | 60 s still on a vibration-isolated block | — | ≤ 2× the datasheet noise density |
| Rate and timing | 10 min at 50 Hz | Sample timestamps | Mean rate 50 ± 0.5 Hz; no gap over 40 ms; timestamps monotonic |
| Saturation | Drop tests onto a mat from 0.5 m and 1 m | Range setting | No clipping at the configured range (±8 g or ±16 g); the fall model's input range respected |
| Gyroscope | Turntable at known rates | Turntable rate | Within ±3 % at 90 and 180 °/s |
| Fall pipeline on the device | The fall-model windows recomputed from the device's stream and compared with the same stream run through `scripts/training/fall_model_v3.py`'s feature code | Python reference | Identical candidate windows; scores within 1e-4 |

## 2. Optical heart rate (AS7058 or the alternative)

| Test | Method | Reference | Pass (proposed) |
| --- | --- | --- | --- |
| Rest | 10 min seated, three skin tones (Fitzpatrick I–II, III–IV, V–VI), at least five people each, adults including over-65s | Chest-strap ECG or a clinical ECG, as the CSO chooses | Mean absolute error ≤ 5 bpm; Bland–Altman 95 % limits within ±10 bpm |
| Light activity | Walking, arm movement, washing up (5 min each) | Same | MAE reported per activity; no pass threshold until the pilot sets one |
| Poor contact | The strap loosened by a notch | Same | Samples marked `SENSOR_STATUS_UNRELIABLE` or `NO_CONTACT`, never reported as good |
| Off-body | Watch taken off and put on a table, 20 times | Observer | Off reported within 1 s every time; no false "on" on a table in 10 min |
| Power | Current draw at 1 Hz and at the HAL's rest rate | Power analyser | Within the budget in `BLUEPRINT.md` section 5 |

This bench test is not clinical validation. It is the check that the sensor meets the contract
before the supervised pilot (`docs/compliance/PILOT_PROTOCOL.md`) measures accuracy properly.

## 3. Barometer (BMP585) and skin temperature (TMP117), planned features

| Test | Method | Pass (proposed) |
| --- | --- | --- |
| Barometer relative accuracy | A 1 m height change, 20 times | Detected in every trial; noise within datasheet × 2 |
| Barometer in water | IP68 soak, then reading | Reading recovers within 60 s |
| Skin temperature | Against a calibrated probe at the wrist, at rest | Within ±0.3 °C after a 10-minute settle; reported only as a trend, never a body temperature |

Neither feeds an alert today (`care/capabilities.py`). They are evaluated so the decision to use
them later is informed.

## 4. Location (GNSS, the SoC's or the MIA-M10Q)

| Test | Method | Pass (proposed) |
| --- | --- | --- |
| Time to fix | Outdoors, cold and hot starts, 20 each | Hot start ≤ 5 s; cold start ≤ 60 s |
| Indoors | Inside a typical house, 20 trials | The accuracy reported honestly; "unavailable" said when there is no fix (`care/location.py`) |
| Accuracy | Surveyed point outdoors | 95 % of fixes within 10 m |

## 5. Battery and the whole watch

- A full day of scripted wear: check-ins, readings at 60 s, 30 minutes of conversation, one
  simulated fall and one Help. The result is the battery left at 22:00.
- The pass mark is set in `BLUEPRINT.md` section 5's budget, from the target of at least a day
  of use. The measured figure replaces the estimate there.

## Records

Each session produces a dated record of the raw streams, the reference exports and the analysis
script output. It is kept with the hardware revision tested, and summarised in a results table
added to this page. Any figure here that the bench contradicts is corrected, not kept.
