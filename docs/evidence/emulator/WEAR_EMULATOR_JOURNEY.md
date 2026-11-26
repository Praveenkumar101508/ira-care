# Wear OS emulator: the care journey passes (26 November 2026)

The watch app ran through the care journey on a Wear OS emulator for the first time, on GitHub's
standard Linux runner with KVM. The runner is free while the repository is public
([`../COMPUTE_RESOURCES.md`](../COMPUTE_RESOURCES.md)). The earlier attempt on the build machine,
without KVM, is in [`NO_KVM_ATTEMPT.md`](NO_KVM_ATTEMPT.md).

## The passing run

| | |
|---|---|
| Workflow | `.github/workflows/native-checks.yml`, which calls `.github/workflows/emulator.yml` (pull request #119) |
| Run and job | run 45761957903, job 139713814067, commit `04f34c8` (with the fix for the crash after pairing). Run 45758552085 (`ed7ac60`) passed earlier, before that crash was found |
| Runner | `ubuntu-latest` (standard), `/dev/kvm` enabled by a udev rule |
| Image | `google/sdk_gwear_x86_64`, Wear OS on Android 14 (API 34), `wearos_large_round` (454 × 454), userdebug |
| Backend | the demo API (`scripts/demo/api.py`) on the runner, fictional seed data, log-only message providers, PostgreSQL and Redis service containers |
| Job time | 05:29:47 to 05:34:13 UTC (4 min 26 s), including the APK build; the emulator booted in 28.5 s |

| Step | Result | Time |
|---|---|---|
| Prepare the device (setup done, reported unplugged, screen kept on) | pass | 1.9 s |
| Install the debug APK | pass ("Success") | 6.6 s |
| Launch (cold start) | pass, 2,099 ms | 2.3 s |
| Pairing screen shown, with its Pair button | pass | 28.7 s |
| Pairing code created on the server, as the coordinator | pass | — |
| Code typed on the watch | pass | 14.3 s |
| Paired: the pairing screen gives way, and the server records the device `paired`, app version 0.1.0 | pass | 2.3 s |
| Home screen with the Help button (after the runtime permissions are granted) | pass | 2.5 s |
| Help tapped: "Help request received" on the wrist | pass | 7.6 s |
| Exactly one open `help_requested` alert for that person on the server, `via: button`, from that device | pass | 7.6 s |

The screenshots, UI dumps, logcat and `results.json` are uploaded as the run's artefact
`wear-emulator-smoke-api34` (kept 14 days).

## Five defects the journey found on the way, all fixed in #119

| Run | What stopped it | Fix |
|---|---|---|
| 45753366726 | The demo backend did not start: its worker scheduler imports `psutil`, which the test requirements leave out | The job installs the version pinned in `requirements.txt`; a test checks it |
| 45755139976 | The app launched, but Wear OS's charging clock (sysui `ChargingActivity`) was on top, because the emulator reports a charger | The smoke reports the device unplugged before launch; if a Wear OS screen (the charging clock, or the watch face as in run 45762586497) still comes in front, the app is brought back, up to three times, each noted in the log |
| 45757010887 | The code was typed, but the tap on Pair did nothing (the Wear keyboard was still over the screen, and the tap went to the label) | The keyboard is closed first, the tap goes to the button's clickable node, and it is repeated once if nothing changes |
| 45758000634 | Pair answered "No connection. Try again.": the debug build's server is `http://10.0.2.2:8000`, and Android refuses cleartext HTTP unless the app allows it, so **the debug build could never reach a development server** | A debug-only network security config permits cleartext to 10.0.2.2, localhost and 127.0.0.1 only; release builds keep Android's default; guard tests (`test_watch_cleartext.py`) |

| 45760714043 | Paired, then the app was killed: `SecurityException: Starting FGS with type health ... requires ... any of [ACTIVITY_RECOGNITION, BODY_SENSORS, HIGH_SAMPLING_RATE_SENSORS]`. Pairing started the care service before the permission screen had been shown. The run above passed only because the permissions were granted a moment sooner | Every start of the service goes through a permission check (`ForegroundPermission`, unit tests); the permission screen starts the service once the grant arrives; a guard test keeps every start behind the check (`test_watch_foreground_start.py`, negative control on the old code) |

The last two are product defects, not test-harness problems. Without the cleartext fix, nobody could pair a
debug watch build with a local server, on an emulator or a real watch. Release builds were never
affected by the cleartext one: they use HTTPS. The crash after pairing affected every build: a
wearer who paired before allowing the sensors lost the app until it was opened again.

Since that crash was hidden by a passing journey once, the journey now ends with an eleventh step,
`no_app_crash`, which fails if the device's crash buffer shows a crash of the app.

When a step fails, the smoke script now prints what the device shows into the job log: the visible
text, the packages and focused window, the clickable nodes with their bounds, the keyboard state,
the crash buffer and recent error lines. The artefact is not always reachable by whoever reads the
result.

## What this shows, and what it does not

- **Shows:** the watch app, built as CI builds it, installs, starts, pairs with the real server code
  through the real pairing API, and delivers a Help request that the server records as one open
  alert. Requirement D1.i is verified on this run.
- **Does not show:**
  - the fall countdown, check-ins, speech or sensors: emulator sensors are simulated and the
    journey does not exercise them;
  - battery life, or behaviour on a real watch (D1.j and the device rows);
  - the family phone app on an Android emulator, which the journey does not yet include.
    Requirement K.g asks for both apps, so it stays open.

## Repeating it

The job runs by itself on any pull request that changes `watch/**`, `mobile/**`, `scripts/emulator/**`
or the three workflow files. It also runs by hand from the Actions tab (`Wear OS emulator`, "Run workflow").
