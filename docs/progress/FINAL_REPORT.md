# IRA CARE: final report

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
| R2-10 final-readiness | 19 (open) | Integrates 14 to 18, proves everything from a clean clone, `docs/READINESS.md`, these pages; updated from `main` after 14 to 18 merged |

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
