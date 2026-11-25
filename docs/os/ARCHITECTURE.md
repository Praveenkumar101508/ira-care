# IRA CARE OS: architecture

Register items E1 (what is app, launcher, system image, firmware and production OS) and the
architecture half of E6. Written 25 November 2026.

**Nothing on this page has been built into an image, booted, or run on a device.** IRA has a
watch app that CI builds in two flavours, and, from this change, an AOSP product layer for a
Cuttlefish virtual device (`os/`) that has never been built because no build host with the
published resources exists (about 400 GB free disk and 64 GB RAM, source.android.com).

"IRA CARE OS" is not one thing. It is five layers, owned by different people, and each is at
a different stage. Talking about them separately is what stops "we have an OS" from meaning
"we have an app".

## 1. The five layers

| Layer | What it is | What IRA has today (repository paths) | What is unbuilt | What needs hardware or a supplier |
| --- | --- | --- | --- | --- |
| **Watch app** | The IRA CARE app: Help, check-ins, falls, readings, speech, visits. An ordinary Android app (APK). | `watch/` (Kotlin, Compose). Two flavours (D-059): `wearos` and `aosp` (`watch/app/build.gradle.kts`, `watch/app/src/aosp/`). CI builds and unit-tests both and runs `scripts/ci/check_watch_aosp_deps.sh`, which fails if the aosp flavour pulls in a Google service library. | A signed `aospRelease` APK has never been produced (it needs IRA's release key, which the owner holds). The app has never run on an AOSP image. | Running on IRA's watch needs the board and a sensor HAL that meets `docs/hardware/SENSOR_HAL.md`. |
| **Launcher** (home screen) | What Android shows as "home": after boot, on the home gesture, and when every other screen closes. On IRA's watch this should be the IRA app itself, with no other home app. | The app's main activity is a `LAUNCHER` entry only (`watch/app/src/main/AndroidManifest.xml`). The image keeps the stock AOSP launcher. The change needed is written down in `os/device/ira/ira_watch_cf/HOME_LAUNCHER.md`. | The app's HOME intent filter (aosp flavour only), back handling, show-when-locked; then removing the stock launcher (`overrides` in `os/device/ira/ira_watch_cf/Android.bp`, commented out until then). | Nothing hardware-specific. Testing it needs a built image (build host). |
| **System image** | The Android system the app runs on: framework, system services, preinstalled apps, overlays, properties, permissions, boot animation; built from AOSP for one target. | `os/device/ira/ira_watch_cf/` for the **Cuttlefish virtual device** (x86_64): product makefile, privileged prebuilt app, privapp allowlist, first-boot grants (proposal), framework overlay, boot animation specification, settings and emergency policy; `os/scripts/build_cuttlefish.sh` and `launch_cuttlefish.sh`. | All of it: no Cuttlefish image has been built or booted. Several AOSP names are marked uncertain in the files themselves (listed in `docs/os/BUILD.md`). A system image for the real watch does not exist even as configuration: it is written on top of the chip vendor's board support package. | The Cuttlefish image needs an AOSP build host (**external blocker**). The real-device image needs the SoC vendor's BSP (`docs/os/QUALCOMM_DEPENDENCIES.md`). |
| **Firmware and bootloader** | Everything below Android on the real chip: the boot ROM and the signed boot chain (on Qualcomm, primary and secondary bootloaders and the Android bootloader that implements fastboot and Android Verified Boot), the TrustZone OS, the modem, DSP, Wi-Fi/Bluetooth and sensor-hub firmware, power-management configuration. | Nothing. The candidate platform (Qualcomm Snapdragon W5+ Gen 1, SW5100 with the QCC5100 co-processor) is named in `docs/hardware/COMPONENTS.md`. | All of it. IRA will not write most of this layer: it comes from the SoC vendor, signed by them, and IRA configures and signs on top. | Entirely supplier and board: NDA access to the BSP, a development board, and the fuse-programming step at manufacture. |
| **Production OS** | What ships on a watch a person wears: the system image built on the BSP for IRA's board, signed with IRA's release keys, with verified boot enforced, a locked bootloader, signed over-the-air updates with rollback protection, a provisioning flow and a device-owner policy, and certified. | Plans only: `docs/hardware/BLUEPRINT.md` section 4, this page (section 3), `os/device/ira/ira_watch_cf/SETTINGS_POLICY.md`. | Everything: release keys, signing, OTA server side, device-policy app, provisioning, factory process. | Board, BSP, supplier signing processes, key custody (an owner decision), certification (`docs/hardware/BLUEPRINT.md` section 8). |

What the Cuttlefish image is for: proving the system-image layer (the product configuration,
the privileged app and its permissions, the overlays, the home-screen change, signing and OTA
mechanics) on a virtual device before any board exists. What it cannot prove: anything about
the firmware layer, real sensors, the radio, battery life, or hardware-backed security.

## 2. The sensor HAL boundary

```
IRA watch app (aosp flavour)               watch/  — IRA owns
  SensorManagerSensorSource -> SensorManager (Android framework, public API)
        |
Android sensor service (system_server)     AOSP — Apache 2.0, unchanged by IRA
        |
Sensors HAL interface (android.hardware.sensors, AIDL in current releases)
        |  <--- THE CONTRACT: docs/hardware/SENSOR_HAL.md (types, units, rates, batching, wake)
Sensors HAL implementation                 board vendor or IRA's hardware engineer
        |
Sensor-hub firmware (QCC5100 co-processor on the W5+ path) and sensor drivers
        |
Sensors (optical heart rate, IMU, off-body, barometer, temperature)   docs/hardware/COMPONENTS.md
```

- IRA owns the top (the app) and the contract. The app reads only standard Android sensor
  types through the public framework API, so any HAL that meets the contract runs it unchanged.
- The HAL implementation and the firmware below it belong to the board. On the W5+ path the
  sensor-hub software is Qualcomm's (proprietary, NDA; see the supplier register).
- On Cuttlefish there is no IRA sensor hardware. Cuttlefish provides virtual sensors through
  its own HAL (UNCERTAIN which types; no heart-rate or off-body sensor is expected). Until a
  board exists the app's simulated driver (`IRA_WATCH_SIMULATED_SENSORS=1`, every reading
  marked "simulated") is how the rest of the system is exercised. A simulated **HAL** (E3) is
  not started.

## 3. The security chain (E4)

Each link, what it does for the person, and whether it can be shown without a board. "Board"
means IRA's target hardware or the SoC vendor's development board, with its BSP.

| Link | What it does | Mechanism (AOSP names) | On Cuttlefish (build host) | Needs the target board |
| --- | --- | --- | --- | --- |
| Verified boot | the watch starts only software IRA signed, and says so if anything was changed | Android Verified Boot 2.0: `vbmeta` signed with `avbtool`, configured by `BOARD_AVB_*` board variables; dm-verity on system partitions | the mechanism can be exercised with test keys or IRA's own keys; the root of trust is the virtual bootloader, so it proves configuration only | yes: the real root of trust is the SoC's secure boot (fused key hash) and a locked bootloader. Fusing is irreversible and done at manufacture |
| Keystore | keys that never leave secure hardware (the app's encrypted storage key, the device's identity key) | Android Keystore over the KeyMint HAL; optionally StrongBox in a secure element | a software KeyMint only. The app's encrypted storage already uses Android Keystore (register C-19); here it would run without hardware backing | yes: hardware backing needs the TEE (Qualcomm's trusted execution environment on the W5+ path) and its KeyMint trusted app, both from the supplier |
| Signed OTA | updates come only from IRA and cannot be altered on the way | release keys (generated with AOSP's `development/tools/make_key`), `sign_target_files_apks` and `ota_from_target_files` (build/make/tools/releasetools), A/B updates applied by `update_engine` | yes, the whole signing and A/B update flow can run against Cuttlefish on a build host. The update server side does not exist (the IRA server serves the app's data, not images) | the final keys and update packages are per board; the mechanism carries over |
| Rollback protection | an attacker cannot reinstall an older, vulnerable image | AVB rollback indexes (`BOARD_AVB_ROLLBACK_INDEX` and per-partition indexes), stored by the bootloader in tamper-evident storage | the index logic can be configured; Cuttlefish's storage proves nothing about tamper resistance | yes: tamper-evident storage (for example eMMC/UFS RPMB) and the bootloader that enforces it are board and supplier parts |
| Provisioning | each watch gets its identity, its owner policy and its mobile plan, once, safely | factory steps (lock bootloader, device identity and attestation keys), the device-owner policy app (`os/device/ira/ira_watch_cf/SETTINGS_POLICY.md`), pairing with the IRA server (exists today: the app's pairing code and device token), eSIM profile download (GSMA SGP.22) | the device-owner app and pairing can be exercised; none of the factory steps can | yes: attestation-key provisioning, bootloader locking and eSIM all need the board and suppliers. UNCERTAIN: attestation-key provisioning for a device without Google services needs a supplier process (Google's remote key provisioning serves certified Google devices) |

Owner decisions this chain needs before any of it is built: who holds the release, platform
and AVB signing keys, where (offline hardware security module or equivalent), and who may
sign. That is recorded in the register's external blockers.

## 4. What is next, in order

1. The watch app gets its HOME filter and lock-screen attributes in the aosp flavour
   (`os/device/ira/ira_watch_cf/HOME_LAUNCHER.md`); this needs no OS build.
2. A build host (external blocker): run `docs/os/BUILD.md`, fix every name marked uncertain,
   boot the Cuttlefish image, record the run.
3. Signing and OTA on Cuttlefish, with test keys first, then the owner's key decision.
4. Supplier access (`docs/os/QUALCOMM_DEPENDENCIES.md`), a development board, and the device
   tree on top of the BSP: only then does a production OS begin.

## Licences

AOSP is Apache 2.0; the Linux kernel is GPL-2.0 (its source must be offered for any kernel
shipped); the SoC vendor's firmware and user-space blobs are proprietary under the supplier's
agreement. IRA's files in `os/` are Apache 2.0. The supplier register lists each part.
