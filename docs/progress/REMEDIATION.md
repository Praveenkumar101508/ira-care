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
| A3d | High | schema (migration 112) | There was no correction history, no `self` provenance and no "observed" state. | `care_medication_events.source` (self_report, family_report, carer_report, carer_observed, device, system; `legacy` for older rows, which are never guessed) and `.recorded_by`. New append-only table `care_medication_event_corrections` (RLS by tenant and workspace; the application role may SELECT and INSERT only). A dose settled as taken or skipped changes only with a `correction_reason`, and the old values are kept. Filling in an unknown or missed dose is also recorded. `GET /people/{id}/medication-corrections` reads the history, and every read is logged. "Observed" counts only for a carer in the person's circle. | DB: source and recorded_by from the caller; a daughter cannot claim "observed"; 409 without a reason; the correction row keeps the old values; UPDATE and DELETE are refused to the app role; another tenant gets 404. 3c06 isolation matrix and erasure-coverage registry include the new table. | fixed |
| A3e | High | `care_checkins` (migration 112), `care/checkin_cycle.py` | The check-in did not record which medicine and which dose it asked about. | `care_checkins.medication_id` and `.dose_due_at` are set when the question is chosen (prepared ahead or asked now), and only when the dose is unambiguous. A constraint keeps the two columns both set or both empty. The answer is bound to exactly that medicine and dose; similar names and close doses no longer matter. If the medicine was stopped, or the answer comes more than 2 hours late, nothing is written. Rows from before 112 use the conservative re-derivation from A3a. | DB: binding beats ambiguous wording; the scheduler binds when it asks; stopped medicine or late answer writes nothing | fixed |
| A3f | — | migration 112 evidence | — | Ran on a scratch database: forward migration, rollback (`rollback/112_care_medication_provenance_down.sql` removes the 4 columns and the table, and leaves the dose records), then re-apply. All three steps succeeded. The integrity record (`MIGRATION_FILE_HASHES.json`) is updated. The migration only adds things: nullable columns, a column with a default, and a new table. | — | fixed |
| A4 | Critical | `care/checkin_cycle.py` line 453 (`ok=not reading.asked_for_help`), `care/askfirst.py` | Any answer that was not a recognised call for help resolved the safety alert as "answered OK": "I'm not OK", "maybe", unrelated talk, and an empty voice transcription (treated as the OK button). | Three outcomes: **ok** only for the paired watch's OK button (`via=button`, no words) or a short unambiguous spoken OK with no sign of trouble from the rules, the model or the classifier; **help** for any request, red flag, pain, distress or negative; **unclear** for everything else, which leaves the alert `asking` with its ORIGINAL deadline. | unit: 21 phrases; DB: each phrase against a real asking alert, deadline unchanged, empty voice ≠ button | fixed |
| A4b | Critical | `watch/.../FallCountdown.kt` lines 77–101 | The watch's spoken-OK expression matched "OK" inside "I'm not OK" and "not fine", ending a fall countdown as if the person had tapped I'm OK. | Same rule as the server: help words ask for help; any negative, doubt or distress word means the countdown runs on; only a short unambiguous positive ends it. "I didn't fall" no longer ends it by voice (it has a negative); the I'm OK button still does. | JVM: `FallCountdownVoiceTest` (compiled with kotlinc 2.0.21 and JUnit 4.13.2 outside Gradle, 21 check-in tests pass); CI's Wear OS and AOSP jobs are the build evidence | fixed |
| A5 | High | `care/providers.py`, care exception logs | The log-only provider logged the first 80 characters of every SMS, call script and monitoring-centre summary (person's name, what happened). Sixteen care log lines printed raw exception messages, which can carry model output or field values. | Provider logs: channel, masked number, length, reference only. Exception logs: the exception type only. | unit: a message with a name, address and heart rate leaves none of it in the log | fixed |
| A6 | High | `care/routes.py` `POST /people/{id}/talk` | Any member with the act permission (a daughter, a carer, a workspace admin, another wearer) could talk as the person: transcripts, memory, reminders and alerts in the person's name. | Only an active member of THIS person's circle with relationship `self`; checked before the stream starts, so a refusal writes nothing. The watch's device route is unchanged. The phone app offers Talk only to that account. | DB: wearer 200; daughter, carer, admin outside the circle, another wearer 403; other tenant 404; revoked membership 403; nothing written on refusal. Jest: `isWearerOf`. | fixed |

## Group A2: emergency message delivery

| ID | Severity | Where | Finding | Fix | Tests | Status |
|----|----------|-------|---------|-----|-------|--------|
| A2-1 | Critical | `care/escalation.py` `run_due_escalations` | Due alerts were selected with no lock. Two monitoring workers at once told the same tier twice. Measured: three workers sent 8 texts and calls to 4 recipients. | Alerts are claimed with `FOR UPDATE SKIP LOCKED` inside the transaction that moves them on. Messages are queued in that transaction, and outbox rows are claimed with a lease. | DB: three workers at once send exactly 4 for 4 | fixed |
| A2-2 | High | `care/escalation.py` `run_workspace` | The whole workspace pass was one transaction, with the SMS, calls and push sent inside it. A failure later in the pass rolled back the record of messages already sent, and a slow provider (15 s timeout) held up every other person's alert. | Messages are queued, the transaction commits, then `care/delivery.py` sends them. Each send has a 20 s limit, at most 8 run at once, and failures are retried 3 times with a growing wait. A queued message survives a crash and is sent on the next pass. | DB: survives a crash between deciding and sending; a slow send does not hold up the others; failure is retried then recorded as failed; a provider that recovers sends on the retry | fixed |
| A2-3 | High | `care/providers.py` | A Twilio 2xx was recorded as `sent`, and nothing ever confirmed delivery. | A 2xx is now `accepted`. Twilio's status callback (`/care/providers/twilio/status/…`) moves the message to delivered, undelivered, failed, no answer or busy. It is verified twice: our HMAC token in the URL, and Twilio's signature over the URL and form (checked against Twilio's own validator). It is replay-safe, and order does not matter. | Unit: Twilio reference signature; anything altered or missing is refused. DB: a signed callback moves the state forward only; an unsigned, forged-token or other-message callback changes nothing; a callback that arrives before the send was recorded is kept | fixed |
| A2-4 | High | push receipts | Push receipts updated the push tickets but never the alert message's state. | `settle_push_attempts`: a delivered ticket makes the push message delivered; all tickets failing makes it failed. | (covered with A2-3) | fixed |
| A2-5 | Medium | `care/escalation.py` | A tier with nobody in it, or with nobody reachable, was passed over without a record. | An explicit `skipped` attempt says so ("nobody is set up in tier N…"). | — | fixed |
| A2-6 | Medium | outbox | A message queued for an alert that was resolved before it was sent would still go out. | It is recorded as skipped ("resolved before this was sent") and not sent. | DB | fixed |
| A2-7 | — | migration 113 | — | Additive. `care_alert_attempts` stays insert-only (an existing test pins this), and states are new rows in the insert-only `care_alert_attempt_events`. The outbox `care_alert_dispatch` can update only tries, lease and finished time. Forward, rollback and re-apply were run on a scratch database. Classified in the tenancy manifest, the isolation matrix and the erasure registry. | registry suites | fixed |
| A2-8 | — | responder follow-up | The brief asked that an acknowledgement start the follow-up deadline, that non-arrival restart the escalation, and that only a recorded outcome resolve the alert. | Already in place before this work: `care/followup.py`, round 3. Not changed here. | existing `test_care_follow_up_db` | already in place |

## Group C: phone app session and privacy

| ID | Severity | Where | Finding | Fix | Tests | Status |
|----|----------|-------|---------|-----|-------|--------|
| C3 | Critical | `mobile/src/App.tsx` sign-out | Sign-out cleared only the stored session. Patients, alerts, the selected person, circle and consents stayed in memory; the phone stayed registered for the account's alert pushes; appearance (photo, wallpaper) stayed on the phone; and a reply still on its way for account A was shown to account B after B signed in on the same phone. | Sign-out now does all of this: unregisters the push token (bounded to 3 s, so offline sign-out still completes); takes the account's notifications off the screen; cancels pending preference writes; clears all account state and the stored appearance. A session generation counter drops any reply that belongs to an earlier sign-in. | `app_signout.test.tsx`: clean-up on sign-out, and A's late reply never reaches B; `push_signout.test.ts`: unregister, and offline does not block | fixed |
| C4 | High | `mobile/src/api/client.ts` | The refresh token was stored and never used: after the 30-minute access token expired, the next refresh signed the person out silently, even in the middle of an alert. | One shared renewal (`POST /auth/refresh`) for every call that hit the expired token, then each call is retried once; there are no loops. A refused renewal is final and the sign-in screen says why ("Your sign-in has expired. Please sign in again to see alerts."). No network while renewing is "no connection", never a sign-out. The renewed token is kept for next time. A request refused with 401 was refused before anything happened, so retrying it is safe. | `session.test.ts`: 5 tests | fixed |
| C1, C2 | High | Talk to IRA | The reply is not streamed, and there is no voice in or out on the phone. | Not done here: it needs native speech modules (`expo-speech` and a speech recogniser), and device builds to verify. React Native's `fetch` cannot stream either, so it needs `expo/fetch` or a native module. | — | open |
| C5 | Medium | colours | Hard-coded colours that break dark mode. | Not reviewed in this branch. | — | open |

## Group B: dashboard live patient record

| ID | Severity | Where | Finding | Fix | Tests | Status |
|----|----------|-------|---------|-----|-------|--------|
| B1 | High | `dashboard/src/app/people/[id]/page.tsx` | The record loaded once and never refreshed. A carer looking at it saw a frozen picture with nothing saying so. | The record refreshes every 30 s, and at once when the page comes back into view. A banner says "Live: updated N s ago", or "Connection lost: reconnecting … may be out of date" after a failure or after 90 s with no update. Loads never overlap. | `person_live.test.tsx`: refreshes after 30 s and says when it last updated; `live.test.ts`: the wording never claims "live" when the record is not | fixed |
| B1b | High | same | A late answer for the previous person could land on the next person's record, and nothing was cleared when switching. | `LiveGate`: every person switch starts a new generation; an answer from an older one is dropped; all state is cleared on switch. | `live.test.ts`: overlap refused; out-of-order answers show only the newest person | fixed |
| B1c | Medium | same | All 11 requests ran as one `Promise.all`, so one failing optional section blanked the whole record. | Status and alerts load first, on their own. The other sections load side by side with `allSettled`; a failed section is named on the banner and the rest still shows. | `person_live.test.tsx`: a failed consents section is named and the record still shows | fixed |
| B1d | — | same | The brief said editing a form must not stop the alert updates, and live data must stay separate from form drafts. | Each form keeps its own draft state, set once, so a refresh cannot overwrite what is being typed, and refreshing never pauses. | (by construction; noted) | fixed |
| B2 | High | dashboard header (line 95), `headline()`; phone `deviceSummary` | An unknown wrist state (`null`) showed as "on the wrist". The headline said "All well" with the wrist unknown, and also while IRA was still asking whether the person was OK. | `wristState()` has four states: on, off, unknown, and stale ("last known …"). The headline says "All well" only when the watch is known to be worn and synced recently and nothing is open or being asked. Otherwise it says "Needs a look: …" or "IRA is asking whether they are OK". The phone app says "wrist unknown" and "on the wrist" explicitly. | `live.test.ts`, `person_live.test.tsx`, mobile `model.test.ts` | fixed |
| B3 | — | dashboard | Not done in this branch: progressive loading beyond essential-first, the review of empty, error, offline and permission states on every tab, mobile-width layout, and real-time push for urgent changes (polling only). | — | — | open |

## Group D: watch speech, storage and the Help path

| ID | Severity | Where | Finding | Fix | Tests | Status |
|----|----------|-------|---------|-----|-------|--------|
| D0 | Critical | `service/CareForegroundService.kt`, Help | The watch spoke "Sending your request for help" and waited up to 15 s for a location fix before writing the Help request to the outbox, even though the comment said the outbox came first. Speech had no time limit, so a stuck speech engine could hold the request back indefinitely. | Help is now: location (at most 2 s; no fix means "location unknown", never a guess), then outbox and send, then speech. | (Android service: built by CI; the speech time limit it relies on is tested below) | fixed |
| D1 | High | `voice/Listener.kt` | `SpeechRecognizer` was created from a background thread (Android requires the main thread), a new recogniser was made for every answer and never destroyed, and cancellation called `cancel()` from any thread. | `RecognizerListener` (pure Kotlin) handles every recogniser call on the main thread: one at a time, destroyed after each answer including on timeout and cancellation, late results ignored, and no recogniser on the watch means "nothing heard". `AndroidListener` only adapts the platform recogniser. | `SpeechCoreTest`: 6 listening tests | fixed |
| D2 | High | `voice/Speaker.kt` | The first sentence could be lost because the engine was not ready yet; there was no time limit, so a stuck engine hung the caller; each sentence replaced the progress listener, so an earlier waiter never resumed; "louder" set the volume to maximum and never put it back. | `EngineSpeaker` (pure Kotlin): waits for the engine to be ready (at most 4 s), one sentence at a time, each with its own id and a time limit based on its length (then stopped), and the volume restored after "louder", including on timeout. | `SpeechCoreTest`: 5 speaking tests | fixed |
| D3 | — | fall countdown | — | Done in group A (A4b). | — | fixed |
| D5 | High | `outbox/Outbox.kt`, `queue/OfflineQueue.kt`, `pairing/PairingStore.kt`, manifest | (a) A rewrite truncated the file before writing it, so a crash mid-rewrite lost every queued urgent event, including an unsent Help. (b) Unreadable lines were dropped silently. (c) Everything was stored in plain text, including the device token and location. (d) Queued events and readings did not record whose they were, so after re-pairing to another person they would be sent in the new person's name. (e) Backup was not turned off (Android's default is on). | (a) `AtomicFiles`: a rewrite goes to a temporary file, is synced and renamed; appends are synced; a leftover temporary file is cleaned up. (b) Unreadable lines go to `<file>.unreadable` and are counted. (c) AES-256-GCM per line with a key in the Android Keystore; older plain text is still read and sealed on the next write; if the Keystore is broken, an urgent event is still written in plain text rather than lost. The pairing values are sealed, and saved with `commit()`. (d) `WearerFiles`: one file per wearer; the old single file goes to the wearer paired when the update first runs. (e) `allowBackup=false` plus data-extraction rules that exclude everything. | `SecureStorageTest` 11; `OfflineQueueTest` +2 | fixed |
| D4, D6 | Medium | foreground service, permissions, platform behaviour | Not reviewed here: reboot and Doze behaviour, permission revocation, Bluetooth and LTE changes, emulator and physical-device tests. | — | — | open (needs emulator or device) |

## Negative control

Migration 112: of the 5 new database tests, run against the code from before 112 on the migrated
database, 4 fail (binding, scheduler binding, provenance, correction). The fifth passes because
the old code also wrote nothing for a late answer or a stopped medicine.

Group A2: the 21 new tests were run against the code from before A2 (the old `escalation.py`
and `providers.py`): 12 fail and 9 pass. The 9 that pass are the pure signature, token and
rank rules, which do not depend on the old sending code. The two-worker test failed on real
duplicates: 8 messages for 4 recipients.

Group C: the 9 new tests were run against the original `App.tsx`, `push.ts`, `storage.ts`,
`client.ts` and `LoginScreen.tsx`: 8 fail and 1 passes. The one that passes is "a 401 with no
refresh token is final", which the old code also met. In the late-reply test the old app showed
account A's person ("Ada-only-for-A") on account B's home screen.

Group B: the three page tests in `person_live.test.tsx` all fail against the original page and
`view.ts`: unknown wrist shown as worn, no refresh, and a failed section blanking the record.

Group D: the old `Listener`, `Speaker` and storage were Android-only, with no place for a test
to hook in, so the new tests cannot run against them unchanged. Instead each original defect
was planted back into the new code and the tests were run:
- no destroy: 3 failures;
- no volume restore: 2;
- no speech time limit: 1;
- no one-at-a-time: 1;
- a rewrite with no crash recovery: 1;
- unreadable lines dropped silently: 1;
- one file for every wearer: 2.

The tests were compiled outside Gradle with kotlinc 2.0.21 (coroutines 1.9.0, serialization 1.7.3,
OkHttp 4.12.0). 53 JVM tests pass. The Android adapters (`AndroidListener`, `AndroidSpeaker`,
`KeystoreKey`, `PairingStore`, the manifest) are compiled only by CI's Wear OS and AOSP builds.

Skipped tests: `main` gives 193 skipped. This branch gives 194 without `psycopg2` installed
(one new database module) and 544 with it, because four database modules that
`importorskip("psycopg2")` then skip test by test instead of as one module. Same two failures
in every run.


The 30 database tests in `tests/tenancy/test_care_safety_answers_db.py` were run against the
original `care/` package (fixes removed, tests kept): **25 failed, 5 passed**. The five that
pass are positive controls that held before too (the OK button resolves; a clear "yes" writes
the right dose; "I forgot" and an empty answer never wrote a taken dose). With the fixes, all
30 pass.

The watch: the new `FallCountdownVoiceTest` case "a negative, a doubt or trouble never ends the
countdown as OK" fails against the original `FallCountdown.kt` and passes with the fix.

## Still open

Rows marked `open` above (C1, C2, C5, B3, D4, D6), and the groups not started yet: E (system
security and integration review), F (full care-journey and accessibility testing) and G (final
documents and release evidence). The whole programme's state is in `COMPLETION_REGISTER.md`.
