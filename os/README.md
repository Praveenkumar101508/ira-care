# IRA CARE OS: AOSP product layer (unbuilt)

This folder is the AOSP product layer for IRA CARE OS on a **Cuttlefish virtual device**:
configuration and scripts that a build host copies into an Android Open Source Project
checkout. **No image has been built from it and nothing has booted from it.** A full AOSP
build needs a 64-bit x86 host with about 400 GB free disk and 64 GB RAM (source.android.com);
the machine this repository is developed on has neither, and the build script refuses to start
on such a machine.

| Path | What it is |
| --- | --- |
| `device/ira/ira_watch_cf/AndroidProducts.mk` | registers the product and its lunch choices |
| `device/ira/ira_watch_cf/ira_watch_cf.mk` | the product: inherits Cuttlefish x86_64, sets name, brand, model, `en_GB`, the watch feature, IRA's modules |
| `device/ira/ira_watch_cf/Android.bp` | the watch app as a privileged prebuilt, its permission files, the framework overlay |
| `device/ira/ira_watch_cf/permissions/` | privileged-permission allowlist (empty, derived from the manifest) and proposed first-boot grants |
| `device/ira/ira_watch_cf/overlays/IraFrameworkOverlay/` | round screen, no lock screen by default, emergency gesture and power menu kept |
| `device/ira/ira_watch_cf/bootanimation/` | boot animation specification (`desc.txt`, README); no frames |
| `device/ira/ira_watch_cf/HOME_LAUNCHER.md` | why IRA is not yet the home app, and the app change that makes it one |
| `device/ira/ira_watch_cf/EMERGENCY_UI.md` | how Help stays reachable from the lock screen |
| `device/ira/ira_watch_cf/SETTINGS_POLICY.md` | developer options and factory reset behind an administrator |
| `scripts/build_cuttlefish.sh` | resource preflight, then (only with `IRA_OS_I_HAVE_THE_RESOURCES=1`) sync and build |
| `scripts/launch_cuttlefish.sh` | `launch_cvd` with a 454 x 454 display |

The architecture (what is app, launcher, system image, firmware and production OS), the build
steps and the supplier register are in `docs/os/`.

## Licence

IRA's files here are under the Apache License, Version 2.0, the licence of AOSP. The AOSP
files they inherit and build against (Cuttlefish, the framework, the build system) are
Apache 2.0 and keep their own notices in the AOSP checkout; nothing from AOSP is copied into
this repository. Parts of a real device image carry other licences (the Linux kernel is
GPL-2.0; vendor blobs are proprietary): see `docs/os/QUALCOMM_DEPENDENCIES.md`.
