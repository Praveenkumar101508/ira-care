# Round 10: the family phone app in a real browser (28 November 2026)

The phone app's own code built for the web (`npx expo export --platform web`, the same build the
demo serves; D2.e was verified the same way), driven in headless Chromium (Playwright 1.63.0) against
the running care server (`scripts/demo/api.py`, PostgreSQL 16 with pgvector, Redis 7) on the build
machine, not the owner's computer. Each "device" is a separate browser profile. Fictional people
only. Results: `round10_phone_results.json`.

## `e2e/phone_verification.spec.ts`: 5 of 5 passed

| Requirement | Test | What it proves | Result |
|---|---|---|---|
| C-11.b | the phone never shows a silent or unknown watch as worn | the family member's watch card, with the person's watch row set as a silent watch leaves it: worn but no sync for three hours reads "last known on the wrist"; never said reads "wrist unknown" and not "on the wrist"; negative control, worn and just synced, reads "on the wrist" and not "last known"; the row is put back | passed, after the fix below |
| D2.c | a reminder set by the care team reaches the wearer, and Done is recorded | a reminder made by the coordinator through the API appears on the wearer's home; "Done" on the phone leaves it `confirmed` on the server, and it leaves the open list | passed |
| D2.d | a visit planned by the care team reaches the wearer | the wearer's "Next visit" card reads "Verification Carer is coming at" the planned time | passed |
| D2.b | the appearance chosen on one phone is the one the same person sees on another | the family member turns on dark mode on one device: the server keeps it, the first device draws dark, and a second device (a fresh browser profile, the same person signed in) draws dark too; it was light before; put back afterwards | passed |

| C-16.a | the phone renews an expired sign-in instead of signing the family member out | the server restarted with one-minute access tokens; the family member signs in, and over the next 135 s the server refuses the expired token (401) at least once, the app renews it (`/auth/refresh` answered 200), and the home screen stays with no sign-in screen and no "sign-in has expired" notice | passed (2.3 min, run on its own with `E2E_ACCESS_TOKEN_SECONDS=60`) |

## A defect the test found, fixed

**The phone showed a silent watch as worn (C-11.b).** `deviceSummary` (`mobile/src/model/format.ts`)
said "wrist unknown" for a watch that never said, but "on the wrist" for one that said so hours ago and
then went silent: a family member could read an old state as current. The dashboard already said
"last known" after an hour (`dashboard/src/lib/view.ts`, `wristState`). The phone now does the same,
with the same limit (`WRIST_STALE_AFTER_MINUTES = 60`). The unit test (`mobile/src/__tests__/model.test.ts`)
gained the stale cases and fails without the fix; the phone app's 97 tests pass.

## Notes on running it

- The web build keeps no sign-in across a page load, so the tests wait for the app's own 30-second
  refresh instead of reloading.
- The server limits sign-ins to five a minute; runs are spaced a minute apart (a run started straight
  after another stopped at sign-in with the card never shown: the limit working, not a fault).

## What this does not show

The native phone (its journeys on the Android emulator and the iOS simulator, and a device): the web
build shares the app's code, not its platform (no keychain, no native accessibility tree).
