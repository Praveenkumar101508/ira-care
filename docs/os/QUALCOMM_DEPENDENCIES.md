# Supplier dependency register: Qualcomm and others (E6)

Every part of IRA CARE OS that IRA cannot build from public source, and what the owner has
to do to get it. Written 25 November 2026 for the compute path named in
`docs/hardware/COMPONENTS.md`: **Qualcomm Snapdragon W5+ Gen 1 (SW5100, 4G) with the QCC5100
co-processor and the PMW5100 power-management IC**. The alternative path (another application
processor plus a Quectel LTE module) is noted where it changes the answer.

**Nothing here has been obtained.** IRA has no Qualcomm account, no NDA, no BSP and no board.
"Availability" says how the part is normally obtained, as far as public information shows;
where only the supplier can say, the row says so. Licence terms of NDA material are not
public and are described only in general.

The Cuttlefish image (`os/`) depends on **none** of these: it is built entirely from AOSP
(Apache 2.0) and runs on a virtual x86_64 device.

## The register

| # | Component | Supplier | What it is for | Availability | Licence | Owner action needed |
| --- | --- | --- | --- | --- | --- | --- |
| Q1 | Board support package (BSP) for SW5100 | Qualcomm | the vendor half of Android for the chip: device tree, HAL implementations, vendor libraries, build integration for the AOSP release Qualcomm supports | not public. Distributed to licensed customers (typically through Qualcomm's customer portal), often through a design house or ODM that already holds the licence. Which AOSP release it supports is set by Qualcomm | proprietary licence agreement for the closed parts; the open-source parts (kernel, some HALs and bootloader sources) are published under their own licences | approach Qualcomm or an authorised design house; sign the NDA and software licence; confirm in writing that the SW5100 BSP supports AOSP **without** Wear OS and Google services (the product brief says "Android Open Source supported"; terms not public) |
| Q2 | Linux kernel for the platform | Qualcomm (on Android common kernel) | drivers for the SoC, display, power, radios, sensors bus | source for the shipped kernel must be obtainable (GPL); the platform tree comes with the BSP. Qualcomm publishes open-source code on CodeLinaro (formerly CodeAurora) | GPL-2.0 (IRA must offer the kernel source for every image it ships) | obtain with Q1; set up the GPL source-offer process before first shipment; decide whether to track Android's Generic Kernel Image |
| Q3 | Boot chain and bootloader (primary/secondary boot loaders, Android bootloader with fastboot and AVB) | Qualcomm | starts the chip, enforces secure boot and verified boot, holds the rollback index | binaries signed by Qualcomm with the BSP; the Android bootloader is partly open source | proprietary for the early stages; open-source licence for the open parts | obtain with Q1; plan secure-boot key generation and fuse programming (irreversible) with the supplier and the contract manufacturer (E4) |
| Q4 | TrustZone OS and KeyMint trusted app | Qualcomm | hardware-backed Keystore, attestation, secure storage | binaries with the BSP | proprietary | obtain with Q1; confirm the KeyMint version and whether attestation-key provisioning is available for a device without Google services (`docs/os/ARCHITECTURE.md` section 3) |
| Q5 | LTE modem firmware and radio interface (RIL) | Qualcomm (SoC path) or Quectel (module path) | calls and data on its own line: Help when away from home Wi-Fi | SoC path: signed firmware and vendor RIL with the BSP. Module path: Quectel supplies its Android RIL driver to customers on request (not verified) | proprietary; firmware cannot be modified | obtain with Q1 (or from Quectel); the modem then needs GCF/operator approval (`docs/hardware/BLUEPRINT.md` 8.6). Cuttlefish has **no modem** in IRA's image: no real calls or SMS |
| Q6 | eSIM (eUICC) and local profile assistant (LPA) | Infineon (OC1230) for the chip; an LPA supplier | the watch's own mobile plan without a SIM tray | eUICC chip: orderable (the page notes introduction in the first half of 2026). AOSP defines the eUICC service API but ships no LPA implementation, so an LPA must be licensed or written | chip: commercial. LPA: per supplier | choose an LPA supplier (or decide to build one against GSMA SGP.22); agree the profile provider (operator or MVNO) |
| Q7 | Sensor hub software on the QCC5100 co-processor | Qualcomm | always-on sensor processing so the main processor sleeps (`docs/hardware/SENSOR_HAL.md` section 3); the sensors HAL that feeds Android | with the BSP; the co-processor's software framework and its sensor driver model are NDA material | proprietary | obtain with Q1; ask which sensors have drivers already and what it costs to add the heart-rate and off-body sensors from `COMPONENTS.md`; the HAL must meet `SENSOR_HAL.md` |
| Q8 | GPU user-space drivers (Adreno 702) | Qualcomm | drawing the screen (OpenGL ES, Vulkan) | binary blobs with the BSP. Open drivers (Mesa freedreno/turnip) exist for some Adreno generations; support for the 702 is NOT VERIFIED | proprietary blobs; kernel GPU driver GPL-2.0 | obtain with Q1 |
| Q9 | DSP firmware (audio, voice wake-up) | Qualcomm | low-power microphone listening, speaker path, echo cancellation for calls | firmware with the BSP | proprietary | obtain with Q1; confirm the low-power voice path that the power budget assumes (`BLUEPRINT.md` section 5) |
| Q10 | Wi-Fi, Bluetooth and GNSS firmware and HALs | Qualcomm (on SoC) or Infineon (CYW43012 path) | upload at home, the phone link, one position per SOS | firmware and location HAL with the BSP; Infineon publishes Linux drivers for its combos, firmware under its licence | proprietary firmware; drivers GPL or vendor licence | obtain with Q1; check that the GNSS HAL works with no Google location service (the app uses the platform `GPS_PROVIDER` only) |
| Q11 | Power management (PMW5100) configuration, charger and fuel gauge | Qualcomm | battery charging, gauge, rails, haptics driver | no public datasheet (`COMPONENTS.md`); configuration with the BSP | proprietary documentation under NDA | obtain with Q1; a hardware engineer owns the battery profile |
| Q12 | NDA documentation (datasheets, hardware design guides, reference schematics, layout guides, software guides) | Qualcomm | designing the board and bringing it up | only under NDA, normally through the customer portal or a design house; package dimensions and current figures are not public (`COMPONENTS.md`) | confidential; cannot be copied into this public repository | sign the NDA; decide where NDA material is kept (never in this repository) |
| Q13 | Display panel initialisation and touch controller driver | the panel module maker (SH8601/CO5300 driver, FT3168/FT6146 touch, per `COMPONENTS.md`) | the round screen and touch | panel init sequences and timing from the module maker; touch drivers often available from the controller vendor | varies; touch drivers usually GPL when in the kernel | request the panel's MIPI-DSI init sequence and the touch driver; confirm a panel that supports the SoC's display interface (the candidate module page lists QSPI, the SoC lists MIPI-DSI: to be resolved by the hardware engineer) |
| Q14 | Speech engines on the device (recognition and synthesis) | open projects or a commercial supplier | Talk to IRA without a hosted service (D-056) | open engines exist; each has its own model licence | per engine (to be checked per model, as `docs/DATASETS.md` does for data) | choose engines, record their licences before they enter an image |

## What does not depend on a supplier

- The watch app (`watch/`, aosp flavour): AndroidX and Compose libraries are open source;
  `scripts/ci/check_watch_aosp_deps.sh` keeps Google service libraries out.
- The system-image configuration in `os/` and the Cuttlefish image built from it: AOSP only.
- The server, dashboard and phone app.

## The order of owner actions

1. Decide the compute path (SW5100 or application processor plus module): `COMPONENTS.md`
   "next steps" item 2. It decides which rows above apply.
2. Contact Qualcomm or an authorised design house; NDA and software licence (Q1, Q12).
3. Buy a development board with the BSP; confirm AOSP without Google services builds on it.
4. In parallel, the items that do not wait for the board: LPA supplier (Q6), speech engine
   licences (Q14), GPL source-offer process (Q2), signing-key custody (E4).

Recorded in `docs/progress/COMPLETION_REGISTER.md` under "External blockers" as "Qualcomm
supplier access".
