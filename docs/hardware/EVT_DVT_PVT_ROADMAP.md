# EVT, DVT and PVT roadmap for the IRA CARE watch (requirement H1–H4.f)

Written 25 November 2026. The hardware gates from the manufacturing route in `BLUEPRINT.md`
section 6, each with what is built, what is tested and the exit criteria to pass before the next.
All timings are estimates, not quotations; nothing has been built.

| Gate | When (estimate) | Units | What is built |
| --- | --- | --- | --- |
| Development board | Months 0–3 | 2–5 | The chip vendor's watch reference board, or an AOSP-capable board, with the sensors on a daughter board |
| EVT (engineering validation) | Months 3–8 | 20–50 | Our own logic and sensor boards in a machined case; a first dock |
| DVT (design validation) | Months 8–12 | 100–200 | The production design: moulded case, sealing, strap, dock; industrial design frozen |
| PVT (production validation) | Months 12–16 | 300–1,000 | Units from the ODM's production line and tooling; the pilot run for the wear trial |
| Mass production | From month 16 | Volume | — |

## Development board: exit criteria

- [ ] The IRA CARE OS configuration (`os/device/ira/`) builds on the vendor BSP and boots with the
      watch app as home (the Cuttlefish steps in `docs/os/BUILD.md` repeated on hardware).
- [ ] Every sensor in `SENSOR_HAL.md` section 1 reports through the HAL with true timestamps.
- [ ] The bench tests in `SENSOR_EVALUATION_PLAN.md` sections 1 and 2 pass on the board.
- [ ] The care journey (`docs/demo/CARE_JOURNEY_22.md`) runs end to end with the board as the
      watch (requirement J.f).

## EVT: exit criteria

- [ ] All of `SENSOR_EVALUATION_PLAN.md` passes in the case.
- [ ] Power: the scripted day measured. The battery life is at least 24 hours, or the gap is
      understood with a fix planned.
- [ ] Antenna: radiated LTE, GNSS, Wi-Fi and Bluetooth performance measured on a wrist phantom,
      within the targets the ODM agreed.
- [ ] Pre-compliance: radio emissions, SAR on the wrist, and electrostatic discharge, with no
      blocking failure.
- [ ] Fall detection on the device: simulated falls on mats under supervision, with the shipped
      model v3. Sensitivity and false alarms are recorded against the held-out figures in
      `docs/ai/fall/MODEL_CARD_V3.md`; a material drop stops the gate.
- [ ] Verified boot with test keys; the hardware keystore used by the app (C-19.b).
- [ ] Every "not verified" figure in `COMPONENTS.md` resolved from full datasheets.

## DVT: exit criteria

- [ ] Reliability:
  - drop tests;
  - button life (100,000 presses);
  - strap pull;
  - sweat and cosmetics;
  - temperature cycling;
  - IP68 immersion on the production case.
- [ ] Formal testing for UKCA and CE radio, SAR, battery (UN 38.3, IEC 62133-2), RoHS, and
      PSTI evidence. The certification bodies are engaged.
- [ ] VoLTE and eSIM certification with at least one UK network.
- [ ] Usability with older people: the button, the screen, the strap and the voice, in a small
      supervised session.
- [ ] Signed release builds with IRA's production keys; OTA update and rollback protection
      exercised on the device.

## PVT: exit criteria

- [ ] The production line's yield is at or above the ODM's agreed target over the pilot run.
- [ ] Per-unit calibration and test records complete for every unit.
- [ ] Certification complete; declarations of conformity issued.
- [ ] The supervised pilot (`docs/compliance/PILOT_PROTOCOL.md`) runs on PVT units, with the
      Clinical Safety Officer's sign-off before the first participant.
- [ ] Field returns and the incident process (`docs/compliance/INCIDENT_PROCESS.md`) rehearsed.

## What gates the gates

The first gate needs the chip vendor's BSP under NDA (`QUALCOMM_ENGAGEMENT.md`) and a board.
Every gate needs money and an ODM contract, which are the owner's decisions. Requirements
H1–H4.h to H1–H4.j record these as blocked.
