# Building the IRA CARE OS Cuttlefish image

**None of the steps on this page has been run, in this repository's CI or anywhere else.**
Each step is marked *(not run in this repository's CI)*. They are written from AOSP's public
documentation (source.android.com: hardware requirements, downloading the source, building,
Cuttlefish) and from the files in `os/`. The first person to run them on a real host should
correct this page as they go and record the run.

The only parts CI exercises are the build script's refusals: it refuses on low resources and
does not download without the opt-in (`supracloud-jarvis/ira/tests/readiness/test_os_product_layer.py`).

## The host this needs

Published by source.android.com:

- a 64-bit x86 machine (Linux; Ubuntu LTS is the documented choice);
- at least 400 GB free disk (about 250 GB for the checkout, 150 GB for the build);
- at least 64 GB RAM.

Not published, IRA's own floor: 8 CPU cores (Google's build machines have 72; fewer cores
work but a full build then takes many hours). For launching Cuttlefish the host also needs
KVM (`/dev/kvm`) and the Cuttlefish host package.

The machine this repository is developed on has 4 cores, 15 GB RAM and about 55 GB free, so
`os/scripts/build_cuttlefish.sh` refuses to start there, by design.

## Steps

1. **Check out this repository** on the build host. *(not run in this repository's CI)*

2. **Install the build prerequisites** listed on source.android.com for the chosen Ubuntu
   release (`git`, `repo`, Python 3, the packages the "Establishing a build environment" page
   names) and the Android SDK build-tools (for `aapt2` and `apksigner`). *(not run in this
   repository's CI)*

3. **Install the Cuttlefish host package** (`google/android-cuttlefish` on GitHub builds the
   Debian packages), add your user to the `kvm`, `cvdnetwork` and `render` groups, reboot.
   UNCERTAIN: the group names come from the Cuttlefish "Get started" page and may change with
   the host package. *(not run in this repository's CI)*

4. **Run the preflight alone** to see what the script measures:

   ```sh
   bash os/scripts/build_cuttlefish.sh
   ```

   It prints disk, RAM and CPU against the minimums and either refuses (exit 2) or prints
   the plan (exit 3). Nothing is downloaded. *(not run in this repository's CI on a qualifying
   host; CI runs only the refusal)*

5. **Prepare the signed watch APK.** Either set the release signing variables named in
   `watch/app/build.gradle.kts` (`IRA_WATCH_KEYSTORE` and the passwords) so the script can run
   `./gradlew :app:assembleAospRelease`, or build it yourself and set
   `IRA_OS_WATCH_APK=/path/to/app-aosp-release.apk`. The debug build cannot be used (its package
   is `care.ira.watch.debug`). *(not run in this repository's CI)*

6. **Run the build with the opt-in:**

   ```sh
   export IRA_OS_AOSP_DIR=/srv/aosp-ira          # on the 400 GB disk
   export IRA_OS_I_HAVE_THE_RESOURCES=1
   bash os/scripts/build_cuttlefish.sh
   ```

   The script then: `repo init -u https://android.googlesource.com/platform/manifest
   -b android-latest-release` (AOSP's recommended branch since 27 March 2025; override with
   `IRA_OS_AOSP_BRANCH`), `repo sync`, copies `os/device/ira` to `device/ira` in the checkout,
   checks and places the APK (`aapt2` package name, `apksigner verify`, permissions compared
   with the source manifest), checks that the inherited Cuttlefish makefile exists, and runs
   `source build/envsetup.sh && lunch ira_watch_cf-trunk_staging-userdebug && m`.
   *(not run in this repository's CI)*

7. **Fix what the first build reports.** Expected points of failure, each marked in the files:
   - the inherited path `device/google/cuttlefish/vsoc_x86_64_only/phone/aosp_cf.mk` (the
     script names the alternative `vsoc_x86_64/phone/aosp_cf.mk` if that is what exists; change
     `PRODUCT_DEVICE` with it);
   - the release config in the lunch name (`trunk_staging`): run `lunch` without an argument
     to see what the branch offers, and set `IRA_OS_LUNCH`;
   - `frameworks/native/data/etc/android.hardware.type.watch.xml` alongside the phone's
     handheld feature file;
   - Soong properties on `android_app_import` and `prebuilt_etc` in
     `os/device/ira/ira_watch_cf/Android.bp`.
   *(not run in this repository's CI)*

8. **Launch** from the same shell (it needs `ANDROID_PRODUCT_OUT` from `lunch`):

   ```sh
   bash os/scripts/launch_cuttlefish.sh
   ```

   This runs `launch_cvd --daemon --cpus=4 --memory_mb=4096
   --display0=width=454,height=454,dpi=320,refresh_rate_hz=60`. If the host package rejects
   `--display0`, set `IRA_CF_DISPLAY_FLAGS="--x_res=454 --y_res=454 --dpi=320"` (the older
   form) or use `cvd create` with its `--display` flag (the newer form). Open the device in the
   browser through the host package's web interface (the Cuttlefish "Get started" page gives
   the address). *(not run in this repository's CI)*

9. **Check the image against the configuration.** Each command below is expected to show the
   value on the right; any difference is a fix to `os/` and a correction to this page.
   *(not run in this repository's CI)*

   | Check | Command | Expected |
   | --- | --- | --- |
   | product | `adb shell getprop ro.product.name` | `ira_watch_cf` |
   | brand, model | `adb shell getprop ro.product.brand`, `ro.product.model` | `IRA`, `IRA CARE Watch (Cuttlefish)` |
   | locale | `adb shell getprop persist.sys.locale` or `ro.product.locale` | `en-GB` |
   | display | `adb shell wm size`, `adb shell wm density` | `454x454`, `320` |
   | app privileged | `adb shell pm path care.ira.watch` | a path under `/product/priv-app/` |
   | allowlist enforced, boot completed | `adb shell getprop sys.boot_completed` | `1` (a missing privileged permission would stop boot) |
   | first-boot grants | `adb shell dumpsys package care.ira.watch` | the runtime permissions in `default-permissions-care.ira.watch.xml` shown `granted=true` (BODY_SENSORS_BACKGROUND is uncertain) |
   | overlay | `adb shell cmd overlay dump care.ira.os.overlay.framework` | enabled, and each resource in its `config.xml` mapped (a name the release does not define is reported unmapped) |
   | watch feature | `adb shell pm list features` | includes `android.hardware.type.watch` |
   | lock screen | turn the screen off and on | no lock screen; the stock launcher until the app has its HOME filter |

10. **Record the run**: the AOSP revision (`repo manifest -r`), the host, the times, the
    output of step 9, and screenshots, in a dated file under `docs/evidence/`; then update
    register rows E2 and E6. *(not run in this repository's CI)*

## Places this configuration is not certain

Collected from the comments in `os/`, so the first build host knows where to look:

- `device/google/cuttlefish/vsoc_x86_64_only/phone/aosp_cf.mk` versus `vsoc_x86_64/phone/aosp_cf.mk`,
  and with it `PRODUCT_DEVICE` (and so which Cuttlefish `BoardConfig.mk` applies).
- The release config `trunk_staging` in `COMMON_LUNCH_CHOICES` and the lunch target.
- `TARGET_BOARD_INFO_FILE` with `config=phone`, and whether a `wear` host configuration exists.
- Declaring `android.hardware.type.watch` on top of a phone product's handheld features.
- The stock launcher module name (`Launcher3QuickStep`) for the future `overrides`.
- Framework overlay names: `config_mainBuiltInDisplayIsRound` (medium),
  `config_disableLockscreenByDefault` (low), the numeric meaning of
  `config_longPressOnPowerBehavior` in the target release.
- Whether the default-permission policy grants the hard-restricted `BODY_SENSORS_BACKGROUND`.
- The `launch_cvd` display flag form the host package accepts.
- Cuttlefish's virtual sensor types.
