# Family phone app on an Android phone emulator (26 November 2026)

The family app's Android release build ran on an Android phone emulator for the first time, on
GitHub's standard Linux runner with KVM (free while the repository is public).

## The passing run

| | |
|---|---|
| Workflow | `.github/workflows/native-checks.yml`, which calls `.github/workflows/phone-emulator.yml` (pull request #122) |
| Run and job | run 45767465498, job 139731733257, commit `a822ace`; a `pull_request` run builds the pull request merged into `main` |
| Build | `expo prebuild` then `assembleRelease -PreactNativeArchitectures=x86_64`: 4 min 21 s; debug-signed, JavaScript bundled |
| Image | `google/sdk_gphone64_x86_64`, Android 14 (API 34), `pixel_6` profile; booted in 27.8 s |
| Job time | 06:20:08 to 06:26:38 UTC (6 min 30 s) |

| Step | Result | Time |
|---|---|---|
| Install the release APK | pass ("Success") | 5.8 s |
| Launch from the launcher | pass | 1.0 s |
| Sign-in screen: the explanation, Username and Password fields, the Sign in button | pass | 4.8 s |
| No crash of `care.ira.family` in the device's crash buffer | pass | 0.1 s |

Visible on the screen: "IRA CARE", "Sign in with the account your care circle gave you.",
Username, Password, Sign in, "I have an invitation link", Advanced, and "Family see alerts and
summaries. Conversations stay private unless the person agrees."

## On the way

The first run (45766023517) built, installed and launched the app. Then the emulator's own
launcher hung ("Pixel Launcher isn't responding"), and its dialog covered the screen; the app had
not crashed. Now, while waiting for the sign-in screen, the smoke answers Wait on another app's
dialog and brings the app back if something else is in front, at most three times, each noted.
If our own app stops responding, the smoke does not wave it through.

## What this shows, and what it does not

- **Shows:** the release build of the family app, its native modules and its bundled JavaScript
  install and start on Android 14 and draw the first screen without crashing. With the watch
  journey ([`WEAR_EMULATOR_JOURNEY.md`](WEAR_EMULATOR_JOURNEY.md)), this verifies requirement K.g
  (emulator runs of both apps on integrated `main`).
- **Does not show:** signing in, alerts, or any screen after sign-in on Android. Release builds
  refuse plain HTTP, so the smoke cannot reach a local demo server. Those flows are covered by the
  app's unit tests and the browser journey, not on an emulator. Running on a real phone is D2.i.

## Round 9: the authenticated journey — written and dry-run, not yet run on the emulator

`scripts/emulator/phone_smoke.py` keeps its four steps (install, launch, sign-in screen, no crash)
and, given `--api` and `--credentials`, continues through the care journey against the demo
server. **No emulator run of these steps has happened yet**: the build machine has no KVM. The
pull request's `native checks` workflow runs `phone-emulator.yml` on GitHub's standard runner and
its results are to be added here.

### Plain HTTP and the release build: the decision

The release APK refuses cleartext HTTP (Android's default since API 28), so it cannot reach the
demo server at `http://10.0.2.2:8000`, and the release policy is not to be weakened for a test.
A TLS proxy with a certificate the release build trusts would need a certificate in the
emulator's *system* store; on API 34 that store lives in an updatable APEX, and writing to it is
a fragile root-only trick. The honest route taken: the journey runs on the **debug** build, whose
generated manifest already allows cleartext (`android/app/src/debug/AndroidManifest.xml`, Expo's
own template, debug source set only), with its JavaScript bundled so no development server is
needed (`scripts/emulator/android_bundle_debug.sh` empties `debuggableVariants` in the generated
`build.gradle`; it touches nothing of the release variant). The release APK is still built and
still runs the sign-in-screen smoke, so a release policy change would be seen. The same approach
as the watch's debug-only network security config (run 45758000634 above).

### Steps and what a pass proves

| Step | What a pass proves |
|---|---|
| `family_signed_in` | The fictional family member signs in with the seeded account (cloud sign-in, then the workspace if asked) and reaches the home tab |
| `patient_selected` | The person card names the person the circle is about |
| `person_record_shown` | The care record shows the person's name and every latest reading the API reports, with the label the app draws (`adbkit.reading_label`) |
| `plan_shown` | The Plan tab shows each active medicine as the app writes it and the care plan card (its goals when the plan has any) |
| `help_alert_shown` | A Help request from the watch (device token, idempotency key) becomes the urgent banner within the app's 30 s refresh, and the server has exactly one open Help alert |
| `acknowledged_in_app` | "I'm on my way" on the banner leaves the alert `acknowledged` on the server |
| `appearance_kept_across_restart` | Dark mode chosen in Settings is still selected after `am force-stop` and a relaunch |
| `offline_state_shown` | With Wi-Fi and data off the home screen says the server cannot be reached and never says "No open alerts." (G2.i) |
| `connectivity_recovered` | With connectivity back, "No open alerts." returns and the notice goes |
| `secure_sign_out` | Every session the app held is revoked on the server after Sign out (the script's own sessions told apart by user agent); Back shows nothing of the account; a relaunch shows the sign-in screen |
| `wearer_signed_in`, `wearer_talked_to_ira` | The wearer's account sees Talk to IRA, sends one line and receives IRA's reply (the deterministic model in CI; C-7.b) |
| `carer_signed_in`, `carer_home_isolated` | The coordinator's home shows the carer's cards and none of the family experience's (C-14.a) |

### Proved without a device (26 November 2026, this machine)

- The pure helpers: `supracloud-jarvis/ira/tests/readiness/test_phone_journey_helpers.py`
  (12 tests): test ids bare or package-qualified, shell escaping for `input text`, JWT claims, the
  app's sessions told from the script's, reading and medication labels, and every step's check
  (person record, plan, offline, recovered, carer home, nothing revealed, revocation, screenshot
  pixel). The not-responding recovery and its button choice: `test_manual_workflows.py`.
- The server side, `--api-only`, against `scripts/demo/api.py` on port 8792 with its own database
  `ira_phonejourney` (migrated to 123, seeded): `api_status` (2 latest readings), `api_plan`
  (2 medicines, a plan without goals), `api_help_and_acknowledge` (one Help alert opened with the
  device token, acknowledged by the family member: `acknowledged`), `api_sessions_listed`: **4 of 4
  passed**. The first run found the wrong device header (`Authorization` instead of
  `X-Device-Token`, 401) and the wrong idempotency header name; both fixed before the run above.
- `scripts/emulator/android_bundle_debug.sh` run on a locally generated project
  (`npx expo prebuild --platform android`): inserts one line inside `react { }`, is idempotent, and
  the generated debug manifest carries `usesCleartextTraffic="true"` while the main manifest does
  not. The debug build itself was not compiled here (no Android SDK at the time of writing).

### First emulator run of the authenticated journey (run 45910155640, 26 November 2026, 23:27 UTC)

On GitHub's standard runner (Pixel 6 image, API 34, KVM), the debug build with its JavaScript
bundled: **11 of 16 steps passed** on the first run. Passed: install, launch, the sign-in screen
on the release build (the smoke) and on the debug build; `family_signed_in` (23 s); `plan_shown`
(the two seeded medicines and the care plan card); `help_alert_shown` (the watch's Help reached the
urgent banner in 16.5 s); `acknowledged_in_app` (the alert `acknowledged` on the server 3.6 s after
"I'm on my way"); `appearance_kept_across_restart` (dark mode kept after `am force-stop`);
`offline_state_shown` and `connectivity_recovered` (the out-of-date notice with Wi-Fi and data off,
"No open alerts." back when restored); no app crash.

Failed, all three for two journey-script faults and not for an app defect: `patient_selected` and
`person_record_shown` looked for "Ada Hartley" where the app shows the preferred name "Ada" (the
seed file carries only the display name; the record is now read for the preferred name);
`secure_sign_out` never found the Sign out button, which lives inside the Account section of
Settings and below the fold (the journey now opens that section and scrolls), so the session stayed
live and the wearer's and the carer's sign-ins that follow could not start. The next run is the
proof for those five steps.

### Second emulator run (run 45974160883, 27 November 2026, 09:39 UTC)

**16 of 17 steps passed.** With the two script faults fixed: `patient_selected` and
`person_record_shown` (the preferred name and both latest readings), `secure_sign_out` (one live
app session before Sign out, none after; Back shows nothing; a relaunch shows the sign-in screen),
`wearer_signed_in` and `wearer_talked_to_ira` (the wearer's home, Talk, one line sent and IRA's
streamed reply shown, C-7.b) all passed, beside everything that passed before. The one failure,
`carer_signed_in`: the journey began the carer's sign-in while still on the wearer's Talk screen,
which hides the tab bar, so Sign out was never reached. The journey now goes back until the tab bar
shows. No app defect was found by either run.

### Third emulator run (run 45977153362, 27 November 2026, 10:02 UTC)

**16 of 17 again**, every step through the wearer's Talk passing a second time (and the launcher
hang now answered: Wait, then Close app, then the app in front). The carer step failed for a new
reason: the system Back key on the Talk screen left the app, so the carer's sign-in began on the
phone's home screen. The journey now taps the app's own Back button and brings the app to the front
if it has left. Still no app defect found.

