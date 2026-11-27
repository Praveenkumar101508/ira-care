# IRA CARE

IRA CARE is a proactive companion for older people who live independently. A watch worn
all day runs IRA, which starts short, warm conversations itself (meals, medication, mood,
pain, plans), notices when the person does not answer, reads the watch's own sensors, and
raises an alarm that reaches a human who goes to check. Family use a phone app; carers and
care organisations use a web dashboard. Conversations stay private unless the person
agrees to share them.

**Stage: built, tested and demonstrated in software; not yet run on a real watch or phone,
and not yet hosted.** Round 2 added real 24/7 safety (readings checked as they arrive, ask
first then escalate, night rules), home and location for responders, fall detection on the
watch, a real conversation with IRA, a hardware-free demo, sign-up and billing, deployment
files with proved backups, store readiness and the compliance drafts. Every part has
automated tests; the demo was recorded in a real browser; nothing has been tried on a
Galaxy Watch or with a real text-message, call, mail or payment provider.
[docs/READINESS.md](docs/READINESS.md) lists every feature as works, needs hardware, or needs
the owner; [STATUS.md](STATUS.md) says what is proved; [docs/progress/FINAL_REPORT.md](docs/progress/FINAL_REPORT.md)
is the hand-over.

## What is here

| Path | What it holds | Checked in CI by |
| --- | --- | --- |
| `supracloud-jarvis/ira/care/` | The care backend: data model API, check-in engine, continuous monitoring and ask-first, monitoring rules, escalation chain, location and access notes, falls, the conversation service, onboarding, providers, reports, history, retention, the deployable server and worker | Backend unit and database jobs |
| `supracloud-jarvis/postgres/099_*.sql` to `105_*.sql` | Seventeen care tables, all tenant and workspace scoped with forced row-level security, and the round-2 columns | Database job (adversarial RLS suites) |
| `watch/` | The Wear OS app (Kotlin, Compose for Wear OS, Health Services) | Gradle build and unit tests |
| `mobile/` | The family and patient phone app (Expo, TypeScript) | Type check and tests |
| `dashboard/` | The carer dashboard (Next.js, TypeScript) | Type check, tests and build |
| `IRA_CLOUD_REGISTRATION_ENABLED` (setting) | Opens `/care/signup` (off by default: no sign-ups). `IRA_PAYMENT_PROVIDER` stays unset unless billing is configured; care plans carry no price | Setting |
| `IRA_CARE_MODEL` (setting) | `ollama` (default: two free open-weight models served on the same machine, `IRA_CARE_MODEL_PRIMARY` then `IRA_CARE_MODEL_SECONDARY`, then the set questions; `docs/MODEL_CHOICE.md`), `fake` (tests and CI) or `off` (templates only); the first model is `gemma4:e4b`, the second `gemma4:e2b`; a turn asks only a model already loaded (`IRA_CARE_MODEL_KEEP_ALIVE`, 30 min), speaks an instant acknowledgement (`IRA_CARE_INSTANT_ACK`), gives the model 8 s for its first token (`IRA_CARE_MODEL_FIRST_TOKEN_S`) and 12 s for its first sentence (`IRA_CARE_MODEL_FIRST_SENTENCE_S`) before the set line takes over. With a GPU, `deploy/compose.gpu.yml` makes the chain `gemma4:12b` then `gemma4:e4b`, both loaded (D-074; `docs/DEPLOY.md`, "With a GPU"). No hosted model API exists. | Setting |
| `deploy/` and `docs/DEPLOY.md` | Containers for the server, worker and dashboard, HTTPS through Caddy, nightly encrypted backups with a restore proved in CI, and the step-by-step guide for one UK-region machine; `scripts/soak_24h.py` is the accelerated day for 200 people | Deployment |
| `docs/DEVICE_SETUP.md` | Pairing a watch, permissions, fall detection in IRA CARE and the watch maker's own as a second layer | Document |
| `scripts/demo/` and `docs/demo/` | The hardware-free demo: seed, demo API, scripted scenarios, the recording, and `DEMO_SCRIPT.md` for a presenter; the watch simulator is `/simulator` on the dashboard | Demo |
| `scripts/simulate_patient.py` | Five end-to-end scenarios on a test database: the person stops answering and moving; abnormal heart rate at 02:00 with no answer; abnormal heart rate answered "I'm OK"; no movement by wake time; the Help button pressed away from home | Database job |
| `docs/hardware/` | Our own watch, designed in Blender: eight renders (`renders/`, the exploded view in stack order), the 20-second `turntable.mp4`, `viewer.html` (turn it, explode it, tap a part; open it in any browser), the glTF and `.blend`, `BLUEPRINT.md` and `COMPONENTS.md`; one look everywhere, a graphite case and a coral Help button (D-072) | Document |
| `docs/film/ira_care.mp4` | The five-minute film at 1080p: the turntable, then the journey on the real staging stack with IRA's own words, captioned; `NARRATION.md` says how it is remade | Document |
| `docs/READINESS.md` | Every promised feature as works (proved by), needs hardware, or needs the owner | Document |
| `docs/compliance/` | Intended purpose, clinical safety case and hazard log, DPIA, DTAC answers, MHRA adviser questions, incident process; all DRAFT FOR REVIEW BY A QUALIFIED PERSON | Documents |
| `docs/store/` and `docs/legal/` | Store listings, permission and Data safety answers, and the draft privacy notice and terms (DRAFT FOR LEGAL REVIEW) | Documents |
| `docs/` | Product specification, build rules, every decision (D-001 to D-073), progress and the final report | |

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

## Who builds it

- Praveen Kamineti ([Praveenkumar101508](https://github.com/Praveenkumar101508))
- Sravani Perni ([sravaniperni](https://github.com/sravaniperni))
- Uday Kumar Dandaveni ([UdayKumar08](https://github.com/UdayKumar08))

## Licence

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
