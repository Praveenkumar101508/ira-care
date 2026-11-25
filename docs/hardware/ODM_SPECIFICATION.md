# ODM specification for the IRA CARE watch (requirement H1–H4.e)

Written 25 November 2026 for a request for quotation to an original design manufacturer (ODM).
Nothing has been sent or contracted. It gathers what an ODM needs to quote from the existing
design files: `BLUEPRINT.md`, `COMPONENTS.md`, `SENSOR_HAL.md`, `parts.json`, the 3D model
(`ira_watch.glb`) and the dimensioned drawing (`dimensions.svg`). Where those files say "not
verified" or "estimate", so does this.

## 1. The product

A 46 mm round smartwatch for older people living alone, running IRA CARE OS (AOSP based) with
the IRA watch app as its home screen. It has its own mobile line (LTE with VoLTE, eSIM), a Help
button, fall detection, heart rate and a two-day-class battery target. It is sold to care
organisations and families, not as a general consumer watch.

## 2. What the ODM is asked to deliver

1. Electrical design: the logic board and the sensor board on the reference platform below.
2. Antenna design and tuning for LTE, GNSS, Wi-Fi and Bluetooth in the case, measured on a
   wrist phantom.
3. Mechanical design for manufacture from our design intent (`ira_watch.glb`, `dimensions.svg`).
4. The board support package (BSP) bring-up with the chip vendor, so that our AOSP-based image
   boots. We supply the product configuration (`os/device/ira/`) and the app; the ODM supplies the
   vendor partition, kernel, bootloader integration and the sensor HAL (`SENSOR_HAL.md`).
5. The charging dock.
6. Builds at each gate (`EVT_DVT_PVT_ROADMAP.md`), pre-compliance testing, and support through
   certification.
7. A production line: test fixtures, a calibration station for the accelerometer and the optical
   sensor, a programming station for IRA's signed images and keys, and traceability per unit.

## 3. Platform and parts (preferred; the ODM may propose equivalents with reasons)

| Function | Preferred | Requirement that must hold for any substitute |
| --- | --- | --- |
| SoC | Qualcomm Snapdragon W5+ Gen 1 (SW5100 + QCC5100) | AOSP supported on the SoC; LTE Cat 1 bis or better with VoLTE; GNSS; always-on co-processor for sensors |
| PMIC | Qualcomm PMW5100 | Fuel gauge; haptics driver; charge from the dock |
| eSIM | Infineon OC1230 | GSMA SGP.22 v3; remote provisioning |
| Motion | Bosch BMI270 | ±8 g at 50 Hz with FIFO; the bench criteria in `SENSOR_EVALUATION_PLAN.md` |
| Optical HR | ams OSRAM AS7058 | Contact detection exposed as off-body; honest accuracy flags |
| Barometer, skin temp | BMP585, TMP117 | Optional at EVT; footprints reserved |
| Display | 1.39–1.43 inch round AMOLED, 466 × 466, touch | Readable at arm's length; always-on mode |
| Battery | About 410 mAh curved Li-Po | UN 38.3 and IEC 62133-2 certified cell |
| Charging | Qi 5 W BPP receiver (BQ51013C class) or a contact dock | The Qi transmitter controllers we checked are end-of-life (`COMPONENTS.md`); the ODM proposes the dock |

## 4. Performance requirements

- **Battery:** at least 24 hours of the scripted day in `SENSOR_EVALUATION_PLAN.md` section 5;
  target 48.
- **Help button:** a physical button that works with the screen off and from the lock screen.
  Press to vibration in under 200 ms.
- **Water:** IP68, with a target of 5 ATM for washing and showering.
- **Speaker and microphone:** speech intelligible at arm's length in a quiet room, for the
  companion's questions and answers.
- **Radio:** LTE bands for UK networks (bands 1, 3, 7, 8, 20 as a minimum, to be confirmed with
  the chosen network); VoLTE certified with at least one UK network.
- **Strap:** quick-release, one-handed, soft; sizes for small and large wrists.

## 5. Security and software requirements

- Verified boot (AVB 2.0) with IRA's keys. The ODM programs the public key hash into the fuses on
  the line, with a locked bootloader; IRA keeps the private keys (decision E4.g, the owner's).
- Rollback-protected storage (RPMB) wired to AVB rollback indexes.
- A hardware-backed keystore (StrongBox where the SoC offers it). The watch app's storage keys
  depend on it (C-19.b).
- No debug ports enabled on production units. The UART and JTAG test points are fused off or
  removed.
- Over-the-air updates through A/B partitions; the update server is ours.
- UK PSTI: no universal default passwords, a published vulnerability disclosure policy, and a
  stated support period.

## 6. Quality and compliance

- The certifications in `BLUEPRINT.md` section 8: UKCA and CE under the radio equipment
  regulations, battery, RoHS and WEEE, SAR on the wrist, IP68, network and eSIM, and PSTI.
- An ISO 9001 quality system at the ODM. ISO 13485 only if the MHRA adviser finds the product is
  a medical device (`docs/compliance/MHRA_ADVISER_QUESTIONS.md`).
- Per-unit traceability: serial number, board revision, calibration values and test results.

## 7. What the quotation must state

- Non-recurring engineering cost by gate.
- Unit cost at 1k, 10k and 50k.
- Tooling cost and lead times.
- The BSP and licensing terms from the chip vendor, and who holds them.
- The test and certification support included.
- Who owns the design files at the end.

IRA's estimates in `BLUEPRINT.md` section 8.9 are estimates only, and the quotation replaces them.
