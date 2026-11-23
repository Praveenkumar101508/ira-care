# IRA CARE: readiness

One table, every promised feature, one of three states:

- **works (proved by …)**: an automated test, a recorded run or a measured number in this
  repository shows it, from a clean clone;
- **needs hardware**: built and unit-tested, but never run on the real device it is for;
- **needs the owner**: built, but a payment, a signature, an account or a qualified review
  that only the owner can give stands between it and use.

The counts and the clean-clone run are in `docs/progress/FINAL_REPORT.md`. Nothing below is
rounded up.

## Round 1 (merged to `main`, pull requests 1 to 9)

| Feature | State | Proof or blocker |
| --- | --- | --- |
| Care data model: 16 tables, forced row-level security, access log | works | `tests/tenancy/test_care_isolation_rls.py`, `test_3c06_seventeen_tables.py`, the manifest ratchet |
| Check-in cycle: 15 to 20 minutes, quiet hours, warm wording, answers summarised | works | `tests/care/test_care_checkin_rules.py`, `tests/tenancy/test_care_checkins_db.py` |
| Safety rules and tiered SOS with acknowledgements and retries | works | `tests/care/test_care_monitoring_rules.py`, `tests/tenancy/test_care_escalation_db.py`, `scripts/simulate_patient.py` |
| Text and call providers, log-only by default; emergency numbers refused | works (log-only) / needs the owner for real sending | `test_the_system_never_dials_the_emergency_services`; Twilio keys and a decision to pay |
| Wear OS watch app: foreground service, sensors behind an interface, speech, offline queue, pairing | needs hardware | 35 JVM tests and a release build; never run on a watch |
| Family app: sign-in, alerts, acknowledge, "call 999 now", status, history under consent, consents, pairing | works on the web build (recorded) / needs hardware on a phone | `docs/demo/10-family-home.png` to `12-family-on-my-way.png`; 19 tests |
| Carer dashboard: overview, alerts, timeline, readings, routines, medication, circle, settings, consents, conversation, reports, search, access log | works | 18 tests, the production build, the recording in `docs/demo/` |
| Daily carer report and weekly family report | works | `tests/care/test_care_reports_rules.py`, `tests/tenancy/test_care_reports_db.py` |
| Searchable history, audited | works | `tests/tenancy/test_care_reports_db.py` (search), `test_care_audit_coverage.py` |
| Consent: summaries for all, words only with recorded consent | works | `tests/tenancy/test_care_api_db.py`, `test_care_conversation_db.py` |
| Retention per person; the access log never purged | works | `tests/tenancy/test_care_retention_db.py` |
| Load: 1,000 people in one pass | works (measured) | `test_a_thousand_people_fit_in_one_cycle_pass` |

## Round 2 (pull requests 10 to 13 merged; 14 to 18 open; 19 integrates them)

| Feature | State | Proof or blocker |
| --- | --- | --- |
| Readings checked as they arrive; two in a row over two minutes | works | `tests/care/test_care_continuous_rules.py`, `tests/tenancy/test_care_continuous_db.py` |
| Ask first, then escalate; the server keeps the deadline | works | `test_a_confirmed_breach_at_night_asks_first_and_silence_becomes_an_sos`, `test_im_ok_resolves_the_question_and_help_opens_it` |
| Night rules: heart rate asks first, off wrist a notice, no movement nothing, the morning check | works | `test_night_rules_off_wrist_is_a_notice_and_no_movement_is_nothing`, `test_the_morning_check_runs_when_nothing_has_moved_by_wake_time` |
| The system watches itself: heartbeats, `/health/jobs`, the "monitoring paused" banner, the operations message | works | `test_the_operations_check_notices_when_the_jobs_stop`, `dashboard topbar.test.tsx` |
| Home record: address, coordinates, emergency summary; access notes sealed, tier 1 and 2 only, every view logged | works | `tests/tenancy/test_care_location_db.py`, `tests/care/test_care_location_rules.py` |
| Location once at an SOS; at home / away / unknown; map link; in every SOS message | works | the same suites; `docs/demo/09-dashboard-alerts.png` |
| Fall detection on the watch: impact then stillness; countdown; SOS with location; per-person off switch | needs hardware | `FallDetectorTest.kt` (synthetic: 0 of 20 falls missed, 0 false positives on 50 non-falls with the wrist known, 10 of 10 unknown-wrist table drops read as falls), `FallCountdownTest.kt`, `tests/tenancy/test_care_fall_db.py`; real-world accuracy unmeasured |
| The watch maker's own fall detection as a second layer | needs the owner | `docs/DEVICE_SETUP.md`; a real watch to turn it on |
| IRA talks: composed check-ins, structured answers with the stricter reading winning, red-flag phrases ask first; a chain of two free open-weight models on the same machine (`gemma4:12b`, then `gemma4:e4b`, then the set questions); no hosted model API | works (measured) | `docs/MODEL_CHOICE.md`: 150-turn eval set over five candidates on 4 CPU cores (`docs/evidence/eval/*.json`); red-flag recall 1.00 for every model because the rules decide; first sentence p50 10.0 s (12B) / 4.6 s (E4B); `tests/care/test_care_eval_runner.py`, `tests/reasoning/*` prove the external path is closed |
| Our own models: a trained fall detector in the watch (beats the rules on held-out subjects), an answer classifier behind a default-off switch (equals the rules), a conversation fine-tune measured and not adopted (worse than the base); every dataset's licence recorded | works (fall model: 5 watch tests with the real TensorFlow Lite file; classifier: 5 tests; evidence files with the Kaggle numbers) | `watch/app/src/test/java/care/ira/watch/sensors/ModelFallDetectorTest.kt`, `supracloud-jarvis/ira/tests/care/test_care_classifier.py`, `docs/evidence/training/*.json`, `docs/DATASETS.md`, `docs/MODEL_CHOICE.md` |
| Talk to IRA on the watch and the phone; calls dial as the person's own action; reminders; advice refused | works (server and apps) / needs hardware (watch speech) | the same suites; `docs/demo/06-simulator-talk.png`; `CareApiTest.kt` talk test |
| Rolling memory; transcripts under consent | works | `test_talk_streams_sentences_keeps_transcripts_under_consent_and_rolls_the_memory` |
| Demo: seeded organisation, watch simulator, scripted scenarios, recording, presenter script | works | `docs/demo/` (12 screenshots, `demo.mp4`), `scripts/demo/`, the clean-clone run |
| Sign-up, the organisation, invitations by link with consent captured, acceptance, plan limits | works (routes and apps) / needs the owner to enable sign-up and mail | `tests/tenancy/test_care_onboarding_db.py`; `IRA_CLOUD_REGISTRATION_ENABLED` and an SMTP account |
| Plans and billing | works (catalogue, limits, status) / needs the owner to price and configure a provider | the billing routes; no price rows; `IRA_PAYMENT_PROVIDER` unset; no live keys |
| Containers, HTTPS proxy, health checks, restart policies | needs the owner | `deploy/`; no container runtime here, so the first `docker compose up` on a rented machine is the proof; `docs/DEPLOY.md` |
| Nightly encrypted backups with a proved restore | works | `deploy/backup.sh`, `restore.sh`, the CI round trip, the local run (151 tables, 104 migrations before and after) |
| Soak: 200 people, a simulated day, no missed escalations, job timings | works (measured locally) | `scripts/soak_24h.py`: 20 of 20 escalated, 0 missing, 0 late; pass timings in STATUS.md |
| Release builds: watch (minified, unsigned here), family app (profiles, no submission), icons, listings, Data safety answers | needs the owner | a signing key, store accounts, a build-service project, submissions |
| Accessibility: labels, roles, contrast, sizes, live regions | works (automated) / needs hardware for a screen-reader pass | `jest-axe` and the contrast test; the family app's accessibility test; the watch's source test |
| Privacy notice and terms | needs the owner | marked DRAFT FOR LEGAL REVIEW |
| Compliance drafts: intended purpose, safety case and hazard log, DPIA, DTAC, MHRA questions, incident process | needs the owner | marked DRAFT FOR REVIEW BY A QUALIFIED PERSON; a Clinical Safety Officer, a DPO, an adviser |
| CI green on every pull request | works (proved by the merged runs of pull requests 10 to 18 and the run on 19) | the two-day `startup_failure` was GitHub refusing to start jobs after declined card payments; it cleared when the payment went through on 23 November. Keeping the card as the default payment method is the owner's |

## Round 3 (the completion run)

| Feature | State | Proof or blocker |
| --- | --- | --- |
| Demo state never committed; per-run demo passwords and keys; gitleaks over the whole history in CI | works | `tests/readiness/test_demo_state_not_tracked.py`, the `secrets` CI job, `.gitleaksignore` |
| The wrist shows the true SOS state: sending, waiting for connection, received, "[Name] is on the way", "[Name] has arrived"; the server pushes changes | works (server, simulator) / needs hardware (the watch screen) | `tests/care/test_care_sos_rules.py`, `tests/tenancy/test_care_sos_db.py` (state and stream), `SosStateTest.kt`, `CareApiSosTest.kt`, the dashboard simulator test |
| Durable outbox on the watch with idempotency keys; the server ignores duplicates and remembers refusals | works (server and JVM tests) / needs hardware (a real restart and reboot) | `OutboxTest.kt` (restart, backoff, keys), `test_care_sos_db.py` (help, fall, answer sent twice; refusal remembered; keys per device) |
| Every heart-rate sample kept at its own time; out-of-range samples uploaded at once; readings every 60 s (a setting) | works (server, simulator) / needs hardware (sensor timing) | `ReadingBatcherTest.kt`, `test_every_sample_keeps_its_own_time_and_arrival_is_stamped`, `test_the_watch_is_told_the_carers_range_and_the_upload_interval` |
| Sample-to-alert delay measured through the simulator path | works (measured) | `scripts/measure_sample_to_alert.py`, `docs/evidence/sample_to_alert.json`, the numbers in STATUS.md |
| "I'm on my way" starts a 30 minute deadline; the responder is asked, then the chain restarts; an alert closes only with an outcome; two responders at once are safe | works | `tests/tenancy/test_care_follow_up_db.py` (5 tests: deadline, re-escalation, arrival, required outcome, concurrency) |
| Scheduled visits: who comes when, with tasks; the watch says "[Name] is coming at 10:00" and "[Name] is here"; check-ins pause while a carer is in the room; no arrival is late (a notice and an extra check-in) then missed (an alert that escalates); departure records outcome, tasks done and a handover; the timeline names the source of every line | works (server, dashboard, phone model) / needs hardware (the watch screen) | `tests/care/test_care_visit_rules.py` (4), `tests/tenancy/test_care_visits_db.py` (4: planning and the watch, late then missed, arrival and departure, tenancy and rollback refusal), `VisitStateTest.kt` (3; the whole watch set: 48), `mobile/src/__tests__/visits.test.ts` (3), the dashboard timeline tests |
| IRA personal: a communication profile (pace, prompting, channels) edited by the person or representative and applied on the watch; with consent, familiar topics, topics to avoid and a named person for repeated worries; real timed reminders from conversation, confirm/snooze/skip, two-step medication confirmation, hydration prompts; what IRA remembers editable by the person; concern summaries labelled "summary by IRA" and linked to their source; the fall countdown hears "I'm OK" and "help" | works (server, dashboard, phone model) / needs hardware (speech on a real watch) | `tests/care/test_care_personal_rules.py` (8), `tests/tenancy/test_care_personal_db.py` (4), the 162-turn eval set (`docs/evidence/eval/phase7_rules.json`), `FallCountdownVoiceTest`, `CheckinControllerProfileTest`, `mobile/src/__tests__/reminders.test.ts` |
| Care push only to the circle's registered phones; tickets, receipts and delivery state on the alert; a second family never receives the first's alert; a removed member and a revoked token stop receiving | works (log-only provider by default) / needs the owner to set `IRA_CARE_PUSH_PROVIDER=expo` for real phones | `tests/care/test_care_push_rules.py` (guard), `tests/tenancy/test_care_push_db.py` (8 tests) |

## What "needs hardware" means here

A Galaxy Watch 4 or later with LTE for the watch app (sensors, speech, the fall detector,
location at an SOS), and an Android or iOS phone for the family app. Nothing in those paths
has been executed on a device; the unit tests and the browser simulator are what exist.

## What "needs the owner" means here

Pay: an SMS and call provider, a mail account, a machine in a UK region, store accounts, a
build service, a payment provider. Sign: the signing key, the privacy notice and terms after
legal review, contracts for tier 3. Review: a Clinical Safety Officer for the hazard log, a
data protection officer for the DPIA, an MHRA adviser for the questions, a security test.
And first: restore GitHub Actions so the open pull requests can be merged.
