# Wear OS emulator without KVM: what was tried and why it did not complete

Run on 26 November 2026 (UTC) on the shared build container: 4 CPUs, 15 GB of memory,
no `/dev/kvm`, and a CPU that does not offer `vmx` or `svm` to the container. The aim was
requirement D1.i (the watch app on a Wear OS emulator through the care journey) and K.g
(emulator runs on integrated main). **Neither is met.** Both stay `blocked_external`.

This is emulator evidence only of what is recorded here. The watch app's JVM unit tests
(171 passed, `testWearosDebugUnitTest`, same day) are a different thing and are not
emulator evidence.

## Set-up

| Item | Value |
|---|---|
| Command-line tools | `commandlinetools-linux-13114758` |
| Emulator | 37.3.10 (build 16970787) |
| Platform tools | 37.0.3 |
| System image | `system-images;android-34;android-wear;x86_64` (Wear OS 5, revision 1) |
| Device fingerprint | `google/sdk_gwear_x86_64/emu64xa:14/AW2A.240422.007.E8/12230326:userdebug/dev-keys` |
| AVD | `wearos_large_round`, 454 × 454 |
| Flags | `-no-window -no-audio -gpu swiftshader_indirect -accel off -no-snapshot -no-boot-anim -cores 2 -memory 2048 -read-only` |
| APK | `watch/app/build/outputs/apk/wearos/debug/app-wearos-debug.apk`, built with `./gradlew assembleWearosDebug testWearosDebugUnitTest` (as CI does; BUILD SUCCESSFUL) |
| Backend | `scripts/demo/api.py` on host port 8000 (the debug build's `10.0.2.2:8000`), its own fresh database, fictional seed, log-only providers |

`watch/app/src/androidTest` does not exist, so there are no instrumented tests to run.

## What happened

1. Without an override the emulator refuses: `Software CPU emulation (-accel off) is not
   supported. A hardware CPU accelerator is required (or set ANDROID_I_WANT_MY_TCG=yes to
   force TCG software emulation).`
2. With `ANDROID_I_WANT_MY_TCG=yes` it ran under QEMU's software CPU (TCG), at about one
   host core. `sys.boot_completed` became 1 after **2 031 s (33.9 minutes)**.
3. During boot and after it, Android's framework watchdog killed `system_server` again and
   again ("Blocked in handler on main thread (main) for 60s"), roughly every four to five
   minutes; 14 different `system_server` process ids were seen in 100 minutes. Each kill
   takes every system service with it (`Can't find service: activity`, `Can't find
   service: input`) and the user goes back to `BOOTING`.
4. Smoke journey (`scripts/emulator/watch_smoke.py`), in the windows between kills:
   - `adb install`: **Success** (199 s the first time, 655 s the second);
   - `am start -W` of `care.ira.watch.ui.MainActivity`: the activity was resumed, but the
     launch reported `Status: timeout` after 56 s: no first frame was drawn;
   - the UI tree (`uiautomator dump`, [`tcg_ui_pairing_dump.xml`](tcg_ui_pairing_dump.xml))
     **did show the pairing screen**: "Pair this watch", "Type the pairing code from the
     family app.", the code field and the Pair button;
   - screenshots, from both `adb exec-out screencap -p` and the emulator's own
     `screenrecord screenshot`, were **entirely black**
     ([`tcg_pairing_screenshot_black.png`](tcg_pairing_screenshot_black.png)): nothing
     reached the display;
   - a pairing code was created on the backend, but before it could be typed
     `system_server` was killed again (`cmd: Can't find service: input`), and in the
     next windows the user never left `BOOTING`, so the activity could not be resolved.
   Steps from the second run: [`tcg_smoke_run2.jsonl`](tcg_smoke_run2.jsonl).
5. Two mitigations were tried and neither stopped the kills: raising
   `system_server_watchdog_timeout_ms` to 30 minutes, and (with `adb root`, possible on
   this userdebug image) enabling JDWP for all processes and holding a debugger
   connection open to each new `system_server`. Log lines:
   [`tcg_emulator_log_excerpt.txt`](tcg_emulator_log_excerpt.txt).

An arm64 image was not tried. Whether this emulator version would start one on an x86_64
host was not checked; if it did, TCG would be translating arm64 instructions, which is
slower than the x86_64 run that already could not keep the framework alive.

The SDK, the image and the AVD (about 5.6 GB) were deleted afterwards.

## Conclusion

Software CPU emulation on this container is too slow for Wear OS 5: the framework never
stays up long enough to pair, and nothing is drawn. What did run: the debug APK installs,
the main activity starts, and its accessibility tree shows the pairing screen. That is
not the care journey and does not move D1.i or K.g.

The journey needs a host with KVM. `.github/workflows/emulator.yml` is ready for that: a
manual-only workflow on a standard Linux runner with KVM enabled, which boots the Wear OS
image with hardware acceleration and runs the same `scripts/emulator/watch_smoke.py`,
including pairing with a real code, the Help button, pressing Help and checking the alert
on the server, and uploads screenshots, UI dumps and logcat. It has not been run yet; it is
manual, and the founder triggers it.
