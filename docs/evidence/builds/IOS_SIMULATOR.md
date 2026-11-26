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
