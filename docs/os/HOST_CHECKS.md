# What the host checks of the OS product layer prove, and what they do not

The AOSP product layer in `os/` has never been built: that needs a 64-bit x86 host with about
400 GB free disk and 64 GB RAM (`BUILD.md`), and none exists for this repository. This page
lists every check that runs on an ordinary machine instead, in
`supracloud-jarvis/ira/tests/readiness/test_os_product_layer.py` (76 tests with
`test_os_sensor_sim.py`), what each proves, what it does not, and what only a real build can
show. **None of them proves that an image builds, boots, or that a person would see what the
files describe.**

Run them with:

    cd supracloud-jarvis/ira && python3 -m pytest -q tests/readiness/test_os_product_layer.py tests/readiness/test_os_sensor_sim.py

## The reference the checks compare against

`os/reference/aosp_reference.json`, written by `os/scripts/aosp_reference.py` on 26 November 2026.
android.googlesource.com is not reachable from the build machine (the network proxy refuses
it), so the script reads the public Git mirrors instead and records where each fact came from:

| What | Source read | Branch |
| --- | --- | --- |
| framework resource names and types (`config.xml`), permission protection levels, the default-permission policy's XML tags, the boot animation's search paths | `frameworks/base` on github.com/aosp-mirror | `android16-release` (what `android-latest-release` pointed at when written) |
| `Launcher3QuickStep` as a handheld system_ext package, `lunch`'s default release config | `build/make` on github.com/aosp-mirror | `android16-release` |
| Sensors HAL flag constants, sensor type ids, the default service's AIDL version (3) and VINTF fragment | `hardware/interfaces` on github.com/LineageOS (no aosp-mirror copy) | `lineage-22.2` (Android 15) |
| which feature files exist under `frameworks/native/data/etc/` | `frameworks/native` on github.com/LineageOS | `lineage-22.2` |
| `device/google/cuttlefish`, `packages/apps/Launcher3` | **no public mirror reachable; unchecked** | |

Only names, types and constants are copied; no code. The reference records what it could not
check (`unchecked`). `python3 os/scripts/aosp_reference.py --check` says whether the mirrors
have moved on.

## Defects the checks found and fixed (26 November 2026)

1. **A build-stopping copy source.** `ira_watch_cf.mk` copied
   `frameworks/native/data/etc/android.hardware.type.watch.xml`. AOSP has no such file
   (`wearable_core_hardware.xml` exists there and does not declare the watch type; Wear devices
   declare it in their own device trees). `PRODUCT_COPY_FILES` with a missing source fails the
   build. Fixed: IRA's own `permissions/android.hardware.type.watch.xml`, copied to
   `/product/etc/permissions/`.
2. **The simulated HAL declared one-shot sensors.** `SimSensorsHal.cpp` wrote the on-change
   reporting mode as `2 << 1` (= 4), which is `SENSOR_FLAG_BITS_ONE_SHOT_MODE`; the framework
   would have deactivated the heart-rate and off-body sensors after their first event. Fixed:
   the `SensorInfo::SENSOR_FLAG_BITS_*` constants (on-change = 2, wake-up = 1).
3. **The HAL module was incomplete.** No init script and no VINTF fragment, and the NDK library
   version was a guess (V2). Added both files; V3, the version the AOSP default service links.
4. **Stale text** that would have misled the first build host: the boot-animation README said the
   `PRODUCT_COPY_FILES` line was commented out (it is live); the privapp allowlist and
   `HOME_LAUNCHER.md` said the aosp flavour has no manifest (it has, since #60); `BUILD.md`
   named the AOSP feature path from item 1.
5. `make_bootanimation.py` accepted any `desc.txt`; it now refuses one the platform's parser
   would not play (part type not `c`, `p` or `f`; a malformed first line; non-square frames).
6. The permission classification table in the test had three protection levels that differ
   from the current framework (`INTERNET`, `ACCESS_NETWORK_STATE`, `WRITE_SECURE_SETTINGS`);
   none changed the privileged set. The table is now held equal to the reference by a test.

## The checks, item by item

| Register item | Test(s) | Proves (on the host) | Does not prove |
| --- | --- | --- | --- |
| E1.a architecture | `test_architecture_names_the_five_layers` | the page names all five layers | that the description is right |
| E2.a product makefile | `test_product_registration_is_consistent`, `test_product_copy_files_sources_all_resolve`, `test_product_packages_and_overrides_resolve_to_known_modules`, `test_every_referenced_file_exists` | `PRODUCT_NAME`, `PRODUCT_MAKEFILES` and both lunch choices name the same product, with `lunch`'s default release config and a legal variant; `PRODUCT_DEVICE` is the inherited Cuttlefish device and the build script checks the same inherited makefile; every `PRODUCT_COPY_FILES` source is a file in `os/` (or, if from `frameworks/native/data/etc/`, one the mirror has); every `PRODUCT_PACKAGES` entry is a module in `os/`; `TARGET_BOARD_INFO_FILE` exists; `en_GB`; allowlist enforcement set | that `device/google/cuttlefish/vsoc_x86_64_only/phone/aosp_cf.mk` exists in the pinned branch (no mirror reachable; the build script stops and names the alternative); that `trunk_staging` is a release config of the checked-out branch; that the phone product's handheld feature file coexists with the watch feature; that the makefile parses in Kati |
| E2.b privapp allowlist | `test_privileged_allowlist_matches_the_merged_aosp_manifest`, `test_allowlist_equals_the_privileged_permissions_in_the_manifest`, `test_allowlist_names_the_app_package`, `test_protection_levels_match_the_aosp_reference` | the main and aosp-flavour manifests, merged as Gradle's merger would (an approximation: union of permissions, components merged by name, conflicting attributes refused; `tools:node` other than `merge` and placeholders not modelled), request no `signature|privileged` permission, so the empty allowlist is exactly right; the package is the aosp `applicationId`; each requested permission's protection level is the framework's | the permissions library manifests add at build time (the build script checks the built APK with `aapt2 dump permissions`); that the platform accepts the file at boot |
| E2.c first-boot grants | `test_first_boot_grants_are_exactly_the_runtime_permissions_requested_and_revocable`, `test_first_boot_grants_are_only_permissions_the_app_requests` | the XML uses only the tags and attributes `DefaultPermissionGrantPolicy` reads (anything else is ignored silently on a device, so here it fails); it grants exactly the seven runtime (`dangerous`) permissions the merged manifest requests, all `fixed="false"`; it is installed to `/product/etc/default-permissions`, a directory the policy reads | that the policy grants the hard-restricted `BODY_SENSORS_BACKGROUND` from this file; the owner's and DPIA sign-off (the file is a proposal) |
| E2.d framework overlay | `test_every_overlaid_resource_exists_in_the_framework_with_its_type` | each of the five overlaid names is a resource in `frameworks/base` `config.xml` at `android16-release`, with the overlay's type (`bool`/`integer`); values well-formed; manifest targets `android`, static, numeric priority, no code | the meaning of `config_longPressOnPowerBehavior` = 1 in the target release; that `config_disableLockscreenByDefault` has the intended effect; that the RRO is enabled at boot (`BUILD.md` step 9) |
| E2.e boot animation spec | `test_boot_animation_spec_is_text_for_the_watch_display`, `test_boot_animation_generator_run_from_the_command_line_matches_the_specification` | `desc.txt` follows `FORMAT.md` (`454 454 30`, `c`/`p` parts); README's frame counts are one and two seconds at that rate | what it looks like |
| E2.f boot animation produced | `test_boot_animation_generator_run_from_the_command_line_matches_the_specification`, `test_the_boot_animation_zip_follows_the_platform_format`, `test_the_boot_animation_is_deterministic`, `test_negative_control_the_generator_refuses_a_desc_the_platform_would_not_play` | the generator runs from the command line here (Pillow 13) and writes a zip whose every entry is stored (`unzip -v` shows `Stored`), `desc.txt` first and verbatim, 30 + 60 PNG frames of 454 x 454 RGB named in play order, the mark inside the 320-pixel safe circle and black beyond it, byte-identical on rerun; the product copies it to `/product/media/bootanimation.zip`, the first path `BootAnimation.cpp` searches | that it is shown at boot (E2.f's title says "shown at boot": that half needs an image) |
| E2.g build script | `test_build_script_preflight_measures_this_machine_and_refuses_when_it_is_short`, the earlier refusal tests | on this machine (4 cores, 15 GB, no KVM) the preflight measures and refuses with exit 2 and downloads nothing; with overrides, each shortfall is named; the opt-in alone does not bypass the preflight | anything after the preflight |
| E2.k launcher | `test_the_merged_aosp_manifest_makes_the_app_the_home_screen_and_help_reachable_when_locked`, `test_the_stock_launcher_is_overridden_and_the_app_is_a_home_app` | the merged aosp manifest gives `MainActivity` MAIN + HOME + DEFAULT, exported, `singleTask`, `showWhenLocked`, `turnScreenOn`; the main manifest alone (wearos) never declares HOME; `Android.bp` overrides `Launcher3QuickStep`, which is a handheld system_ext package in `build/make` | that `overrides` on a product-partition prebuilt removes a system_ext package in Soong; that Android chooses IRA as home without asking |
| E3.a sensor contract | `test_the_hal_service_declares_the_contract_sensors_with_the_contract_rates_and_modes` | the contract names the types the HAL declares, with units and wake behaviour | the contract's power figures (candidate) |
| E3.c, E3.d, E3.e simulated drivers | `test_os_sensor_sim.py` (model built with g++ and run: 1 Hz heart rate 25 to 250 bpm, `NO_CONTACT` off the wrist, gravity in m/s² at rest, a fall over 2.5 g then stillness, 50 Hz gyroscope in rad/s, on-change off-body within 100 ms, deterministic), `test_the_hal_service_declares_...`, `test_the_sensors_hal_version_agrees_between_android_bp_vintf_and_the_aosp_default`, `test_the_hal_service_says_its_readings_are_simulated` | the HAL table declares the four contract types at the contract rates (1 Hz, 50 Hz, on-change) with the AIDL reporting-mode constants (continuous 0, on-change 2, wake-up 1); vendor string "IRA simulated"; `Android.bp`, the VINTF fragment and the AOSP default service agree on version 3 and `ISensors/default`; the init script starts the installed binary | that `SimSensorsHal.cpp` compiles against the AIDL headers (it needs an AOSP tree); the event-queue plumbing; that Cuttlefish's framework takes the service |
| E6.a build instructions | `test_build_doc_commands_name_scripts_and_variables_that_exist`, `test_build_doc_marks_every_step_as_not_run` | every `bash os/scripts/...` in a fenced block is an existing script that passes `bash -n`; every `IRA_OS_*`/`IRA_CF_*` variable the page names is read by a script; the older display-flag form it offers is accepted by the launch script; every step is marked not run | that the steps work |
| E6.b supplier register | `test_supplier_register_has_fourteen_rows_each_with_a_status_from_the_fixed_set`, `test_supplier_register_covers_the_required_components` | 14 rows Q1 to Q14, each with a status from `not obtained`, `requested`, `under NDA`, `obtained`, `not needed`; all `not obtained` | anything about the suppliers |
| launch script | `test_launch_script_refuses_without_kvm_or_the_host_tools`, `test_launch_script_refuses_a_non_cuttlefish_product` | without `/dev/kvm` (this machine) it refuses with exit 4 and launches nothing; a non-Cuttlefish product is refused | the `launch_cvd` flag form the host package accepts |

Every check has a negative control in the same file (`test_negative_control_*`): the same
helper on a deliberately broken copy must report the break. They were run, and they fail on
the original defects (the AOSP feature path, the `2 << 1` flag, a VINTF version that differs
from the library, a status word outside the set).

## What only a real build can show

- That the inherited Cuttlefish makefile and its path exist in the checked-out branch, and
  that `trunk_staging` is one of its release configs.
- That Soong accepts every property in both `Android.bp` files, and that `overrides` removes
  `Launcher3QuickStep` from the image.
- That the image boots with `ro.control_privapp_permissions=enforce`, grants the first-boot
  permissions (including the hard-restricted one), enables the overlay and maps all five
  resources, declares the watch feature, shows no lock screen, and starts IRA as home.
- That the boot animation is drawn, and the display flags the host package accepts.
- That `SimSensorsHal.cpp` compiles and the framework lists its four sensors as "IRA simulated".

`BUILD.md` step 9 is the checklist for that run.
