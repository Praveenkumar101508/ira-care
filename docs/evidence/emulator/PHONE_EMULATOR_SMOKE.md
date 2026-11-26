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
