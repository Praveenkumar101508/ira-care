# IRA CARE: clinical safety case report and hazard log (DCB0129 structure)

**DRAFT FOR REVIEW BY A QUALIFIED PERSON.** Written by the build to the structure of DCB0129
(Clinical Risk Management: its Application in the Manufacture of Health IT Systems). No
Clinical Safety Officer has reviewed it; no clinical risk management system is in place; it
claims no compliance. Its value is that every hazard is tied to the code that mitigates it
and the test that proves the mitigation, so a reviewer can check rather than trust.

## 1. System definition

See `INTENDED_PURPOSE.md`. Components: the watch app (Kotlin, Wear OS), the server and
worker (Python), the family app (React Native) and the dashboard (Next.js); the data model
in `supracloud-jarvis/postgres/099` to `105`.

## 2. Clinical risk management system

Not established. This document proposes the hazard log below as the starting point; the
operator appoints a Clinical Safety Officer who owns it from then on.

## 3. Risk acceptability

Proposed scale: likelihood (rare, unlikely, possible, likely) and severity (minor, significant,
considerable, major, catastrophic) as DCB0129 defines; the reviewer sets the acceptance
line. Initial ratings below are the build's estimate and are marked as such.

## 4. Hazard log

Each row: the hazard, its cause, the harm, the controls in the product, where the control
lives, the test that proves it, and the residual risk as estimated by the build.

| # | Hazard | Cause | Harm | Controls in the product | Code | Tests proving the control | Residual (estimate) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| H1 | A person in trouble is not noticed | No question asked; the watch off, flat or out of signal; quiet hours | Delayed help | Check-ins every 15 to 20 minutes outside quiet hours; a notice when the watch has not synced, is off the wrist, or is low; "no movement by wake time" morning check; off-wrist at night is a notice; the watch keeps an offline queue and retries | `care/checkin_cycle.py`, `care/monitoring.py`, `watch/.../OfflineQueue.kt` | `tests/tenancy/test_care_checkins_db.py`, `test_care_continuous_db.py::test_the_morning_check_runs_when_nothing_has_moved_by_wake_time`, `test_care_escalation_db.py`, `watch/.../OfflineQueueTest.kt` | Possible/considerable: a watch that is off cannot notice; the product says so (DEVICE_SETUP, store text) |
| H2 | A person does not answer and is not moving, and nobody is told | The sensor check or the escalation fails | Delayed help | After a miss the watch syncs a snapshot; the pass opens an SOS when movement is at or below the floor; tiers 1, 2, 3 with retries and acknowledgement; the last tier says "call 999 now" | `care/monitoring.py::evaluate_missed_checkin`, `care/escalation.py` | `test_care_escalation_db.py`, `scripts/simulate_patient.py` (stops-answering), `scripts/soak_24h.py` (20 of 20 silent people escalated) | Unlikely/major |
| H3 | A heart rate outside the carer's range is missed or acted on wrongly | One noisy reading; a range nobody set | Delayed help, or alarm fatigue | Readings checked on arrival; two in a row over two minutes; no built-in range; "ask first" before an SOS; the server keeps the deadline | `care/continuous.py`, `care/askfirst.py`, `care/repository.py::evaluate_arrivals` | `tests/care/test_care_continuous_rules.py`, `test_care_continuous_db.py` (asks first, silence becomes an SOS, I'm OK resolves) | Possible/significant: the range is the carer's judgement |
| H4 | A fall is not detected, or detected when there was none | The heuristic's limits; the watch off the wrist | Delayed help; alarm fatigue | Impact then stillness; off-wrist suppresses; a 30 s countdown that asks first; the server's own deadline; a per-person off switch; the maker's own detection recommended as a second layer; accuracy stated as unmeasured | `watch/.../FallDetector.kt`, `FallCountdown.kt`, `care/falls.py` | `FallDetectorTest.kt` (synthetic set, counts in STATUS.md), `FallCountdownTest.kt`, `test_care_fall_db.py` | Possible/considerable: real-world accuracy unmeasured; the product says so |
| H5 | The system stops silently | The worker dies; the database or Redis is down | Nobody is told that nobody is being told | Heartbeats per pass; `/health/jobs`; the dashboard's "monitoring paused" banner; an operations message; container health checks and restarts | `care/ops.py`, `care/server.py`, `deploy/docker-compose.yml` | `test_care_continuous_db.py::test_the_operations_check_notices_when_the_jobs_stop`, `dashboard topbar.test.tsx` | Unlikely/significant |
| H6 | Responders go to the wrong place | No location; a stale fix; a guess | Delayed help | Location sent once at the SOS; "unavailable" said plainly; at-home decided from the carer's coordinates with the fix's accuracy; the address in every message | `care/location.py` | `tests/care/test_care_location_rules.py`, `test_care_location_db.py` | Unlikely/significant |
| H7 | The companion gives medical advice or a diagnosis | A language model reply | Wrong action by the person | Every reply through a guard (no diagnosis words, no advice, bounded); advice requests answered with a refusal that offers the carer; templates as fallback; the keyword rules' safety signals can never be downgraded by the model | `care/conversation.py::guard`, `merge` | `tests/care/test_care_conversation_rules.py`, `test_care_conversation_db.py` | Unlikely/significant |
| H8 | A call for help in words is missed | Keyword rules miss a phrasing | Delayed help | "help", "can't get up", "I fell", "chest pain", "can't breathe" and others go straight to ask-first; the model reading adds to, never removes from, the rules | `care/checkins.py::_HELP`, `care/conversation.py::RED_FLAGS` | `test_care_checkin_rules.py`, `test_care_conversation_rules.py::test_red_flag_phrases_go_straight_to_help` | Possible/significant: phrasing is open-ended |
| H9 | Emergency services are dialled wrongly, or nobody calls them | A misconfigured number; reliance on the system | Wasted response; delayed help | The providers refuse emergency numbers outright; the last tier shows "call 999 now" to people and the family app offers the dialler as the person's own action | `care/providers.py` | `test_care_monitoring_rules.py::test_the_system_never_dials_the_emergency_services`, `mobile model.test.ts` | Rare/major |
| H10 | The wrong people see the person's words or health data | A bug in consent or isolation | Loss of privacy, distress | Consent per role; forced row-level security; workspace roles; access notes sealed and shown only to tier 1 and 2 while an alert is open; every read logged | `care/consent.py`, `care/authz.py`, `care/accessnotes.py`, `care/audit.py` | `test_care_isolation_rls.py`, `test_3c06_seventeen_tables.py`, `test_care_api_db.py`, `test_care_location_db.py`, `test_care_audit_coverage.py` | Unlikely/significant |
| H11 | An alert is acknowledged by nobody and forgotten | Tier 3 reached; no monitoring centre | Delayed help | Tier 3 repeats tiers 1 and 2 and shows "call 999 now"; the alert stays open on every screen until resolved | `care/escalation.py` | `test_care_escalation_db.py` | Possible/considerable: depends on the circle |
| H12 | Summaries or reports contain the person's words or a judgement | A model summary that quotes or diagnoses | Distress; wrong action | Summaries never quote (a quote check drops the model's summary); no diagnosis words anywhere; reports describe against the carer's range | `care/conversation.py::_quotes_the_person`, `care/reports.py` | `test_care_conversation_rules.py`, `test_care_reports_rules.py` | Unlikely/minor |
| H13 | A restored backup is wrong or empty | A bad passphrase; a damaged file | Loss of history | The restore refuses before touching the database; CI restores every pull request and compares counts | `deploy/restore.sh` | CI step "Backup and restore round trip" | Rare/significant |

## 5. Test evidence

The suites named above ran green locally at the end of each phase and in CI for every
merged phase (`docs/progress/STATUS.md` has the counts). What the tests cannot show: real
devices, real people, real networks. Those are listed as needing the owner.

## 6. Residual hazards summary

The hazards that depend on the physical world (H1, H4, H8, H11) remain possible and are
disclosed in the product's own words. The reviewer decides whether the disclosure is
sufficient and what evidence a pilot must gather before anyone relies on the service.

## 7. Recommendation

Not for clinical reliance until a Clinical Safety Officer has reviewed this log, a pilot
with real devices has measured H1 and H4, and the MHRA questions have been answered.
