# IRA CARE: final report

## Round 4 (the polish, 24 November 2026)

Four phases, each one branch and one pull request squash-merged into `main` with CI green,
each started from the latest `main`. Everything below is what a test, a measurement or a
recording in this repository shows.

### Round 4: the merges

| Phase | Branch | Pull request | What it delivers |
| --- | --- | --- | --- |
| R4-1 | `feat/fast-conversation` | 37 | The model kept loaded, one model in memory, a shorter prompt and context, `gemma4:e4b` then `gemma4:e2b`, sentences streamed, an instant acknowledgement, check-in questions prepared five minutes ahead (D-069 to D-071) |
| R4-2 | `fix/watch-design-renders` | 38 | One look for the watch (graphite case, coral Help button, the app's colours) everywhere; the exploded view in stack order with legible labels; eight renders, the 20-second turntable, glTF and viewer rebuilt (D-072) |
| R4-3 | `feat/demo-film-polish` | 39 | The film at 1080p with IRA's own words; four conversation faults and one wording fault found while recording, each fixed with a test (D-073) |
| R4-4 | `docs/polish-final` | 40 | The documents in line with the merged code; the Kaggle GPU measurement; this section |

### Round 4: the conversation, timed and scored

On the deployment-sized machine (4 vCPU Intel Xeon 2.10 GHz, 15.7 GB, no GPU, Ollama 0.40.18),
`docs/MODEL_CHOICE.md` ("Round 4"):

- **Quality, 162-turn evaluation:** `gemma4:e4b` 0.909 (the best measured), red-flag recall
  1.00; `gemma4:12b` 0.895; `gemma4:e2b` 0.882; `gemma4:e4b-it-qat` 0.844; `qwen3.5:2b` 0.834.
  Chosen: `gemma4:e4b`, then `gemma4:e2b`, then the set lines.
- **First words:** at once (0.0 s on the server; under 0.1 s through the talk endpoint on
  staging), from the rules' acknowledgement. Target 1.5 s p50: met.
- **First sentence from the model:** 2.8 s p50 (5.9 s p95) on the evaluation set; 3.5 to 5.5 s
  on whole talk turns on the staging stack (about 270 prompt tokens, read again in full on
  every turn). Target 3 s p50: met on the evaluation set, **not met** on a whole talk turn;
  round 3 took about 10 s, and 15 s when the model had unloaded.
- **On a small GPU (Kaggle's free Tesla T4, measured):** `gemma4:e4b` first sentence 0.18 s
  p50, `gemma4:12b` 0.94 s p50 (`docs/evidence/training/gpu_bench.json`). Server prices with a
  20 to 24 GB card, with sources, are in `docs/MODEL_CHOICE.md`.

### Round 4: what recording the film found

Phase 1's evaluation timed the model directly; the film timed the product. Recording against
the real model on staging showed that every talk turn was being answered from the set lines,
for four reasons, each now covered by a test that fails without its fix (D-073): a cold model
never loaded (each turn aborted its load, and the second model's load pushed the first out);
the warm request loaded the model with a different context, so the first turn loaded it again;
the 3-second first-token budget was shorter than a real turn's prompt takes on four cores (now
8 s); and the browser watch read the whole reply before speaking, holding the acknowledgement
back. A fifth fault was wording: "Have you taken your the evening tablet yet?".

### Round 4: where to look

- The film: `docs/film/ira_care.mp4` (1920 × 1080, 5 min 6 s, 15.2 MB); its words and how it is
  remade: `docs/film/NARRATION.md`.
- The renders: `docs/hardware/renders/` (`hero.png`, `front.png`, `side.png`, `back.png`,
  `exploded.png`, `section.png`, `wrist_dock.png`, `screen_home.png`); the turntable:
  `docs/hardware/turntable.mp4`.
- The viewer: `docs/hardware/viewer.html` (one file; open it in any browser, drag to turn,
  Explode to separate the layers, tap a part).
- The measurements: `docs/MODEL_CHOICE.md`, `docs/evidence/eval/round4/`,
  `docs/evidence/training/gpu_bench.json`, `docs/evidence/staging/clean_clone_proof.json`.

### Round 4: test counts

- CI on every pull request: backend lint, backend unit tests, backend database tests, the
  dashboard (type check, tests, build), the secret scan and the readiness check, all green
  before each merge.
- Local, phase 3: care unit tests 159 passed; care database tests against PostgreSQL 16 with
  pgvector 366 passed (8 skipped); dashboard 36 passed.
- Staging from a clean clone of the merged phase 3 tree, with the end-to-end browser tests:
  passed in 186 s.

### Round 4: needs the owner

- Delete the merged branches `feat/fast-conversation`, `fix/watch-design-renders`,
  `feat/demo-film-polish` and, once pull request 40 is merged, `docs/polish-final` (this
  session cannot delete branches).
- Decide whether the first sentence should be faster on a whole talk turn: a shorter talk
  prompt, or a GPU server (`docs/MODEL_CHOICE.md`, "What a small GPU server would give").
- Watch the film and the renders and say whether the look is the one wanted; the colours are
  in one place (`LOOK` in `scripts/hardware/build_watch.py`) and the renders, turntable,
  viewer and film are remade from scripts.
- The round 3 owner items below still stand.
- Pull request descriptions: the tool line added to each of pull requests 37 to 40 was
  removed; none is left.

---

## Round 3 (the completion run, 23 and 24 November 2026)

Seventeen phases, each one branch and one pull request squash-merged into `main` with CI
green, in order. Everything below is what a test, a measurement or a recording in this
repository shows; nothing is rounded up. Pull request numbers and test counts are filled in
from the merges and the clean-clone run at the end of the round (section "Round 3: test
counts").

### Round 3: what was built, per phase

| Phase | Branch | What it delivers |
| --- | --- | --- |
| 0 remove-demo-state | `fix/remove-demo-state` (pull request 20) | No demo state in the repository; per-run demo passwords and keys; gitleaks over the whole history in CI |
| 1 trustworthy-sos | `fix/trustworthy-sos` (21) | The wrist shows the server's SOS state; a durable outbox with idempotency keys; every heart-rate sample kept at its own time; the sample-to-alert delay measured |
| 2 care-push-isolation | `fix/care-push-isolation` (22) | Push reaches the circle's registered phones only; tickets, receipts, delivery state on the alert |
| 3 responder-follow-up | `fix/responder-follow-up` (23) | "I'm on my way" is a promise with a deadline; an alert closes only with an outcome |
| 4 private-model | `feat/private-model` (24) | No hosted model API; an Ollama chain of two open models chosen by a 150-turn evaluation on CPU |
| 5 own-models | `feat/own-models` (25) | A trained fall detector in the watch; an answer classifier behind a default-off switch; a conversation fine-tune measured and not adopted; every dataset's licence recorded |
| 6 scheduled-care | `feat/scheduled-care` (26) | Visits with late and missed detection; arrival pauses check-ins and buzzes the watch; the visit record |
| 7 ira-personal | `feat/ira-personal` (27) | Communication profile, real reminders with a two-step dose, hydration prompts, editable memory, labelled concerns, redirection on a repeated worry |
| 8 watch-platform-layer | `feat/watch-platform-layer` (28) | One watch app for Wear OS and plain AOSP behind platform interfaces; CI proves the AOSP flavour carries no Google service |
| 9 hardware-blueprint | `feat/hardware-blueprint` (29) | A Blender model of the watch, eight renders, a turntable, a glTF viewer for a phone, candidate components checked against datasheets, the blueprint with the UK certification roadmap |
| 10 one-mobile-app | `feat/one-mobile-app` (30) | One phone app with three experiences (wearer, family, carer), four tabs, appearance per person synced across devices, guided watch set-up |
| 11 dashboard-care-record | `feat/dashboard-care-record` (31) | A live overview and a person record read in a coordinator's order; recorded and received times with stale states; alerts with delivery, follow-up and outcome; richer reports |
| 12 clean-product-ui | `feat/clean-product-ui` (32) | One design system; the watch on a clean page with presenter controls apart; no developer words on any screen (tested); every screen captured at phone and desktop |
| 13 founder-demo-data | `feat/founder-demo-data` (33) | Demonstration data names only the two builders as real people; made-up wearers, example.com addresses, fiction-range telephone numbers (tested) |
| 14 staging-proof-and-film | `feat/staging-proof-and-film` (34) | Staging from a clean clone the deployment's way; five journey tests and two outage drills in a browser; six defects found and fixed; the film |
| 15 cost-cuts | `ci/cost-cuts` (35) | CI runs what a change can affect and never tests an identical tree twice; caches |
| 16 truth-sweep-and-final | `docs/truth-sweep-and-final` (36) | Every readiness row in one of three states, checked in CI; this report |

### The conversation model

Chosen by measurement on the 4 vCPU, 15.7 GB machine that ran this round, 150 turns each,
no GPU (`docs/MODEL_CHOICE.md`, `docs/evidence/eval/`):

| Model | Quality | Red-flag recall | Advice refused | Injection resisted | First sentence p50 / p95 |
| --- | --- | --- | --- | --- | --- |
| `gemma4:12b` (primary) | 0.86 | 1.00 | 0.92 | 1.00 | 10.0 s / 12.2 s |
| `gemma4:e4b` (secondary) | 0.78 | 1.00 | 0.88 | 0.95 | 4.6 s / 6.6 s |
| `qwen3.5:9b` | 0.78 | 1.00 | 0.88 | 0.95 | 6.5 s / 9.5 s |
| `qwen3.5:4b` | 0.73 | 1.00 | 0.92 | 0.90 | 4.4 s / 9.9 s (2 fallbacks) |
| `gemma4:26b-a4b` | did not load in 15.7 GB | | | | |
| the rules alone | 0.97 | 1.00 | 1.00 | 1.00 | none |

### Our own models

| Model | Before | After | Kaggle GPU time | Shipped |
| --- | --- | --- | --- | --- |
| Fall detector (watch, TensorFlow Lite, 17.9 kB) | rule detector: sensitivity 0.906, specificity 0.984, 344 false alarms a day | sensitivity 0.990, specificity 0.9998, 3.8 false alarms a day, on 21 unseen subjects | 186 s | yes, beside the rules |
| Answer classifier (server) | rules alone: 147 of 150 eval turns | with the classifier at its gates: 147 of 150 (144 and five false alarms without the gates) | 48 s | behind `IRA_CARE_CLASSIFIER`, default off |
| Conversation fine-tune (QLoRA on gemma4:e4b) | base model: 0.94 rule pass rate on 50 held-out prompts | 0.92; training loss 0.321 to 0.369 | about 2.6 h over four attempts | no |

### Datasets and licences

`docs/DATASETS.md` lists every dataset surveyed with its source and licence. Trained on:
SisFall (CC BY 4.0) and UCI HAR (CC BY 4.0) for the fall detector; the product's own
synthetic answers for the classifier; synthetic conversations generated by the base model
for the fine-tune (no public dialogue set with a commercial licence exists; EmpatheticDialogues
is CC BY-NC and was not used). UMAFall and FallAllD stay unused until the owner can read their
original terms.

### Sample-to-alert delay

Measured through the browser watch against the real server (`docs/evidence/sample_to_alert.json`):
three heart-rate samples outside the range uploaded at once; the question "Are you OK?" was
asked 0.028 s after the upload and visible on the watch 0.036 s after; with no answer the SOS
opened 106.3 s after the upload (the ask-first wait is 60 s plus the monitoring pass).

### Where to look

- The watch's design: `docs/hardware/viewer.html` (open it on a phone), `docs/hardware/renders/`,
  `docs/hardware/turntable.mp4`, `docs/hardware/BLUEPRINT.md`, `docs/hardware/COMPONENTS.md`.
- Every screen: `docs/screens/INDEX.md` (68 screenshots, phone and desktop).
- The film: `docs/film/ira_care.mp4` (5 min 30 s, 3.9 MB) and its words in `docs/film/NARRATION.md`.
- The staging proof: `docs/evidence/staging/` and `scripts/staging/clean_clone_proof.sh`.
- CI minutes before and after: `docs/evidence/ci/minutes.json`.

### Round 3: the merges

| Phase | Pull request | Merged | CI |
| --- | --- | --- | --- |
| 0 | 20 | 23 November 2026 | green |
| 1 | 21 | 23 November 2026 | green |
| 2 | 22 | 23 November 2026 | green |
| 3 | 23 | 23 November 2026 | green |
| 4 | 24 | 24 November 2026 | green after the browsing tests followed the closed path |
| 5 | 25 | 24 November 2026 | green (landed as a merge commit, not a squash: the merge tool ignored the method once) |
| 6 | 26 | 24 November 2026 | green |
| 7 | 27 | 24 November 2026 | green |
| 8 | 28 | 24 November 2026 | green |
| 9 | 29 | 24 November 2026 | green |
| 10 | 30 | 24 November 2026 | green |
| 11 | 31 | 24 November 2026 | green |
| 12 | 32 | 24 November 2026 | green |
| 13 | 33 | 24 November 2026 | green |
| 14 | 34 | 24 November 2026 | green |
| 15 | 35 | 24 November 2026 | green; the push to main that followed ran everything (the skip rule's workflow name was wrong, fixed in 16) |
| 16 | 36 | this pull request, merged on green CI | |

### Round 3: test counts

Last full local runs on this machine, on the phase 14 tree (24 November 2026); CI on each
merged pull request ran the same suites with network and passed every job.

| Suite | Result |
| --- | --- |
| Backend against PostgreSQL 16 (`supracloud-jarvis/ira/tests`, database harness on) | 7424 passed, 42 skipped, 2 failed |
| Backend without a database (same tree, harness off) | 4882 passed, 543 skipped, 2 failed |
| Carer dashboard (Vitest) | 35 passed |
| Phone app (Jest) | 43 passed |
| Watch app (Gradle unit tests, Wear OS and AOSP flavours) | all passed, 0 failures in each flavour |
| Browser journey against clean-clone staging (Playwright, `e2e/`) | 7 passed (5 journeys, 2 outage drills) |
| Documentation truth check (`scripts/check_docs_truth.py`) | every readiness row in one of three states, every proof present |

The two backend failures are the same two tests on every run on this machine. The egress
guard test resolves a public host name, which this machine cannot; CI can, and passes it. The
history-scan test (`test_the_table_matches_a_fresh_scan_of_history`) calls a rebuild script the
source project never handed over and looks for values in a history this repository does not
carry; it skips on CI's shallow clone and fails on a full clone. It is left as it is (rule:
never weaken a test) and is the owner's decision, recorded as D-004 in `docs/DECISIONS.md`.

### Round 3: needs hardware

From `docs/READINESS.md` (every row marked "needs hardware"):

- The watch app on a real Wear OS watch and on an AOSP board that meets `docs/hardware/SENSOR_HAL.md`:
  the screen, speech, vibration, sensors, the outbox across a real restart, the fall model on
  real falls.
- The phone app installed on a phone: the photo picker, push notifications, a screen-reader pass.
- The watch hardware itself: a prototype built from `docs/hardware/`, checked by a hardware
  engineer, then the certification roadmap.

### Round 3: needs the owner

- Delete the merged branches (this machine's network refuses branch deletes).
- Decide the fate of `test_the_table_matches_a_fresh_scan_of_history` (D-004): bring the
  history-scanning script and the source project's history over, or retire the test.
- Build the two application images and start the stack with Caddy on a machine with a domain
  name (`docs/DEPLOY.md`).
- Real text messages and calls (Twilio keys), real push (`IRA_CARE_PUSH_PROVIDER=expo`), sign-up
  mail, a payment provider: each is off until the owner provides the account.
- Reviews only a qualified person can give: the privacy notice and terms, the compliance drafts
  (Clinical Safety Officer, DPO, regulatory adviser), the hardware component list.
- Allow `api.figshare.com`, `ieee-dataport.org` and `www.kaggleusercontent.com` if the original
  dataset terms or the notebook output files are wanted.

---


Round 1 was written at the end of its phase 9 and round 2 at the end of its phase 10, both on
21 November 2026. Round 1's counts are from its last full local runs, matched by CI on the
merged pull requests. Round 2's counts are from the clean-clone run described below; CI
matched them for pull requests 10 to 13 and could not start for 14 to 19 (see "What needs
the owner").

## What was built, per phase

| Phase | Pull request | What it delivers |
| --- | --- | --- |
| 1 ci-and-build-rules | 1 | CI on Linux runners (backend unit, backend database with PostgreSQL 16 and pgvector, lint, watch, mobile, dashboard); product specification, build rules, decisions record, progress files, Apple Watch plan; the missing scheduler worker script |
| 2 care-data-model | 2 | Fifteen care tables with forced row-level security (migration 099); the `care` package with authorisation by workspace role, consent rules, scoped device credentials, an access audit, and 40 routes under `/api/v1/care` |
| 3 checkin-engine | 3 | The 15 to 20 minute cycle, quiet hours in the person's timezone, template questions chosen by rule, answers read by rule, two kinds of miss, the watch-side routes, the `care_checkins` job |
| 4 monitoring-and-escalation | 4 | The safety rules, the tiered SOS with acknowledgements and retries, the provider interface (log-only default, Twilio adapter off without keys, emergency numbers refused), the `care_monitoring` job, `scripts/simulate_patient.py` |
| 5 watch-app | 5 | The Wear OS app: foreground service, Health Services sensors behind an interface, speech in and out, offline queue, pairing, "I'm OK" and "Help" |
| 6 family-app | 6 | The Expo app: sign-in and care circle, alerts with one-tap acknowledge, "call 999 now" when the backend flags tier 3, person status, history under consent, contacts, consent recording, pairing codes, the weekly report |
| 7 carer-dashboard | 7 | The Next.js dashboard: overview, alerts, person timeline, readings, routines, medication, circle, settings, consents, conversation under consent, reports, search, access log |
| 8 reports-and-history | 8 | `care_reports` (migration 100), the daily carer report and weekly family report generated once per period in the person's timezone, audited history search; summaries never quote the person's words |
| 9 hardening | 9 | Retention enforcement (`care_retention` job), the static audit-coverage check, the 1,000-person load test, the simulation extended to the next morning's report, final documents |

## Round 2: what was built, per phase

| Phase | Pull request | What it delivers |
| --- | --- | --- |
| R2-1 continuous-monitoring | 10 (merged) | Readings checked on arrival, two in a row over two minutes; ask first with a server-kept deadline; night rules; the morning check; job heartbeats, `/care/ops/status`, the "monitoring paused" banner; migration 101; three simulator scenarios |
| R2-2 home-and-location | 11 (merged) | Home address, coordinates, emergency summary, sealed access notes for tier 1 and 2 while an alert is open; location once at an SOS; whereabouts, address, map and alert links in every SOS message; both apps' alert screens; migration 102 |
| R2-3 fall-detection | 12 (merged) | The watch's fall detector and countdown; the server's `suspected_fall` alert with its own deadline; per-person settings and off switch; synthetic-signal counts; `docs/DEVICE_SETUP.md`; migration 103 |
| R2-4 ira-conversation | 13 (merged) | The conversation service: composed check-ins with templates as fallback, structured answers with the stricter reading winning, red-flag phrases that ask first, Talk to IRA on the watch and the phone, rolling memory; model configurable, fake in CI; migration 104 |
| R2-5 demo-mode | 14 (merged 23 Nov) | The seeded demo organisation, the demo API, the watch simulator page, scripted scenarios, the Playwright recording in `docs/demo/`, `DEMO_SCRIPT.md`; the cloud sign-in and the browser `fetch` fixes |
| R2-6 onboarding-and-billing | 15 (merged 23 Nov) | Sign-up through the identity register, the organisation with a trial, invitations by link with consent captured, acceptance, plan limits; the dashboard's sign-up, join and plan pages; the family app's pairing and join screens; migration 105 |
| R2-7 deployment-ready | 16 (merged 23 Nov) | The deployable care server and worker, `deploy/` (compose, images, Caddy, backups, restore, roles), the CI restore round trip, `docs/DEPLOY.md`, the 200-person soak |
| R2-8 store-readiness | 17 (merged 23 Nov) | Release builds, icons, listings, permission and Data safety answers, the accessibility pass with tests, the draft privacy notice and terms |
| R2-9 compliance-drafts | 18 (merged 23 Nov) | Six documents in `docs/compliance/`, all DRAFT FOR REVIEW BY A QUALIFIED PERSON |
| R2-10 final-readiness | 19 (merged 23 Nov, as a merge commit) | Integrates 14 to 18, proves everything from a clean clone, `docs/READINESS.md`, these pages; updated from `main` after 14 to 18 merged |

## Round 2: test counts (clean-clone run of the integrated branch)

From a fresh `git clone` of `feat/final-readiness` (commit d7d268b) on the build machine, with
the migration chain applied and verified ("104 files, all hashes match"):

| Suite | Result |
| --- | --- |
| Backend lint (`ruff check . scripts`) | All checks passed |
| Backend unit (`pytest tests`, without the database directories) | 3,768 passed, 27 skipped, 1 deselected (D-004) |
| Backend database (`tests/tenancy`, `readiness`, `architecture`, `callers`, `idempotency`, `runtime`) | 3,491 passed, 1 failed: `test_the_watch_side_over_the_real_routes`, which ran on the real clock after 22:00 UTC and met the person's quiet hours. The test now switches quiet hours off for that person and passes at any hour; the fix is in this branch, after the clean-clone commit |
| Watch (`./gradlew testDebugUnitTest assembleRelease`) | 35 passed; the unsigned release package built |
| Family app (`npm ci`, `tsc`, `jest`, `expo export --platform web`) | 20 passed; type check and web export pass |
| Dashboard (`npm ci`, `tsc`, `vitest`, `next build`) | 21 passed; type check and production build pass |
| Demo (`seed_demo.py`, `scenarios.py all`, `simulate_patient.py`) | 2 people seeded with device tokens; 4 scenarios ran without error; 5 simulator scenarios "ran as described" |
| Soak (`soak_24h.py`, 200 people, 5-minute steps) | 20 of 20 silent people escalated, 0 missing, 0 late; check-in pass mean 0.098 s, monitoring pass mean 0.202 s |
| Recording (`record_demo.mjs` against the clean clone's demo API, dashboard and family web build) | 12 screenshots and the video produced |

Round 2 care-specific tests within those suites: 43 unit (continuous rules, location and
access notes, falls, conversation, the care server) and 33 database-backed (continuous,
location, falls, conversation, onboarding), plus the adversarial, manifest and erasure
ratchets extended to the new table and columns.

## Round 2: the merge day (23 November 2026)

- GitHub refused to start any workflow from 20:43 UTC on 21 November. The run page's own
  annotation named the cause: "recent account payments have failed or your spending limit
  needs to be increased"; the payment history showed three declined card payments. The guess
  recorded on the day (the Actions allowance) was wrong and is corrected in STATUS and
  D-049. The payment cleared on 23 November and the owner reopened pull requests 14 to 19,
  which started their runs.
- 14 ran green on its own commit and merged. 15, 16, 17 and 18 were each brought up to date
  with `main` (the decisions log and the status page, and for 17 the family app's manifest
  and package file, resolved by keeping both sides in order), ran green on the merged commit
  and merged in order.
- One real failure: the backup round trip on 16, because the runner image had moved to the
  PostgreSQL 17 client (D-050). Fixed in 16; the database suites had passed on every run.
- 19 was then updated from `main` (its remaining conflicts were the progress pages and the
  three files 16's fix touched, taken from `main`) and awaits its run.
- Nothing was pushed to `main` directly, no history was rewritten, no test was weakened.

## Round 2: pull request descriptions to clean, and branches to delete

- **Descriptions to clean:** the hosting tooling appended a footer to every pull request
  description on every write, so pull requests 10, 11, 12, 13, 14, 15, 16, 17, 18 and 19
  carry it (the text itself is not repeated here). Each description was rewritten once and
  read back; the footer returned each time.
- **Branches to delete** (the session's access cannot delete branches):
  `feat/continuous-monitoring`, `feat/home-and-location`, `feat/fall-detection`,
  `feat/ira-conversation`, `feat/demo-mode`, `feat/onboarding-and-billing`,
  `feat/deployment-ready`, `feat/store-readiness`, `docs/compliance-drafts` (all merged), and
  after merging: `feat/final-readiness`. Round 1's `feat/*` branches too.

## Round 1: test counts (last full local runs)

| Suite | Result |
| --- | --- |
| Backend unit (`pytest tests`) | 4,783 passed, 531 skipped, 1 inherited failure that skips in CI (D-004) |
| Backend database (`tests/tenancy`, `readiness`, `architecture`, `callers`, `idempotency`, `runtime`) | 3,448 passed |
| Care-specific tests within those | 34 unit (rules, credentials, consent, wording, providers, reports), 70 adversarial RLS, 33 database-backed API and scenario tests, 2 static audit checks, 3 retention and load tests |
| Watch (`./gradlew testDebugUnitTest`) | 22 passed |
| Family app (`npm test`) | 14 passed |
| Dashboard (`npm test`, `npm run build`) | 12 passed; build succeeds |
| Negative controls | Every phase broke its own code and saw its tests fail before restoring them (recorded per phase in STATUS.md) |

Load: with 1,000 people in one workspace, a scheduling pass took 1.1 s, a pass with every
check-in due 1.9 s, and a monitoring pass 0.65 s, locally.

## What is deferred, and why

- **The language model does not phrase questions by default.** Templates are what is
  tested and what runs. `IRA_CARE_MODEL_PHRASING=true` lets a model rephrase a question; on
  any failure the template is used. Deferred because no model runs in CI and the cycle must
  not depend on one.
- **Push notifications** go through the existing notifier, whose mobile push stays off until
  its key is configured. Attempts are recorded either way.
- **Branch deletion on GitHub** was not possible from the session's access; the merged
  `feat/*` branches can be deleted from the GitHub branches page.
- **Eight inherited tests of removed parts were retired on the owner's decision** (D-007);
  one inherited test fails only on a full local clone (D-004).

## Round 2: what needs a real device or the owner

The full table is `docs/READINESS.md`. In short: a Galaxy Watch and a phone for everything
on the wrist and in the pocket (the fall detector's real-world accuracy is unmeasured); a
rented UK-region machine for the first `docker compose up`; an SMS and mail provider and a
payment provider with keys; a signing key, store accounts and submissions; legal review of
the privacy notice and terms; a Clinical Safety Officer, a data protection officer and an
MHRA adviser for the compliance drafts; and a security test. The CI block that held pull requests 14 to 19 over the weekend is
gone: it was GitHub refusing to start jobs after three declined card payments, not the
allowance; the payment cleared on 23 November, and 14 to 18 ran green and merged in order
(see "Round 2: the merge day" below).

## Round 1: what needs a real device or the owner

| Need | Why | Blocked on |
| --- | --- | --- |
| Run the watch app on a Galaxy Watch (with LTE for standalone use) | The sensor source, text-to-speech and speech recogniser are written against the Android APIs but never executed on a device; the background body-sensor permission must be granted in system settings | A Galaxy Watch 4 or later |
| Run the family app and the dashboard against a live backend by a person | Only automated tests have exercised them | A phone with Expo Go, a browser, a running backend |
| Vendor sensor SDK | Richer sensors need partner approval and a real watch; the `SensorSource` interface is ready for it | The vendor partner request |
| Text messages and phone calls | The Twilio adapter is off until `IRA_CARE_TWILIO_ACCOUNT_SID`, `IRA_CARE_TWILIO_AUTH_TOKEN` and `IRA_CARE_TWILIO_FROM` are set; no paid service was signed up for | Provider keys, and a decision to pay |
| Tier 3 monitoring centre | No adapter exists because no contract exists; tier 3 shows the family "call 999 now" and repeats tiers 1 and 2 | A monitoring-centre contract |
| Regulatory advice | Remote monitoring software may be a medical device; the product makes safety observations against carer-set ranges and uses no diagnosis words, but that is a design choice, not a determination | A regulatory adviser, before sale |
| Apple Watch | Out of scope; `docs/APPLE_WATCH_PLAN.md` | A Mac, an Apple developer account, a test watch |

## Where to look

- `docs/PRODUCT_SPEC.md`: what the product is.
- `docs/BUILD_RULES.md`: how it is built.
- `docs/DECISIONS.md`: every choice made where the specification left room (D-001 to D-050).
- `docs/READINESS.md`: every promised feature as works, needs hardware, or needs the owner.
- `docs/progress/STATUS.md`: per-phase measurements and deferrals.
- `STATUS.md`: what is proved and what is not, in one page.
