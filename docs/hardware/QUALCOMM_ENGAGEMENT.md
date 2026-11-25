# Engaging Qualcomm for the IRA CARE watch platform (requirement H1–H4.d)

Written 25 November 2026. A plan for the owner, not a contract or a message sent. It names what
IRA needs from Qualcomm (or its authorised distributors and design houses) to build IRA CARE OS on
the Snapdragon W5+ Gen 1 (SW5100 with the QCC5100 co-processor), the candidate platform in
`COMPONENTS.md`. The supplier components it depends on are listed in `docs/os/QUALCOMM_DEPENDENCIES.md`.

## What IRA needs

| Need | Why | Where it lands |
| --- | --- | --- |
| Access to the SW5100 board support package (BSP) under NDA | The kernel, vendor partition, bootloader integration, modem, DSP and sensor-hub firmware the image is built on | `docs/os/ARCHITECTURE.md`, "Firmware and bootloader" layer |
| A reference design and development kit | The development-board gate (`EVT_DVT_PVT_ROADMAP.md`) | Hardware |
| Datasheets for the SW5100, QCC5100 and PMW5100 | Package, power and pin figures marked "not public (NDA)" in `COMPONENTS.md` | Hardware |
| The secure boot and fuse-programming process | Verified boot with IRA's keys; the production line's programming station | `ODM_SPECIFICATION.md` section 5 |
| Sensor-hub (QCC5100) integration guidance | The always-on fall pipeline at 50 Hz on the co-processor | `SENSOR_HAL.md` |
| Modem certification support | VoLTE and network certification in the UK | `BLUEPRINT.md` section 8.6 |
| Licensing terms | Which software IRA may ship, modify and update, for how long, and the support period, which also bears on PSTI | Owner's decision |

## How access is normally obtained

Qualcomm wearable BSPs reach small companies mostly through an authorised design house or ODM
that already holds the licence, rather than directly. The practical route is therefore:

1. Shortlist ODMs and design houses that have shipped SW5100 watches (`ODM_SPECIFICATION.md`).
2. Through the chosen one, request the reference design, the BSP under its licence and a
   development kit.
3. In parallel, contact Qualcomm's sales channel to register the project, which can matter for
   support and allocation.

These are the author's understanding of common industry practice, **not verified** with Qualcomm.

## What IRA brings to the conversation

- **A working software system:** the server, dashboard, phone app and watch app built and tested,
  with the care journey passing end to end (`docs/progress/VERIFICATION.md`).
- **An OS product configuration** for AOSP (`os/device/ira/`), with what is unbuilt stated
  plainly (`docs/os/BUILD.md`).
- **A sensor contract** a board can be checked against (`SENSOR_HAL.md`).
- **An estimated volume and timeline** (`BLUEPRINT.md` section 8.9, estimates).

## Questions to settle with Qualcomm or the design house

1. Does the SW5100 BSP support a current AOSP release for a non-Google (AOSP) watch, and for how
   long? What security-update cadence is committed?
2. Can IRA hold its own verified-boot keys and program the fuses at its ODM's line?
3. Is StrongBox available on the SW5100, and is attestation-key provisioning available to IRA
   (E4.j)?
4. What minimum order and licensing fees apply at 1k, 10k and 50k units?
5. Can the QCC5100 run third-party code, or is IRA's fall pipeline limited to the application
   processor?

## What it does not commit

Nothing here commits money or signs anything. NDAs, licences and purchases are the owner's
decisions (requirement E6.d, blocked: supplier access under NDA).
