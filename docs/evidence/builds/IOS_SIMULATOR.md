# iOS simulator build of the family app (26 November 2026)

The family app's native iOS project was compiled for the first time, on GitHub's standard
macOS runner. The repository is public, so this runner costs nothing
([`../COMPUTE_RESOURCES.md`](../COMPUTE_RESOURCES.md)).

## The run

| | |
|---|---|
| Workflow | `.github/workflows/native-checks.yml`, which calls `.github/workflows/ios-simulator.yml` (pull request #119) |
| Run and job | run 45755139976, job 139691889793, commit `6152b99`; passed again in run 45758552085 (job 139702865063, commit `ed7ac60`) |
| Runner | `macos-15` (standard, free for public repositories), Xcode 16.4 |
| Time | 04:26:11 to 04:43:13 UTC, 17 minutes |
| Steps | `npm ci`; `npx expo prebuild --platform ios --no-install`; `pod install` (79 Podfile dependencies, 78 pods); `xcodebuild` Debug for `iphonesimulator` with `CODE_SIGNING_ALLOWED=NO` |
| Result | `** BUILD SUCCEEDED **`; `IRACARE.app` in `Debug-iphonesimulator`, zipped to 20,822,008 bytes and uploaded as a 14-day artefact |
| Compiler warnings | 4 lines containing ` warning: ` in the job log (third-party pods; not investigated further) |

## What this shows, and what it does not

- **Shows:** the JavaScript bundle, every native module and the generated Xcode project
  (bundle identifier `care.ira.family`, iOS 15.1 and later; see [`IOS_PREBUILD.md`](IOS_PREBUILD.md))
  compile and link into an iOS app for the simulator. Requirement D2.h, the iOS build, is
  accepted on this evidence.
- **Does not show:** that the app runs. The app was not launched in a simulator in this job, and
  there is no simulator journey yet. The build is unsigned, so it cannot be installed on an
  iPhone. A signed device build and App Store or TestFlight distribution need the Apple
  Developer Program (a paid membership; requirement D2.k, the App Store listing, and the founders' action list). Running
  on a real iPhone is D2.i.

## Repeating it

The job runs by itself on any pull request that changes `mobile/**` (or the workflow files), and
by hand from the Actions tab (`iOS simulator build`, "Run workflow").

## Round 9: launch in the simulator — run, sign-in screen shown (run 45994124214)

The build job now bundles the JavaScript into the Debug `.app` (`FORCE_BUNDLING=1`; the Debug
configuration otherwise expects a development server), and the same job then picks an available
iPhone simulator, boots it, installs `IRACARE.app`,
launches it with its console attached and reads the app's **journey probe**: in development
bundles only (`__DEV__`), `mobile/src/journeyProbe.ts` logs one line per screen change
(`ira-journey {"screen":"sign-in","signedIn":false}`); a release bundle logs nothing
(`mobile/src/__tests__/journey_probe.test.ts` pins both). The launch step records `simulator_booted`,
`installed`, `launched`, `sign_in_screen_shown`, `still_running` and `no_crash_report`, with a
screenshot, the console and the app's unified log (`scripts/simulator/ios_journey.py`;
`supracloud-jarvis/ira/tests/readiness/test_ios_journey.py` covers the simulator choice, the probe
parsing and the crash-report check).

This machine is Linux without Xcode; the pull request's `native checks` workflow runs the job on
GitHub's standard macOS runner, and its results are below. What this job
does not do yet: sign in against a backend (a PostgreSQL and Redis on the macOS runner, as the
Android job has, is the next step), and nothing about a physical iPhone or signed distribution,
which stay blocked on an Apple developer account (D2.i, D2.k).

### First launch in the simulator (run 45910155640, 26 November 2026, 23:38 UTC)

On GitHub's macOS 15 runner: the Debug build with its JavaScript bundled compiled (18 minutes), an
iPhone SE (3rd generation) on iOS 26.2 booted in 123 s, `IRACARE.app` installed (138 s) and
launched, **the process was still running after 97 s and no crash report appeared**: the native
app and its bundled JavaScript start in the simulator. The `sign_in_screen_shown` step failed
because the probe's console line never reached the attached console (one line in 90 s): the
console of an app started with `simctl launch --console-pty` is not a dependable channel. The
probe now also posts each record to a listener the journey script runs on the host's loopback
(`mobile/src/journeyProbe.ts` `PROBE_URL`, development bundles only; a release bundle posts
nothing, pinned by a test), and the script reads that first. The next run is the proof for the
sign-in step.

### Second and third launches: the JavaScript was missing, then the full pass

Run 45980371196 saw no probe on either channel; the app's unified log showed it never loaded
JavaScript, and `main.jsbundle` was absent from the `.app`: `FORCE_BUNDLING` set as an environment
variable does not reach Xcode's bundling phase. It is now passed as a build setting, and a backstop
step runs `npx expo export:embed` into the `.app` so the bundle is there either way. The launch
became a plain `simctl launch` whose answer (`care.ira.family: <pid>`) is recorded, and the app's
log is read by that pid (an attached-console launch had been marked passed while the app never ran).

**Run 45994124214, job 140489106671, commit `fdfa92b`, 27 November 2026: 6 of 6 steps passed.**

| Step | Result |
|---|---|
| `simulator_booted` | iPhone SE (3rd generation), iOS 26.2, 114 s |
| `installed` | 154 s, no error |
| `launched` | `care.ira.family: 48138` |
| `sign_in_screen_shown` | the probe posted `{"screen": "sign-in", "signedIn": false}` to the loopback listener after 21.5 s; 465 lines of app log |
| `still_running` | pid 48138 in `launchctl list` at the end |
| `no_crash_report` | no new crash report |

This is the family app's native shell and its bundled JavaScript starting in the iOS simulator and
drawing the sign-in screen. It is not a sign-in against a backend (the next step, as above), nor a
physical iPhone or a signed build (D2.i, D2.k).

## The authenticated care journey: 16 of 16 (28 November 2026)

**Run 46106258647, pull request 133, commit `0838076`: 16 of 16 steps passed**, after the launch
checks (6 of 6) in the same job. The unsigned build is signed to run locally on the runner (identity
"-", no Apple account), installed in an iPhone simulator, and driven by Maestro against the care
server started on the runner (PostgreSQL with pgvector and Redis from Homebrew, the fictional seed).

| Step | What it proves |
|---|---|
| `care_server_started`, `launched` | the server and the app are up |
| `family_signed_in`, `patient_selected` | the family member signs in and sees the person |
| `person_record_shown`, `plan_shown` | the record with the latest readings; medication and the care plan |
| `help_alert_shown`, `acknowledged_in_app` | Help from the watch becomes the urgent banner; "I'm on my way" leaves the alert acknowledged on the server |
| `appearance_kept_across_restart` | dark mode kept after the app is stopped and started (the keychain keeps the sign-in) |
| `offline_state_shown`, `connectivity_recovered` | with the server stopped the app says alerts may be out of date, never "No open alerts."; it recovers when the server returns |
| `secure_sign_out` | sign-out ends the session on the server and leaves nothing on screen |
| `wearer_signed_in`, `wearer_talked_to_ira` | the wearer is offered Talk and gets IRA's reply |
| `carer_signed_in`, `carer_home_isolated` | the carer's home, with none of the family's or the wearer's screens |

What it took (each from a run's log): scrolling to off-screen fields; a server field typed only on
request; no autocorrect on account fields; the warning bar kept off the tab bar; "I'm on my way"
reachable by VoiceOver; signing to run locally so the keychain works; answering iOS's "Save
Password?" sheet; one signing secret across the journey's server restarts. A simulator, not a
physical iPhone (D2.i).

