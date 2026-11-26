# Overnight report, 26 November 2026 (round 8)

Written at 12:00 UTC on 26 November 2026 for the founders, after the overnight autonomous
completion directive. Everything here is on `main` at `540c6e5` unless it says otherwise. Money
spent: **£0**. Nothing was bought, rented, upgraded, trialled or committed to.

Two things interrupted the night and are stated up front:

- the build container restarted at about 06:54 UTC, which killed the two long memory-evidence
  soaks that were running and the local Postgres;
- the account's weekly usage limit was reached at 06:49 UTC and lifted at 10:00 UTC, which
  stopped the memory investigation agent before it could finish and delayed this report.

## A. Executive summary

| Measure (`scripts/completion_summary.py`) | Start of the night (`main` after #113, `5e03a2f`) | Now (`main` at `540c6e5`) |
|---|---|---|
| Requirements complete (accepted or better) | 150 of 253 | **154 of 253** |
| Software implementation (hardware-independent) | 170 / 190, 89.5 % | **174 / 190, 91.6 %** |
| Software acceptance | 149 / 190, 78.4 % | **153 / 190, 80.5 %** |
| Software verification | 71 / 205, 34.6 % | **74 / 205, 36.1 %** |
| AI development (area ai, accepted) | 34 / 46, 73.9 % | **35 / 46, 76.1 %** |
| OS development (hardware-independent) | 5 / 28, 17.9 % | 5 / 28, 17.9 % |
| Hardware integration | 0 / 14 | 0 / 14 |
| External and commercial readiness | 1 / 34 | 1 / 34 |
| Blocked by something external | 58 | **54** |

Eight pull requests merged (#115 to #122), each with every required check green; one is open in
draft (#123, memory growth) with its evidence run still in progress. The four requirements that
moved to complete: F4.f (cross-dataset evaluation), D2.h (iOS build), D1.i (watch on a Wear OS
emulator through the care journey) and K.g (both apps on emulators).

What matters most:

1. **The fall-countdown overlap defect is fixed (D-092, #118).** A suspected fall during a running
   countdown is never dropped, never moves the deadline, and an ambiguous or late OK cannot cancel
   a newer emergency. Found by the WEDA-FALL cross-dataset run; fixed on the watch and the server
   with original-fails, fixed-passes tests and mutation checks.
2. **The native apps now run on real Android images, for free.** GitHub's standard runners expose
   KVM and are free for a public repository. The Wear OS watch app pairs through the real API and
   requests help, end to end, on every pull request that changes the apps; the family app's
   release build reaches its sign-in screen on an Android 14 phone emulator; the iOS app compiles
   on a free macOS runner. Running the journey found **two product defects in the watch app** that no
   unit test had: the debug build could never reach a development server (Android refuses plain
   HTTP), and **every build crashed after pairing** if the sensor permission had not yet been granted.
   Both are fixed with guard tests.
3. **The 1-hour soak's memory criterion is still not met.** Two genuinely unbounded growths and a
   per-request reference cycle were found and fixed with regression tests (#123), and growth fell
   (6.65 %/h before; 5.29 %/h on the only post-fix run, cut short at 1 h 04), but 5.29 is above the
   5 %/h limit. A complete 1-hour run on the fixed code is in progress; its figure goes into #123
   whatever it says.
4. **The public repository was reviewed and protected (#121):** no real credential or personal data
   in the tree or the history; data, weights and env files are ignored; a staged-file check runs in
   CI. Two items need the owner: the SisFall-trained v1 weights remain in public history, and the
   conversation dataset's manifest says "proprietary" under an Apache 2.0 repository.
5. **The 24-hour soak (G2.e) remains the one achievable requirement this environment cannot do:**
   the container restarts. The harness is validated; the founder-machine procedure is written.

## B. Overnight tasks

Every merge is a squash of a pull request whose required checks all passed at the merge
(`tests` workflow; `native checks` where the apps changed). Commits are the squash commits on `main`.

| PR | Branch | Merged | Requirement(s) | What it did | Tests and negative controls | CI |
|---|---|---|---|---|---|---|
| #115 | `fix/cross-dataset-evaluation` | `722c4dd`, 02:15 | F4.f verified | The shipped fall pipeline on WEDA-FALL (326 of 350 falls; 20 of the 24 misses were the countdown-overlap defect; 52.9 false alarms an hour on older-adult daily activities) and the heart pipeline on GalaxyPPG (learned 6.18 bpm on the E4, 7.83 bpm on the Galaxy Watch). `scripts/eval/fall_cross_weda.py`, `heart_cross_galaxyppg.py`, `docs/ai/fall/CROSS_DATASET_WEDA.md`, `docs/ai/heart/CROSS_DATASET_GALAXYPPG.md` | `tests/readiness/test_cross_dataset_eval.py` | green |
| #116 | `docs/founder-actions` | `4870e15`, 03:34 | — (founder guides) | Signing keys step by step, branch clean-up list (72 branches), dataset register and candidates (`docs/legal/DATASET_CANDIDATES_2026-11-26.md`), computing resources, market evidence, competitors, projection assumptions, validation interviews | docs-truth and register tests | green |
| #117 | `fix/native-verification-prep` | `2fd818c`, 04:07 | G2.e prepared; D1.i, D2.h, K.g prepared | Wall-clock soak harness (`scripts/soak_wallclock.py`), two 1-hour runs (both FAIL on memory growth only), `HOW_TO_RUN_24H.md`, the no-KVM emulator attempt record, manual emulator and iOS workflows, `watch_smoke.py`, phone-app permission clean-up, `/ios/` and `/android/` ignored | `test_soak_wallclock.py`, `test_manual_workflows.py` | green |
| #118 | `fix/fall-countdown-overlap` | `a2c8a27`, 04:15 | hazard H4; D-092 | See section C | Watch `FallCountdownOverlapTest` (original fails 5 of 7, fix 7 of 7); six mutations each fail tests; server `test_care_fall_overlap_db.py` (original fails 6 of 6); server mutations fail | green |
| #121 | `fix/public-repo-protection` | `428206a`, 04:26 | public-repository protection | gitleaks over 121 refs and 389 commits, detect-secrets over 1,111 candidates, personal-data sweep of 3,684 blobs: nothing real. `.gitignore` blocks, `scripts/ci/check_staged_files.py`, `docs/security/PUBLIC_REPOSITORY.md`, WEDA-FALL licence caveat (`unclear`) | `test_public_repository.py` (33), `test_check_staged_files.py` (15), each rule with a negative control; the guard caught the #117 soak archives on main, allow-listed after a secret scan of their contents | green |
| #120 | `fix/heart-low-confidence-unavailable` | `212fa08`, 04:29 | F3.g; D-094 | An unsure learned heart estimate is reported unavailable rather than replaced by the classical one. Held-out S1: 7.19 bpm on 77.5 % of windows → 4.59 bpm on 60.1 %. An agreement rule was tested and rejected (`scripts/training/heart_fallback_rule.py`) | care 615, heart research 42, dashboard 138; old code fails 6 new tests, old dashboard fails the D-094 test | green |
| #119 | `fix/native-checks-on-pr` | `cd20743`, 06:05 | D1.i verified; D2.h accepted; K.g (watch half); two watch defects | `native-checks.yml` runs the emulator journey and the iOS build on pull requests that change the apps. Six emulator runs found five problems, all fixed (section D). `docs/evidence/emulator/WEAR_EMULATOR_JOURNEY.md`, `docs/evidence/builds/IOS_SIMULATOR.md` | `test_manual_workflows.py` (24), `test_watch_cleartext.py` (4), `test_watch_foreground_start.py` (3, flags all three old start sites), `ForegroundPermissionTest.kt` | green, including both native jobs |
| #122 | `fix/phone-emulator-smoke` | `540c6e5`, 11:47 | K.g verified | The family app's release build on an Android 14 phone emulator: install, launch, sign-in screen, no crash. `docs/evidence/emulator/PHONE_EMULATOR_SMOKE.md` | `test_manual_workflows.py` (29, negative controls for the dialog and retry rules) | green, three native jobs |
| #123 (draft) | `fix/api-memory-growth` | open, `758e353` | G2.e's memory criterion | Section G | 15 new tests; 1,141 in the surrounding suites; 15 database tests | running |

## C. Safety

**D-092, hazard H4 (#118).** How a fall was lost: on the watch, `suspectedFall()` returned nothing
while a countdown ran, and "I'm OK" closed the check without naming an incident; on the server, a
fall start during an unresolved fall alert answered 409 (treated as final by the outbox), "I'm OK"
resolved any asking fall alert, and Help during an open fall SOS opened a second SOS; in the outbox,
a newer event could overtake an older one and two drains could run at once.

The fix: every countdown is an incident with an id carried on every event (`fall-<id>-d<n>`, `-ok`,
`-help`); a detection within 1.5 s of the triggering impact is the same impact; a separate detection
during a countdown is attached to the incident, the watch asks again, and **the deadline does not
move**; "I'm OK" after a separate detection reports the first incident OK and asks again at once with
a fresh countdown; an unclear answer cancels nothing; Help escalates at once; the server takes a
per-person lock, attaches a start to the unresolved alert, resolves only the latest incident (a stale
OK gets 409) and returns the existing SOS for Help. No thresholds, model or fallback changed.
Sensitivity was not weakened; false alarms were not traded for it. Hand-impact false alarms were
analysed only; candidate mitigations are in the safety case for the clinical reviewer.

**Two watch defects found by the emulator journey (#119), fixed:**

- `SecurityException` after pairing on every build: the app started its `health` foreground service
  before any sensor permission was granted, and Android (targetSdk 35) kills the app for that. A
  wearer who paired before allowing the sensors lost the app until it was reopened. Every start now
  goes through `ForegroundPermission`; the permission screen starts the service after the grant; a
  guard test keeps every start behind the check.
- The debug build could never reach a development server: Android refuses plain HTTP. A debug-only
  network security config allows 10.0.2.2, localhost and 127.0.0.1; release builds are unchanged.

**Owed to the clinical safety officer:** review of D-092, the hand-impact false-alarm analysis and
D-091 (the advice guard), as before.

## D. Native evidence

All on GitHub's standard runners, free for a public repository. Nothing was dispatched by hand
(the integration's dispatch call returns 403); every run was a pull-request run.

| What | Result | Runs | Evidence |
|---|---|---|---|
| Wear OS watch app, care journey (Wear OS 14 / API 34, `wearos_large_round`) | **passes**: boot in about 28 s, install, cold start about 2.5 s, pairing screen, code typed, paired through the real API (server records `paired`), home screen, Help on the wrist, exactly one open `help_requested` alert on the server, no crash in the crash buffer (11 steps) | 45758552085, 45761957903, 45763753087, and two runs on #122 | `docs/evidence/emulator/WEAR_EMULATOR_JOURNEY.md` |
| Family phone app, release build, Android 14 phone emulator (`pixel_6`) | **passes**: APK built for x86_64 in 4 min 21 s, boot in 28 s, install, launch, sign-in screen with every expected element in 4.8 s, no crash (4 steps). Release builds refuse plain HTTP, so the smoke does not sign in | 45767465498 and the evidence run on #122 | `docs/evidence/emulator/PHONE_EMULATOR_SMOKE.md` |
| iOS family app, unsigned simulator build (macos-15, Xcode 16.4, 78 pods) | **compiles**: `BUILD SUCCEEDED`, `IRACARE.app`, 20.8 MB zip, 17 minutes. Not launched in a simulator; not signed | 45755139976, 45758552085, 45761957903, 45763753087 and #122's runs | `docs/evidence/builds/IOS_SIMULATOR.md` |

What the emulator journey found on the way, all fixed in #119: `psutil` missing from the demo
backend's install; Wear OS's charging clock over the app (the emulator reports a charger); a tap on
Pair that hit the label under an open keyboard; the two product defects in section C; and Wear OS's
own watch face coming in front after launch. The smoke script now prints what the device shows into
the job log when a step fails, brings the app back when another screen covers it, and fails the run
if the app crashed at any point.

Requirement states: **D1.i verified**, **K.g verified**, **D2.h accepted** (a signed device build still
needs the Apple account, D2.k). Physical devices (D1.j, D2.i and the device rows) remain blocked.

## E. IRA CARE OS

No change tonight: 5 of 28 hardware-independent OS requirements accepted. The remaining ones need
an AOSP build host (about 400 GB of disk, 64 GB of RAM, 8 or more cores, KVM for Cuttlefish). No such
machine is available for free: GitHub's runners have 14 GB of disk; the build container has about
20 GB and no KVM. The route is Google for Startups or AWS Activate credits on an Intel instance
with nested virtualisation (`docs/evidence/COMPUTE_RESOURCES.md`), or Essex Startups' partner
credits (enquiry drafted). Nothing was applied for, because applications need the company's
identity and a billing account, which only the founders can give.

## F. The seven intelligence components

| Component | Tonight | State |
|---|---|---|
| **Conversation** | No change. Gemma 4 E4B / E2B base model behind IRA's rules; the 25 November fine-tune stays unpromoted (D-090: better quality but escalated on 3 of 11 held-out help answers against the base's 9) | In use |
| **Safety rules** | No change to the rules (D-091 narrowed guard stays). D-092 is a safety-logic change on the watch and server, not a model change | In use |
| **Memory** | No change. Consent-controlled per-person store; erasure, retention and isolation covered by the database suites | In use |
| **Fall** | Cross-dataset run on WEDA-FALL (#115): 326 of 350 young-adult falls onto a mattress; 20 of 24 misses were the overlap defect, now fixed (D-092); 52.9 false alarms an hour on older-adult daily activities, nearly all from the model path after a ≥2 g impact, analysed for the clinical reviewer. WEDA-FALL's licence is now recorded as `unclear` (sources conflict): the figures stand as historical and there is no further use until the authors confirm. SisFall stays out of every build | v3 MLP + rule detector in every build (BITS-2 and UCI HAR, CC BY 4.0) |
| **Heart** | Independent evaluation on GalaxyPPG (CC BY 4.0, #115): learned 6.18 bpm on the E4 and 7.83 bpm on the Galaxy Watch. D-094 (#120): an unsure learned estimate is unavailable, not replaced by the classical one; held-out S1 7.19 bpm at 77.5 % → 4.59 bpm at 60.1 %; GalaxyPPG, derived from the same run, E4 2.19 bpm at 82.0 %, Galaxy Watch 1.63 bpm at 66.0 % | Classical in use; learned selector in research mode only (D-086); never alerts |
| **Activity** | No change. WISDM wrist model, macro-F1 0.790, 51 people | Hourly summaries to carers under consent; never alerts (D-084) |
| **Speech** | No change. faster-whisper `small`, WER 4.16 % on LibriSpeech test-clean, WAV only. Older-adult accuracy (F8.c) needs the Mozilla Data Collective account (Common Voice, CC0) | In use |

None is validated on older adults or on the IRA watch. IRA CARE is not a medical device. No new
training ran on Kaggle; no dataset was downloaded, redistributed or added to the repository.

## G. Reliability

- **Wall-clock soak harness (#117):** validated by two 1-hour runs against the demo on this
  container. Every criterion held (availability 1.0, p95 latency about 16 ms, no server errors, no
  lost or duplicate alerts, outbox drained, worker heartbeat) **except the API's resident memory: 6.65
  and 6.45 % an hour against a 5 % limit.** `docs/evidence/soak/WALLCLOCK_1H.md`.
- **Investigation (#123, draft):** three causes found, each with a regression test and a negative
  control: the consumed execution-ticket set grew for the life of the process (now bounded, replay
  protection unchanged); every escalation pass re-read every push attempt that ever had a receipt
  (now only the unrecorded ones); Starlette's `BaseHTTPMiddleware` left about 51 cyclic objects per
  request with a body (both middlewares are now plain ASGI). Evidence so far, honestly: a 3-hour run
  on the first two fixes was cut to 2 h 04 by the restart and showed 6.23 %/h; a 2-hour run with all
  three was cut to 1 h 04 and showed 5.29 %/h; two 30-minute profiling runs showed 1.0 and 3.0 %/h
  but are too short to count. **The criterion is not yet shown to be met.** A complete 1-hour run on
  the fixed code merged with `main` started at 11:48 UTC on this container; its summary goes into
  #123 before it leaves draft.
- **24-hour soak (G2.e): not started**, because this container restarted during the night and has
  no persistent host. `docs/evidence/soak/HOW_TO_RUN_24H.md` is the founder-machine procedure; a
  GitHub runner cannot do it (6-hour job limit).

## H. Financial

- **Spent: £0.** No hardware, servers, cloud services, GPU time, memberships, subscriptions, trials
  or payment details. No paid or larger runners. No paid API. No Kaggle training.
- **Billable resources touched:** none. All compute was this build container and GitHub's standard
  hosted runners, which are free while the repository is public (verified on GitHub's billing page
  for Actions, `docs/evidence/COMPUTE_RESOURCES.md`).
- **GitHub Actions used today (26 November, 00:00 to 12:00 UTC):** 43 workflow runs, about **748
  Linux minutes and 153 macOS minutes**, counted from job timings. Free at the public-repository rate.
- **Still to check by the owner:** November's usage before the repository went public was about
  6,100 Linux minutes, above the Free plan's included amount; CI kept running, so a payment method
  or budget may be in place. The billing API is not reachable from here.

## I. Founder actions

Each item: why, what was tried, the free alternative, the steps, where, whether it is needed before
the endorsement application, and whether it can wait until investment.

| # | Action | Why | Tried here | Free alternative | Steps | Before endorsement? | Can wait for investment? |
|---|---|---|---|---|---|---|---|
| 1 | Check Settings → Billing → Usage for GitHub Actions in November | About 6,100 Linux minutes were used while the repository was private, above the Free plan's US$12; a charge may be pending | The billing API returned no access | None needed; the repository is now public, so future runs are free | GitHub → profile → Settings → Billing and licensing → Usage | No | **No**: check this week |
| 2 | Enable secret scanning, push protection and Dependabot alerts | The repository is public; these are free for public repositories and were not readable from here | Settings not readable by the integration | Free | Repository → Settings → Code security → enable all three | No | No: five minutes, do now |
| 3 | Decide on the SisFall v1 weights in public history | The SisFall-trained model (excluded from every build since #99) is still in the repository's history under a licence that may not allow it; the repository is public | Nothing: rewriting history needs your separate authorisation | Ask the SisFall authors (draft 4 below) and, if needed, rewrite history with `git filter-repo` and force-push once | Read `docs/security/PUBLIC_REPOSITORY.md`; decide; if rewriting, every clone must re-clone | Advisable before showing the repository | No: it is a live exposure |
| 4 | Review and send the five Gmail drafts (SisFall, KFall, UMAFall, FallAllD via DataPort, Essex Startups) | Dataset permissions and a possible compute sponsor; nothing was sent, by your rule | Drafted and verified; recipients could not all be verified from here | Free | Gmail → Drafts, in `pk.kamineti1999@gmail.com`; read, correct, send | Essex: helpful; datasets: no | Datasets: yes; Essex: better now |
| 5 | Fix the conversation dataset manifest's licence line | `docs/ai/conversation/dataset/MANIFEST.json` says "proprietary" in an Apache 2.0 repository; a reader will trust one or the other | Nothing: it is your licensing decision | Free | Decide (Apache 2.0, CC BY 4.0, or keep proprietary and move it out); one-line change, or tell this session | Advisable | No |
| 6 | Create an IEEE DataPort login and download FallAllD | A second wrist fall dataset under CC BY 4.0 (15 people, 238 Hz) for cross-dataset evaluation | The page was read; download needs a login | Free account | ieee-dataport.org → doi:10.21227/bnya-mn34 → sign in → download; put it outside the repository and tell this session the path | No | Yes |
| 7 | Create a Mozilla Data Collective account and accept the Common Voice terms | Older-adult speech accuracy (F8.c) has no other commercially licensed source | Search-index reading only; the terms must be accepted in a browser | Free account | datacollective.mozillafoundation.org → account → Common Voice English 27.0 → accept terms → save a copy of the terms into `docs/legal/` | No | Yes |
| 8 | Read the Kaggle terms and decide on company use | The terms describe the services as for personal, non-commercial use; past training used Kaggle; none since | Nothing more to do here | Not needed; the shipped models do not depend on Kaggle | kaggle.com/terms; decide; tell this session | No | Yes |
| 9 | Open the UMAFall and UP-Fall terms pages | Both could add older-adult or wrist test data; their terms could not be read from here | Pages refused or not found from the build environment | Free | Visit the pages; paste the licence text or verdict into `docs/legal/` | No | Yes |
| 10 | Run the 24-hour soak on a founder machine | G2.e is the one achievable requirement this environment cannot do; the container restarts | Two attempts here; the harness and the procedure are done | Any laptop left on for a day (4 cores, 8 GB) | `docs/evidence/soak/HOW_TO_RUN_24H.md`; copy `summary.json` and `checkpoint.jsonl` into `docs/evidence/soak/` | No | Before a pilot, not before investment |
| 11 | Create the watch release signing key | Needed before any signed build goes on a physical watch | Nothing: keys are made only on a founder-controlled machine | Free (`keytool`) | `docs/security/SIGNING_KEYS_STEP_BY_STEP.md`, section 1; two encrypted backups | No | Until a physical watch exists |
| 12 | Apply for Google for Startups (Start tier) or AWS Activate credits | The IRA CARE OS build and boot need a 64 GB, 500 GB, KVM host; credits are the free route | Programmes researched; applying needs the company identity and a billing account | None other than the Essex enquiry (item 4) | `docs/evidence/COMPUTE_RESOURCES.md`; apply with the company domain email; set a budget alert; stop the VM between builds | No | Yes, unless an OS demo is wanted sooner |
| 13 | Delete the two remaining obsolete branches: `praveen/tender-carson-8yjrj4` (covered by #44) and `docs/business-research` (merged in #116) | Tidiness; they hold nothing unmerged | `git push --delete` and the API both return 403 for this session | Free | GitHub → Branches → delete; or `git push origin --delete <branch>` | No | Yes |
| 14 | Clinical safety officer's review of D-092, the hand-impact false-alarm analysis and D-091 | Fall and advice behaviour changed without clinical sign-off; the hazard log requires it | Nothing: needs a qualified person | None | `docs/safety/` hazard log H4 and H7; `docs/DECISIONS.md` D-091, D-092 | No, but it is part of the endorsement story | Before a pilot |
| 15 | Decide D-004: bring over `scripts/security/published_credential_digests.py` or retire its test | `test_the_table_matches_a_fresh_scan_of_history` fails on every full clone because the script never came across from IRA | Documented in `VERIFICATION.md` since round 7 | Free | Tell this session which | No | Yes |
| 16 | Apple Developer Program, Google Play Console | Signed iOS and Play distribution (D2.j, D2.k) | Nothing: paid memberships | None | When ready to distribute | No | **Yes**: paid |
| 17 | Ask the WEDA-FALL authors to confirm commercial use | The licence sources conflict (`unclear` in the register); F4.f's figures stand as historical | No draft exists (it is not one of the five, so none was created) | Free | A GitHub issue or email on <https://github.com/joaojtmarques/WEDA-FALL>; this session can draft it on request | No | Yes |

## J. GO / NO-GO

| Use | Decision | On what |
|---|---|---|
| Internal engineering demonstration | **GO** | Everything in `docs/progress/VERIFICATION.md`, round 8 |
| Investor demonstration | **GO** | Fictional people, test providers, the labelled browser watch; the emulator recordings and screenshots as artefacts |
| Innovator Founder technical demonstration | **GO** | The same, with the technical endorsement report and this report |
| Native app demonstration | **GO on emulators; NO-GO on physical devices** | The Wear OS journey and the phone sign-in screen run on GitHub's runners on every app change (artefacts: screenshots, UI dumps, logcat). A live demonstration needs a machine with KVM (Android emulator) or a Mac (iOS simulator; the app compiles but has not been launched in one). No signed build, no device |
| Virtual OS demonstration | **NO-GO** | No OS image has been built; it needs the build host (section E) |
| Physical-device testing | **NO-GO** | Needs devices and the release signing key (actions 11, 16) |
| Supervised care pilot | **NO-GO** | Clinical safety officer (action 14), MHRA view, devices, live providers, older-adult accuracy for every model, the 24-hour soak (action 10), the memory criterion (#123) |
| Commercial release | **NO-GO** | The pilot's needs, plus store accounts and signed builds, hosting, an independent penetration test, the OS image |
| Unsupervised real-world care | **NO-GO** | Not in scope until the above hold over time |

## Where the figures come from

`python3 scripts/completion_summary.py` on `main` at `540c6e5`; `docs/progress/VERIFICATION.md`
(round 8) for the integrated test runs; the pull requests named above for each change's own tests;
the GitHub Actions runs named in section D for the native evidence. The completion history
snapshot for this round is recorded once #123 is settled.
