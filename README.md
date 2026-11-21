# IRA CARE

IRA CARE is a proactive companion for older people who live independently. A watch worn
all day runs IRA, which starts short, warm conversations itself (meals, medication, mood,
pain, plans), notices when the person does not answer, reads the watch's own sensors, and
raises an alarm that reaches a human who goes to check. Family use a phone app; carers and
care organisations use a web dashboard. Conversations stay private unless the person
agrees to share them.

**Stage: built and tested in software; not yet run on a real watch or phone.** Every part
below has automated tests that run in CI. Nothing has been tried on a Galaxy Watch, in an
emulator, or with a real text-message or call provider. [STATUS.md](STATUS.md) says exactly
what is proved and what is not; [docs/progress/FINAL_REPORT.md](docs/progress/FINAL_REPORT.md)
is the hand-over.

## What is here

| Path | What it holds | Checked in CI by |
| --- | --- | --- |
| `supracloud-jarvis/ira/care/` | The care backend: data model API, check-in engine, monitoring rules, escalation chain, providers, reports, history, retention | Backend unit and database jobs |
| `supracloud-jarvis/postgres/099_*.sql`, `100_*.sql` | Sixteen care tables, all tenant and workspace scoped with forced row-level security | Database job (adversarial RLS suites) |
| `watch/` | The Wear OS app (Kotlin, Compose for Wear OS, Health Services) | Gradle build and unit tests |
| `mobile/` | The family and patient phone app (Expo, TypeScript) | Type check and tests |
| `dashboard/` | The carer dashboard (Next.js, TypeScript) | Type check, tests and build |
| `scripts/simulate_patient.py` | Five end-to-end scenarios on a test database: the person stops answering and moving; abnormal heart rate at 02:00 with no answer; abnormal heart rate answered "I'm OK"; no movement by wake time; the Help button pressed away from home | Database job |
| `docs/` | Product specification, build rules, every decision, progress and the final report | |

The foundation under `supracloud-jarvis/` (conversation, memory, scheduler, notifier,
logins and roles, approval and privacy, multi-tenant isolation) is taken from **IRA by
SupraCloud**; see [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md) for how the care features
sit on it.

## How it works, in one paragraph

Every 15 to 20 minutes while the person is awake, the backend schedules a check-in; the
watch collects it, speaks it, listens, repeats louder with vibration after a minute, and
posts the answer or that nobody answered. A missed check-in triggers a sensor check: no
movement, a heart rate outside the range the carer set, or the watch off the wrist opens an
SOS; the escalation chain tells family and carer (push, text, call), then the care
organisation, then shows the family "call 999 now" (there is no monitoring-centre contract
yet, and the system never dials 999 itself) until somebody acknowledges with "I'm on my
way". Readings, check-ins, alerts and medication confirmations feed a daily carer report and
a weekly family report, in summaries only.

## Running the backend tests

```bash
cd supracloud-jarvis/ira
python3.12 -m venv ../.venv
../.venv/bin/pip install -r requirements-test.txt
../.venv/bin/python -m pytest -q
```

The database suites need PostgreSQL 16 with pgvector, the roles from
`supracloud-jarvis/postgres/provisioning/001_database_roles.sql`, the chain applied with
`python -m scripts.migrate apply` as `ira_migrator`, and `IRA_TEST_DATABASE_DSN` (as
`ira_app`) and `IRA_TEST_ADMIN_DSN` (as `postgres`) set. `.github/workflows/test.yml` shows
the exact steps. The watch, phone and dashboard each have their own README.

## Things only the owner can do

A Galaxy Watch with LTE for device testing; the vendor sensor-SDK partner request before
release; text and call provider keys (the adapter stays off without them); a
monitoring-centre contract for tier 3; regulatory advice before sale. See
[docs/progress/FINAL_REPORT.md](docs/progress/FINAL_REPORT.md).

## Licence

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
