# Safety remediation register

Started 25 November 2026 from `main` at `1f41c5d` (the audited revision). One row per
finding. Status values: **fixed** (code and tests merged on this branch), **partial**
(the safe part is done; the rest needs a decision or a migration), **open** (not started).

The rule behind every row: IRA never records something as true when it does not know it is
true.

## Group A: backend care safety

| ID | Severity | Where | Finding | Fix | Tests | Status |
|----|----------|-------|---------|-----|-------|--------|
| A1 | Critical | `care/checkins.py`, `care/personal.py` | Two different answer readers; the reminder reader read "okay", "right" and "I will" as a taken dose, and the check-in reader read "I took it yesterday" or "I took a different tablet" as taken. | One conservative reader, `read_dose_answer` → yes / later / skip / unclear, used by both paths. Doubt, another time, another medicine, a dose count, a bare "okay" and any unplaced negative are never "yes". Meals and non-medication reminders keep their plainer reading. | `tests/care/test_care_safety_answers.py` (the 18 mandatory cases plus 50 more) | fixed |
| A2 | Critical | `care/conversation.py` `merge()` | The model or the classifier could upgrade an unclear dose answer to "yes"; the structured `medication_taken` kept the model's "yes" even when the merged answer was unclear; the summary could disagree with both. | For a dose the rules decide; the model and classifier can only make the answer more cautious. `said`, `dose`, `medication_taken` and the summary always agree. An existing test that pinned the unsafe upgrade (`test_care_classifier.py`) now pins the safe outcome. | unit: lying model and classifier; DB: `test_care_safety_answers_db.py` with a model that says "taken" to everything | fixed |
| A3a | High | `care/checkin_cycle.py` | The medicine was guessed from the question text (falling back to "the only one"), and the dose time was the scheduled time nearest the ANSWER, up to six hours away. "Not yet" wrote a `missed` event. | The medicine must be named by the question unambiguously (one active medicine, or exactly one name match; overlapping names give none). The dose is the one scheduled time within the hour of when the question was ASKED; zero or two candidates, or an answer more than two hours late, write nothing. Only "yes" (taken) and "not today" (skipped) are written; "later" leaves the dose open to be asked again. | DB: ambiguous names, two close doses, unnamed medicine, late answer | fixed |
| A3b | High | `care/repository.py`, `care/personal.py`, `care/checkin_cycle.py` | Every write path used `ON CONFLICT DO UPDATE`: a later answer silently overwrote a dose already settled. | One writer, `write_medication_event`: inserts or fills an open dose (due / unknown / missed); a dose settled as taken or skipped is never overwritten. The same record again is a no-op; a different one is refused (HTTP 409 on the route, a warning with ids only on the automated paths). | DB: settled dose survives a later "not today" and a carer's different record | fixed |
| A3c | High | `care/routes.py` medication-event route, `personal.act_on_reminder` | `confirmed_via` came from the request (a family account could claim `device`); a reminder confirmed from the app was always recorded as `family`. | Provenance is derived from the verified caller's circle membership: the person themself → `checkin` (self-report), family → `family`, anyone else → `carer`. The request's `confirmed_via` is accepted for compatibility and ignored. `device` is unreachable from an account. | DB: daughter claiming `device` is recorded as `family` | fixed |
| A3d | High | schema | A correction history for doses, a distinct `self` provenance and an explicit `observed` state need new columns. | **Needs a migration and owner approval.** Until then the code fails safe: a conflicting record is refused rather than written over. | — | partial (migration not prepared) |
| A3e | Medium | `care/checkin_cycle.py` | The check-in does not store which medicine and dose time it was about (no column), so binding is re-derived from the question text and the asked time. | Safe re-derivation above; a `medication_id` + `due_at` on `care_checkins` would make the binding exact. | — | partial (migration not prepared) |
| A4 | Critical | `care/checkin_cycle.py` line 453 (`ok=not reading.asked_for_help`), `care/askfirst.py` | Any answer that was not a recognised call for help resolved the safety alert as "answered OK": "I'm not OK", "maybe", unrelated talk, and an empty voice transcription (treated as the OK button). | Three outcomes: **ok** only for the paired watch's OK button (`via=button`, no words) or a short unambiguous spoken OK with no sign of trouble from the rules, the model or the classifier; **help** for any request, red flag, pain, distress or negative; **unclear** for everything else, which leaves the alert `asking` with its ORIGINAL deadline. | unit: 21 phrases; DB: each phrase against a real asking alert, deadline unchanged, empty voice ≠ button | fixed |
| A4b | Critical | `watch/.../FallCountdown.kt` lines 77–101 | The watch's spoken-OK expression matched "OK" inside "I'm not OK" and "not fine", ending a fall countdown as if the person had tapped I'm OK. | Same rule as the server: help words ask for help; any negative, doubt or distress word means the countdown runs on; only a short unambiguous positive ends it. "I didn't fall" no longer ends it by voice (it has a negative); the I'm OK button still does. | JVM: `FallCountdownVoiceTest` (compiled with kotlinc 2.0.21 and JUnit 4.13.2 outside Gradle, 21 check-in tests pass); CI's Wear OS and AOSP jobs are the build evidence | fixed |
| A5 | High | `care/providers.py`, care exception logs | The log-only provider logged the first 80 characters of every SMS, call script and monitoring-centre summary (person's name, what happened). Sixteen care log lines printed raw exception messages, which can carry model output or field values. | Provider logs: channel, masked number, length, reference only. Exception logs: the exception type only. | unit: a message with a name, address and heart rate leaves none of it in the log | fixed |
| A6 | High | `care/routes.py` `POST /people/{id}/talk` | Any member with the act permission (a daughter, a carer, a workspace admin, another wearer) could talk as the person: transcripts, memory, reminders and alerts in the person's name. | Only an active member of THIS person's circle with relationship `self`; checked before the stream starts, so a refusal writes nothing. The watch's device route is unchanged. The phone app offers Talk only to that account. | DB: wearer 200; daughter, carer, admin outside the circle, another wearer 403; other tenant 404; revoked membership 403; nothing written on refusal. Jest: `isWearerOf`. | fixed |

## Negative control

The 30 database tests in `tests/tenancy/test_care_safety_answers_db.py` were run against the
original `care/` package (fixes removed, tests kept): **25 failed, 5 passed**. The five that
pass are positive controls that held before too (the OK button resolves; a clear "yes" writes
the right dose; "I forgot" and an empty answer never wrote a taken dose). With the fixes, all
30 pass.

The watch: the new `FallCountdownVoiceTest` case "a negative, a doubt or trouble never ends the
countdown as OK" fails against the original `FallCountdown.kt` and passes with the fix.

## Not in this branch

Groups A2 (delivery reliability, Twilio callbacks, which needs a migration of
`care_alert_attempts.outcome`), B (dashboard), C (phone), D (watch), E (system review),
F (journey testing) and G (documents) are separate branches, not started here.
