# IRA CARE: final report

Written at the end of phase 9 (21 November 2026). Test counts are from the last full local
runs; the CI runs of the merged pull requests show the same suites passing.

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

## Test counts (last full local runs)

| Suite | Result |
| --- | --- |
| Backend unit (`pytest tests`) | 4,780 passed, 530 skipped, 1 inherited failure that skips in CI (D-004) |
| Backend database (`tests/tenancy`, `readiness`, `architecture`, `callers`, `idempotency`, `runtime`) | 3,444 passed |
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

## What needs a real device or the owner

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
- `docs/DECISIONS.md`: every choice made where the specification left room (D-001 to D-023).
- `docs/progress/STATUS.md`: per-phase measurements and deferrals.
- `STATUS.md`: what is proved and what is not, in one page.
