# Wear OS emulator: the care journey passes (26 November 2026)

The watch app ran through the care journey on a Wear OS emulator for the first time, on GitHub's
standard Linux runner with KVM. The runner is free while the repository is public
([`../COMPUTE_RESOURCES.md`](../COMPUTE_RESOURCES.md)). The earlier attempt on the build machine,
without KVM, is in [`NO_KVM_ATTEMPT.md`](NO_KVM_ATTEMPT.md).

## The passing run

| | |
|---|---|
| Workflow | `.github/workflows/native-checks.yml`, which calls `.github/workflows/emulator.yml` (pull request #119) |
| Run and job | run 45758552085, job 139702865144, commit `ed7ac60` |
| Runner | `ubuntu-latest` (standard), `/dev/kvm` enabled by a udev rule |
| Image | `google/sdk_gwear_x86_64`, Wear OS on Android 14 (API 34), `wearos_large_round` (454 × 454), userdebug |
| Backend | the demo API (`scripts/demo/api.py`) on the runner, fictional seed data, log-only message providers, PostgreSQL and Redis service containers |
| Job time | 04:58:35 to 05:03:10 UTC (4 min 35 s), including the APK build; the emulator booted in 31.4 s |

| Step | Result | Time |
|---|---|---|
| Prepare the device (setup done, reported unplugged, screen kept on) | pass | 2.5 s |
| Install the debug APK | pass ("Success") | 9.9 s |
| Launch (cold start) | pass, 2,969 ms | 3.1 s |
| Pairing screen shown, with its Pair button | pass | 23.8 s |
| Pairing code created on the server, as the coordinator | pass | — |
| Code typed on the watch | pass | 17.0 s |
| Paired: the pairing screen gives way, and the server records the device `paired`, app version 0.1.0 | pass | 3.4 s |
| Home screen with the Help button (after the runtime permissions are granted) | pass | 4.9 s |
| Help tapped: "Help request received" on the wrist | pass | 8.1 s |
| Exactly one open `help_requested` alert for that person on the server, `via: button`, from that device | pass | 8.1 s |

The screenshots, UI dumps, logcat and `results.json` are uploaded as the run's artefact
`wear-emulator-smoke-api34` (kept 14 days).

## Four defects the journey found on the way, all fixed in #119

| Run | What stopped it | Fix |
|---|---|---|
| 45753366726 | The demo backend did not start: its worker scheduler imports `psutil`, which the test requirements leave out | The job installs the version pinned in `requirements.txt`; a test checks it |
| 45755139976 | The app launched, but Wear OS's charging clock (sysui `ChargingActivity`) was on top, because the emulator reports a charger | The smoke reports the device unplugged before launch, and brings the app back once if the clock still appears |
| 45757010887 | The code was typed, but the tap on Pair did nothing (the Wear keyboard was still over the screen, and the tap went to the label) | The keyboard is closed first, the tap goes to the button's clickable node, and it is repeated once if nothing changes |
| 45758000634 | Pair answered "No connection. Try again.": the debug build's server is `http://10.0.2.2:8000`, and Android refuses cleartext HTTP unless the app allows it, so **the debug build could never reach a development server** | A debug-only network security config permits cleartext to 10.0.2.2, localhost and 127.0.0.1 only; release builds keep Android's default; guard tests (`test_watch_cleartext.py`) |

The last one is a product defect, not a test-harness problem. Without the fix, nobody could pair a
debug watch build with a local server, on an emulator or a real watch. Release builds were never
affected: they use HTTPS.

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
