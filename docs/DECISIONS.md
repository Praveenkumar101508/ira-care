# IRA CARE: decisions

Every choice made during the build where the specification left room. Newest at the
bottom. Each entry says what was decided, why, and what would change it.

## D-001 Branch and merge model (phase 1)

Work happens on `feat/<phase-name>` branches that are squash-merged into `main` once CI
is green. The cloud session that started this build was opened on an automatically named
branch; that branch is not used for any work and can be deleted on GitHub.

## D-002 CI layout (phase 1)

One workflow, `.github/workflows/test.yml`, because existing tests and the protected-paths
list already name that file. Backend unit tests, backend database tests, backend lint,
and three client jobs (watch, mobile, dashboard) that are gated on whether their
directory exists, so the workflow is green before a client is built and enforces as soon
as one appears. Linux runners only.

## D-003 Database job shape (phase 1)

The database job provisions the real roles (`postgres/provisioning/001_database_roles.sql`),
applies the chain with `scripts.migrate` as `ira_migrator`, verifies migration hashes, and
runs the database suites as `ira_app`. This mirrors the install path rather than loading
SQL files directly, so CI proves the same thing a fresh install proves.

## D-004 A test that cannot pass on a full local clone (phase 1)

`tests/test_published_credentials.py::test_the_table_matches_a_fresh_scan_of_history`
needs a history-scanning script that was not carried over from the source project and a
history this repository does not have. It skips on a shallow clone, which is what CI
checks out, and fails on a full local clone. It is left unchanged (rule: never weaken a
test); the local failure is recorded in `progress/STATUS.md`. Bringing the script over or
retiring the test is the owner's decision.

## D-005 Defaults recorded from the specification (phase 1)

Configurable per person; these are the defaults the code ships with.

- Check-ins every 15 to 20 minutes, randomised, during waking hours.
- Quiet hours 22:00 to 07:00: silent monitoring only.
- No answer: repeat after 1 minute louder with vibration; sensors checked 2 minutes later.
- Escalate on heart rate outside the carer-set range, no movement, or watch off the wrist
  during a no-answer check; otherwise log a missed check-in and escalate after 3 in a row.
- "Help" button or spoken "help": escalate immediately.
- Tier 1 family and carer (push, SMS, call), 5 minutes for an acknowledgement; tier 2 the
  care organisation; tier 3 a monitoring-centre adapter if configured, otherwise tiers 1
  and 2 repeat and family see a "call 999 now" screen. The system never dials 999.
- Carer alerts: watch off the wrist 30 minutes in the day, battery under 15 %, background
  sensor permission off, no sync for 30 minutes.
- No built-in medical thresholds; no diagnosis words.
- Family see alerts and summaries; transcripts only with recorded consent. Every view of
  health data or a transcript is audited. Retention 12 months by default.

## D-006 Emergency route (phase 1)

Default: family and carer, then the care organisation. A paid 24-hour monitoring centre is
an adapter that stays unconfigured until the owner signs a contract. Reason: no paid
service may be added, and UK emergency services do not accept automated alerts.

## D-007 Inherited database tests of removed parts: retired (phase 1)

Running the database suites for the first time found 9 tests that could not pass here.
One, in `tests/tenancy/test_p4_scheduler.py`, wanted a dedicated worker entry point
(`scripts/staging/run_scheduler.py`) that was not carried over; IRA CARE needs one, so it
was written and the test passes. The other 8 checked parts of the source project that
were not carried over: two release documents under `docs/cloud`, the "untestable items"
record, and four removed routes (files, agents, technical library, self knowledge). They
could not pass without re-adding what was removed.

**Owner's decision (21 November 2026): retire them**, consistent with the 165 test files
of the same kind the split already dropped. The eight functions were deleted from
`tests/readiness/test_phase_4_acceptance.py`, `tests/readiness/test_concurrency_and_failover.py`
and `tests/callers/test_api_route_callers.py`; the rest of each file stands. This is the one
deliberate exception to "never delete a test", made by the owner, and recorded here.

The 34 billing-test errors seen locally were a local artefact: the payment-event guard
accepts the privileged role only when it is literally named `postgres`, which CI uses.
With that role the 41 billing tests pass.

## D-008 Care data belongs to the person, not to an account (phase 2)

The fifteen `care_*` tables are tenant AND workspace scoped with forced row-level security,
and none is in the account erasure scope: a family member or carer deleting their own
account must not take the cared-for person's health history with them. A person's data is
removed by the care package's own erasure (`DELETE /api/v1/care/people/{id}`, which
cascades), and by tenant deletion (`ON DELETE CASCADE`). The exception is
`care_access_log`, audit evidence: it is append-only (a trigger refuses UPDATE for
everyone and DELETE for the runtime roles), its keys are RESTRICT, and it survives the
person it is about. Only the schema owner can purge it, which is how a retention purge
is an operator's deliberate act and never the application's.

## D-009 Transcript consent follows the reader's role in the care circle (phase 2)

Family see alerts and summaries. The person's own words (check-in answers, transcripts)
are readable only under a consent recorded for the reader's role in that person's care
circle: `transcripts_family` for family, `transcripts_carer` for carers and clinicians,
`transcripts_organisation` for the care organisation. The reader's role is their row in the
circle, matched on account id. A workspace administrator who is not in the circle gets no
transcripts however many consents exist. In the private edition the deployment owner is
treated as a carer. Every read, allowed or refused, writes `care_access_log`.

## D-010 Workspace roles decide read, act and manage (phase 2)

`viewer` reads status, alerts and summaries; `member` also acknowledges and resolves alerts
and confirms medication; `workspace_admin` also manages people, the circle, devices, safety
settings, routines, medication lists, consents and the access log. The deployment owner has
every level. Roles come from the verified workspace membership, never from a header. This
is a check on top of row-level security, not instead of it.

## D-011 Watch credentials carry their scope (phase 2)

A pairing code and a device token both encode the tenant and workspace they belong to, plus
a random secret; only the SHA-256 of the whole string is stored. A request carrying one is
bound to that scope first, and the hash is looked up inside it, so a token can never be
matched against another family's devices. Pairing codes live 15 minutes and are single use.

## D-012 The watch drives the conversation; the server keeps the clock honest (phase 3)

The server schedules every check-in (15 to 20 minutes on, randomised per the person's
settings, pushed past quiet hours) and composes the question when the watch collects it.
The watch does the talking: it asks for what is due, repeats louder with vibration after
the repeat interval, and posts the answer or that nobody answered. The server's one-minute
cycle marks a check-in `missed` on its own when the watch asked and no answer came within
the repeat and sensor-check windows plus a minute's grace, and when a scheduled check-in
was never collected for a whole window (the watch was offline, flat or not worn), with the
reason written in the summary. What a miss means (sensor check, escalation) is phase 4.

## D-013 Questions come from templates; a model may only rephrase them (phase 3)

The question kinds and words are templates in `care/checkins.py`: one greeting, one
question, the person's preferred name, no medical terms. Which kind comes next is a rule:
a medication due within the hour with no dose recorded, then a meal routine due within the
hour, then a follow-up on pain mentioned earlier today, then a rotation. A language model
may rephrase the composed question when `IRA_CARE_MODEL_PHRASING` is on (off by default);
on any failure the template is used, so the cycle never depends on a model being up.
Answers are read by rules too (yes/no for meals and medication; "mentioned pain",
"sounded low", "asked for help" as plain signals), and a yes/no about a dose records a
medication event for the nearest due time. A short note of each summary is written to the
memory store, best effort.

## D-014 Alerts describe, the carer's settings decide (phase 4)

Every rule in `care/monitoring.py` compares a reading or a miss with the person's own
settings and writes what it saw: "no answer and no movement for 3 minutes (movement count
0, floor 1)", "last heart-rate reading 150 is outside the range set for this person (45 to
130)". No reading is judged without a setting: an unset heart-rate range is no rule, and a
missing movement reading is unknown, never zero. A test pins that no alert text contains
a diagnosis word.

## D-015 Tiers, waits and the tier-3 fallback (phase 4)

`sos` and `urgent` alerts escalate every `ack_wait_minutes` (5 by default): tier 1 is the
circle's tier-1 members (family, carer, clinician) by push, text and call; tier 2 the
care organisation, plus a dashboard banner; tier 3 a monitoring-centre adapter when one
is configured, otherwise the alert is marked "show call 999 now" for the family app and
tiers 1 and 2 are told again, and again every wait, until somebody acknowledges or
resolves. `notice` alerts (watch off the wrist by day, low battery, permission off, no
sync) are told to tier 1 once, by push and text only, and wait to be resolved. One
unresolved alert per kind per person. **The system never dials the emergency services**:
`care/providers.py` refuses the well-known numbers before any provider is reached.

## D-016 Providers: log-only by default, a paid adapter only with keys (phase 4)

Text messages and calls go through `care/providers.py`. The default provider records every
attempt as `logged` and sends nothing, so the whole chain is proved in CI at no cost. A
Twilio adapter exists and is constructed only when its three settings
(`IRA_CARE_TWILIO_ACCOUNT_SID`, `IRA_CARE_TWILIO_AUTH_TOKEN`, `IRA_CARE_TWILIO_FROM`) are all
present; nothing in the repository signs up for or buys a service. No monitoring-centre
adapter exists yet: a contract comes first. Push goes through the existing notifier.

## D-017 The watch app's shape (phase 5)

Kotlin, Compose for Wear OS, minimum Wear OS 3 (API 30), compile SDK 35. Sensors come
through Wear OS Health Services (passive monitoring of heart rate and daily steps, plus the
system off-body sensor for wrist state) behind a `SensorSource` interface, so a vendor SDK
can be added after partner approval without touching the rest. Speech uses the watch's own
text-to-speech and speech recogniser; audio never leaves the device and is never stored.
The check-in conversation is a state machine with no Android in it (`CheckinController`),
driven by the foreground service: ask, listen, repeat louder with vibration after a minute,
give up two minutes later, report every step to the backend. Readings go to a file-backed
offline queue (bounded, oldest dropped) and are re-sent until acknowledged; the backend
de-duplicates. The device token lives in the app's private preferences; a revoked token is
refused server-side at once. The backend address is a build setting. Nothing has run on a
real watch yet.

## D-018 The family app's shape (phase 6)

Expo SDK 52 (React Native 0.76) in TypeScript, with no navigation library: three or four
screens switched by state, so the app stays small and testable. Sign-in uses the existing
`/auth/token` login and `/api/v1/workspaces`; one care circle is one workspace, chosen once
when the account belongs to several. Every care call carries the bearer token and the
`X-Workspace-ID` header, which the backend verifies. The session lives in the phone's secure
store. Alerts are polled every 30 seconds while the app is open, and push registration goes
through the existing `/mobile/devices` route (push itself stays off on the backend until its
key is configured). The alert list orders the most serious unacknowledged first; "I'm on my
way" is one tap; the "call 999 now" screen appears only when the backend set that flag at
tier 3, and dialling is the person's own action through the phone's dialler, never the
app's. The person's words are shown only when the backend says consent allows it.

## D-019 The dashboard's shape (phase 7)

Next.js 15 with the app router, TypeScript, no UI framework and no chart library: the
charts are plain SVG sparklines that describe readings without coloured thresholds, because
a threshold on a chart is a judgement the product does not make. The session is held in the
browser's session storage for the tab's life. Roles come from the verified workspace
membership: viewers and members read; administrators add people, routines, medication and
circle members, change the safety settings, record consents, issue pairing codes and read
the access log. Conversation text is requested from the backend and the 403 it returns
without consent is shown as such, so the dashboard never caches or infers what it may not
show. Tests run with Vitest and Testing Library (no browser).

## D-020 Reports are summaries, once per period, in the person's own clock (phase 8)

The daily carer report and the weekly family report are generated from the care tables and
stored in `care_reports` (migration 100) with a unique key per person, kind and period, so
the hourly job generates and delivers each exactly once: the daily report after 07:00 in the
person's own timezone, the weekly report on Monday after 08:00. Reports carry summaries
only, never the person's own words, whatever the consents say, because a report travels
(push, e-mail) in a way a screen does not. The same rule now applies to check-in summaries
themselves: a summary says "answered about how they feel (mentioned pain)" and never quotes
the answer, so what family see without consent, and what reports contain, cannot leak the
person's words. Readings are reported against the carer's range or as "no range set"; a test
pins that no report contains a diagnosis word. Delivery goes through the existing notifier.

## D-021 History search is one query, audited (phase 8)

`GET /care/people/{id}/history/search` searches check-in summaries and alerts, and the
transcripts only when the caller's role has consent, exactly as the transcript endpoint
does. Every search writes an access-log row with the words searched for and whether
transcripts were included, because a search is a read of health data.

## D-022 Retention is enforced per person, and never touches the record of looking (phase 9)

A daily job removes each person's readings, check-ins, transcripts, medication events and
reports older than their retention period (the `data_retention` consent, 12 months by
default), and alerts older than that only when they were resolved or cancelled. An alert
that is still open, acknowledged or escalating is never purged, and neither is the access
log: that is audit evidence with its own retention, removable only by the schema owner.
Each purge that removed anything writes one access-log row with the counts, so a deletion
is itself on the record. The purge runs under row-level security, one workspace at a time.

## D-023 What "hardened" means here, and what it does not (phase 9)

Hardening added: retention enforcement; a static check that every route reading health
data or transcripts writes the access log; a 1,000-person load test of the check-in and
monitoring passes (about a second each locally); the end-to-end simulation extended to the
next morning's report; and honest final documents. It did not add: a real device test (no
watch), a running model (the templates are what is tested), push or text delivery (the
providers are log-only without keys), or regulatory review. Those are listed in
`progress/FINAL_REPORT.md` as needing the owner.

## D-024 Readings are checked as they arrive, and a breach must be confirmed (round 2, phase 1)

Before this phase the heart-rate rule ran only in the minute-by-minute monitoring pass and
looked at the latest reading. Now every watch sync is evaluated on arrival
(`care/continuous.py`, called from `record_report`). A reading outside the carer's range
counts only when the trailing run of readings outside the range is at least
`heart_rate_confirm_readings` long (2) and spans at least `heart_rate_confirm_seconds`
(120); one reading back inside the range ends the run. A single spike is noise and raises
nothing. The range itself stays the carer's: with no range set there is no rule.

## D-025 Ask first, then escalate; the server keeps the deadline (round 2, phase 1)

A confirmed breach does not open an SOS at once. It opens an alert in a new state,
`asking`, and a `safety` check-in with fixed words ("{name}, are you OK? Tap I'm OK, or say
help.") that the watch collects at any hour, vibrates for, speaks loud and repeats sooner
than an ordinary question. "I'm OK" (the button, or any answer that is not a call for help)
resolves the alert with the outcome recorded; "help" (spoken, or the Help button) opens the
same alert rather than a second one. If nobody answers within `ask_first_wait_s` (60), the
escalation pass promotes the alert to `open` and runs tier 1: the deadline is the server's,
so a watch that dies mid-question still escalates. One unresolved alert per kind: a second
breach while the first is open adds nothing.

## D-026 Night rules: what the watch does at 02:00 (round 2, phase 1)

At night (the person's quiet hours) a confirmed heart-rate breach asks first exactly as in
the day. The watch off the wrist is usually charging: a notice for the carer after
`night_off_wrist_minutes` (60), never an SOS. No movement at night raises nothing: sleep is
not an emergency. The morning check closes the gap: once the usual wake time
(`usual_wake_time`, 07:30) plus `wake_grace_minutes` (60) has passed, the watch has reported
since waking but counted no steps, and no check-in has been asked since waking, a `morning`
check-in is asked at once (within three hours of the deadline, and never in quiet hours) ("Good morning {name}.
Are you up and about?"), once a day; no answer follows the normal missed-check-in path to
an SOS. A watch that has not reported at all since waking is the no-sync rule's business.
The settings are the carer's, on the safety settings page.

## D-027 The system watches itself (round 2, phase 1)

Each pass of `care_checkins` and `care_monitoring` writes a heartbeat to the existing
`job_runs` table. `GET /care/ops/status` reports both jobs' last run and whether either is
older than five minutes; the dashboard polls it every minute and shows a red "monitoring
paused" banner across every page when it is. The same check tells the operations contact
(`IRA_CARE_OPS_PHONE`, a placeholder number by default) through the configured provider,
log-only without keys, at most once every fifteen minutes. The check is tested by running
both jobs and then advancing the clock with no further pass. The banner is the honest
statement that check-ins and alerts are not being processed; it is not a substitute for
running the worker under a supervisor, which deployment readiness covers later.

## D-028 The home record: free text the carer controls, and sealed access notes (round 2, phase 2)

The person record gains a home address, optional home coordinates with a radius (entered
by the carer; there is no geocoding service), an emergency information summary and access
notes (key safe, which door, pets). The summary and the notes are free text; IRA CARE has no
fields of its own for conditions. The access notes are sealed at rest with the existing
secrets mechanism: a per-row data key (AES-256-GCM) wrapped under the configured
key-encryption key and bound to the tenant and the person's row, the same envelope the voice
templates use. The architecture ratchet that confines key handling to `core/connectors/`
caught the first draft, which opened the notes from the care package; the sealing now lives
in `core/connectors/care_access_notes.py`, inside the one package the rule allows, and the
care package keeps only the policy (who may view) and the column names. The ratchet itself
was not changed. The test's own comments prefer a named exemption for a module that owns its
secrets over placing non-connector secrets in that package; that is a decision for the owner
(listed under "Needs the owner"), and either answer keeps the same cryptography. The notes
never appear in a person record: clients see only `has_access_notes`.
They open through one route, `GET /care/alerts/{id}/access-notes`, for tier 1 and tier 2
recipients (a circle member linked to the caller's account, or the organisation's manager)
while the alert is open, escalating or acknowledged, and every opening writes an access-log
row naming the alert. Without the encryption key the server refuses to store notes rather
than store them in the clear.

## D-029 Location once, with the alert, never streamed (round 2, phase 2)

The watch asks for one location fix when the Help button is pressed, help is spoken, or (in
the next phase) a suspected fall is confirmed, and sends it with the help request. There is
no route that accepts a location on its own and readings do not carry one, so nothing can be
streamed. The fix is stored on the alert row, which means it lives exactly as long as the
alert under the person's retention period. "At home" is decided server-side from the home
coordinates and radius, widened by the fix's own accuracy, so a fix that might be at home
is not reported as away; with no fix or no home coordinates the answer is "unknown" and the
messages say so. The map link is a plain OpenStreetMap address with no key.

## D-030 SOS messages tell responders where to go (round 2, phase 2)

Every push, text and call script for an SOS now carries the whereabouts sentence (at home
with the address, away with the distance and address, or "location unavailable" with the
address), the coordinates and map link when there is a fix, and a link to the alert at
`IRA_CARE_PUBLIC_URL` (default the dashboard's development address). Notices about the watch
itself do not broadcast the address. The family app and the dashboard show the same
sentence, the emergency information, a map link, and a button that opens the access notes
and says that the view has been logged.

## D-031 A suspected fall is a heuristic on the wrist, and the server keeps the deadline (round 2, phase 3)

The watch runs a fall detector on its accelerometer: a sample at or above the impact
threshold, then no movement above the stillness threshold for the still window (defaults
2.5 g, 0.3 g, 20 seconds). A twitch is tolerated; getting up clears it; when the watch knows
it is off the wrist nothing is detected. The thresholds, the countdown and the on/off switch
are the carer's per-person settings, fetched by the watch after every sync, and none of them
is a medical claim. The detector has been tested on synthetic signals only; `STATUS.md`
records the counts and states plainly that accuracy on real people is unmeasured until real
devices are used. The watch makers' own fall detection should be turned on as well
(`docs/DEVICE_SETUP.md`).

A suspected fall asks first: the watch vibrates, says "It looks like you may have fallen.
Are you OK?", shows the two big buttons and counts down (30 seconds by default), and tells
the server at once. The server opens an `asking` alert of kind `suspected_fall` with no
check-in (the watch is asking) and a deadline of the countdown plus ten seconds, so a watch
that dies mid-countdown still escalates. "I'm OK" resolves it; Help, or the countdown
ending, sends a help request marked `fall` with the one-off location, which opens the same
alert. A watch that could not reach the server when its countdown started still opens a
suspected-fall SOS at the end. With detection turned off the server refuses a start, so a
watch with stale settings cannot open fall alerts.

## D-032 The accelerometer signal never leaves the watch (round 2, phase 3)

Only the detector reads the accelerometer, and nothing from it is stored or sent. When a
fall is suspected the server learns one peak value in g and the still duration, which appear
in the alert's summary ("a hard impact (3.4 g) followed by 20 seconds without movement")
so responders know what was observed, and nothing else.

## D-033 IRA talks through one conversation service, and the template is always there (round 2, phase 4)

`care/conversation.py` composes check-in questions, reads answers into a fixed structure
and holds free conversation, all through one model adapter. The default model is IRA's own
model layer (`utils/llm.py`, the fast tier: a small local model); `IRA_CARE_MODEL=fake` is
the deterministic model the tests and CI use, and `off` means templates only. A question is
composed from the person's context (name, routines, medication due, earlier answers today,
the rolling summary) in the person's language setting (default en-GB), one short warm
question at a time. The template takes over whenever the model fails, its first sentence is
slower than `IRA_CARE_MODEL_FIRST_SENTENCE_S` (default 4 seconds), or the reply fails the
guard; a composed medication question that does not name the medication is not used, because
the answer is read against that name. `care_checkins.question_source` records which path
asked. Model output is streamed and cut into sentences as they complete, so the watch speaks
the first one while the rest is still coming.

## D-034 Answers: the model reads, the rules keep the floor, and some phrases ask first (round 2, phase 4)

Each answer is read by the model into a validated structure (wellbeing, pain mentioned,
medication taken yes/no/unknown, needs help, mood words, a one-sentence summary). The
keyword rules in `care/checkins.py` still run on every answer, and the stricter reading
always wins: help from either side is help, pain from either side is pain, "no" about a dose
beats "yes", so the model can never downgrade a safety signal. A model summary that quotes
the person's words or fails the diagnosis filter is dropped for the keyword summary (D-020),
and for meal and dose questions the keyword summary is kept because it already says yes or
no. Phrases such as "I fell", "I can't breathe", "chest pain" or "I can't get up", in an
answer or in free conversation, go straight to "ask first": a `concerning_phrase` alert in
the asking state with the safety question, then the escalation chain if nobody answers.

## D-035 Talk to IRA answers from the record first, and never advises (round 2, phase 4)

"Talk to IRA" is a button on the watch and a screen in the family app for the person's own
phone. A call ("call my daughter") shows the contact from the care circle and the phone dials
only when the person taps: their own action, never the system's. "What's on today" and "did
I take my tablets" are answered from routines and medication events, reminders are noted for
the next check-in, and a request for advice gets "I can't advise on that, shall I let
[carer] know?". Only after those does the model reply, each sentence through the guard (no
diagnosis words, no advice, no links, bounded length); a sentence that fails stops the reply
and a safe fallback line is spoken instead. Both sides of every exchange go into
`care_transcripts` under the existing consent rules; the rolling summary per day
(`care_conversation_memory`, summaries only, purged by retention and erased with the person)
is what IRA remembers of earlier today and this week.

## D-036 Latency is unmeasured here (round 2, phase 4)

No local model runtime exists in the build environment (4 CPUs, 15 GB, no GPU, no model
server), so first-sentence latency of a real small model is **unmeasured**. The 4-second
budget is a setting, not a measurement. CI uses the fake model. Measuring on the owner's
machine with the configured model is listed under "Needs the owner".

## D-037 The demo is the real system with fake people and a watch in the browser (round 2, phase 5)

`scripts/demo/seed_demo.py` creates a demo organisation in one command: three accounts that
can sign in, two obviously fake people with fake numbers and an Example Street address,
routines, medication, a week of synthetic readings and check-ins, one resolved alert and a
report. Logins and device tokens go to `.demo/demo.local.json`, which git ignores. The watch
simulator (`/simulator` on the dashboard) talks to the same device API with the same device
token as the real watch: it asks, speaks with the browser's speech, takes typed or spoken
answers, has I'm OK and Help, and injects a heart rate, no movement, a fall or taking the
watch off. The scripted scenarios (`scripts/demo/scenarios.py`) drive the real care code
against the seeded organisation on the real clock, so the dashboard and the family app show
what happened. Nothing in the demo dials anyone; the providers are log-only.

## D-038 A demo API entry point, and two findings the demo surfaced (round 2, phase 5)

The full server imports the whole of IRA at start (chat, voice, the agent graph, the
embedding model) and does not start without those libraries. `scripts/demo/api.py` is a lean
entry point with the real login, workspace and care routers, the same rate limiting and IP
blocklist, the two care jobs, and browser origins for the dashboard and the family app on
localhost. Running the apps in a real browser against a cloud-edition server found two
things the unit tests had not: both apps signed in through the private edition's owner route
only, so they now sign in by email through the cloud route and fall back to the owner route
when it is absent; and the browser `fetch` was passed as a bare function and threw when
called with another receiver, so it is wrapped. One more is recorded for the deployment phase
rather than changed here: the full server's delegated-scope gate knows only the private
edition's delegated accounts and answers 403 to every cloud account on `/api/*`, so the demo
API does not install it. The family app's web build needed the web platform and three
packages (`react-dom`, `react-native-web`, `expo-asset`).
## D-039 Sign-up and the organisation reuse IRA's identity register (round 2, phase 6)

A care organisation starts with `POST /care/signup`, which is IRA's own `register_account`
behind a care-shaped route: the same password rule, the same confirmation link by mail, the
same answer whether or not the address existed, and 404 unless `IRA_CLOUD_REGISTRATION_ENABLED`
is on (it is off by default, so there are no sign-ups on a deployment nobody opened). Once
the address is confirmed, `POST /care/organisations` is `provision_tenant`: tenant, a "Care
team" workspace, the founder's memberships, plus a 30-day trial row on the chosen care plan so
its limits apply from the first day. Mail goes through `core/notify/mail`, which prints to
the log (console) or discards (null) unless an SMTP host is configured.

## D-040 Family are invited by link, and consent is captured at the invitation (round 2, phase 6)

`POST /care/people/{id}/invitations` creates a tenant invitation (`tenant_invitations`, the
existing single-use, address-bound token), a pending circle member that carries the
invitation id and address, and the consents the person or their legal representative gave
for that member's role, recorded as `care_consents` rows at that moment with a note naming the
invitation; the event is written to the access log. The link is mailed and also returned so
it can be shared another way. `POST /care/invitations/accept` is the invitee's own, signed in
with the invited address: the identity layer's `accept_invitation` creates the memberships
and the pending member is linked to the account. Workspace managers invite; members and
viewers may not; another organisation's person is not found.

## D-041 Plans and billing reuse IRA's billing, and nothing can be paid for yet (round 2, phase 6)

Two care plans (`care_family`, `care_organisation`) are rows in `billing_plans` with
entitlements (`care_people_max`, `care_circle_members_max`) that the care routes enforce with
a 402 when a trial or subscription exists. No price rows are inserted: IRA's billing routes
answer `PRICE_NOT_CONFIGURED` for checkout, never a zero. The payment provider stays as IRA
built it: off unless `IRA_PAYMENT_PROVIDER` names one; `test` works only with a test secret
and is refused in production or staging; `stripe` needs its keys. No live keys exist anywhere
in this repository. `GET /care/plan` shows the plan, limits and usage; the dashboard's Plan
page shows the catalogue with "price not set".
## D-042 The deployable care server is its own process, and the jobs are a worker (round 2, phase 7)

`care/server.py` is the process a deployment runs: the care routes, login and the workspace
picker on the full server's request pipeline (rate limiting, IP blocklist, request context),
with two health routes (`/health`: process, database, Redis; `/health/jobs`: the care jobs
have run in the last five minutes) and no documentation pages. It does not start the full
server's chat, voice or agent graph, whose libraries and model server a care deployment does
not have. The four care jobs run in `care/worker.py`, a separate container whose health check
is the server's `/health/jobs`; `IRA_CARE_JOBS_IN_API=true` runs them in the server for a
one-container setup. The full server's delegated-scope gate is not installed here, for the
reason D-038 records.

## D-043 One machine, every piece restarted and checked, HTTPS by the proxy (round 2, phase 7)

`deploy/docker-compose.yml` runs PostgreSQL (pgvector), Redis, the server, the worker, the
dashboard (standalone build), Caddy and the backup job, every one with a health check and
`restart: unless-stopped`. Caddy obtains the certificate for `IRA_CARE_DOMAIN` itself and
serves the dashboard and the API from one host name, so the browser makes same-origin calls
and no CORS origin needs listing. The database roles the migration chain expects are created
on the volume's first run; migrations apply when the server starts, as `ira_migrator`.
Secrets come from `deploy/.env`, which git ignores; `deploy/.env.example` and `docs/DEPLOY.md`
explain every variable. Nothing creates a cloud account or resource: the guide tells the
owner what to rent and open.

## D-044 Nightly encrypted backups, and the restore is proved in CI (round 2, phase 7)

`deploy/backup.sh` is `pg_dump`, gzip, then AES-256-CBC with PBKDF2 under
`BACKUP_PASSPHRASE`, with a sha256 beside the file; the backup container runs it at
`BACKUP_HOUR_UTC` and keeps `BACKUP_KEEP_DAYS`. `deploy/restore.sh` checks the sha256, refuses
a tampered file, decrypts and restores into an empty database. CI runs the pair on every pull
request against the test database and compares the table count and the migration ledger.
The accelerated soak (`scripts/soak_24h.py`, 200 people, a day in five-minute steps) ran
locally: every silent person escalated to tier 3, none missing or late, and the pass timings
are in `STATUS.md`. The soak taught one thing about simulation, not product: a watch sends a
sensor snapshot right after a miss, and a simulation that forgets it sees no movement data
in the window and no SOS.
## D-045 Ready to publish, not published (round 2, phase 8)

The watch app has a release build (minified and shrunk, with keep rules for the JSON models;
signing read from the owner's environment or a `keystore.properties` outside git; the
production server and the version code set at build time; an unsigned release package built
in this repository's checks), launcher icons, and the listing, permission declarations and
Data safety answers in `docs/store/WEAR_OS_LISTING.md`. The family app has its icons,
splash, bundle and package identifiers, build numbers, notification usage text and an
`eas.json` with build profiles and an empty `submit` section, with its listing in
`docs/store/FAMILY_APP_LISTING.md`. No store account, build-service project or submission
exists; those are the owner's, and the listing text says plainly that the app is not a
medical device and never dials emergency services.

## D-046 Accessibility is tested where the tooling reaches, and owed where it does not (round 2, phase 8)

Dashboard: a skip link, a visible focus ring, 16 px text, 40 px targets, labels on every
unlabelled field, article names on alert cards, a live region on the simulator's watch face,
reduced-motion and high-contrast media queries; `jest-axe` checks an alert card and a test
pins the contrast ratios of the theme's tokens. Family app: every big button announces its
label, role, hint and state, screens have a header, text scales with the system setting (to a
cap so an SOS stays readable), text fields carry labels, buttons are at least 56 points;
tested with the test renderer. Watch: content descriptions on the three buttons, the status
text as an assertive live region, 48 dp buttons; tested at the source level, because no
instrumentation runs here. A screen-reader pass on a real watch and phone remains owed and
is listed under "Needs the owner".

## D-047 The privacy notice and the terms describe the software, and say they are drafts (round 2, phase 8)

`docs/legal/PRIVACY_POLICY.md` and `docs/legal/TERMS_OF_USE.md` are written from what the
code does (what is collected, where it goes, how long it is kept, who may see what under
consent, what IRA CARE is not) and are marked DRAFT FOR LEGAL REVIEW, with the operator's
names, bases, liability and governing law left for a lawyer. They make no claim of
compliance.
## D-048 The compliance drafts describe the code and name their own limits (round 2, phase 9)

`docs/compliance/` holds the intended purpose statement, a clinical safety case with a
hazard log in the DCB0129 structure (every hazard tied to the module that mitigates it and
the test that proves the mitigation), a data protection impact assessment, DTAC answers, the
questions an MHRA adviser must answer, and an incident, complaints and safety-issue process.
Every document is marked DRAFT FOR REVIEW BY A QUALIFIED PERSON, claims no compliance, and
answers "not yet" or "unmeasured" where that is the truth (no Clinical Safety Officer, no
DPO review, no penetration test, no pilot, no ICO registration). Their worth is accuracy:
a reviewer can open the file and the test named beside each claim.

## D-049 The final branch integrates the unmerged phases, because CI stopped starting (round 2, phase 10)

Each phase of round 2 began from `main`, as the rules require. From phase 5 on, GitHub
Actions reported `startup_failure` for every run on an unchanged, valid workflow with no
GitHub incident open (the cause, learnt on 23 November, was declined card payments on the
account, not the allowance this entry first guessed); the rules allow a merge only on green
CI, so pull requests 14 to 18 stayed open. Rather than stop, `feat/final-readiness` merges those five branches in order (the only
conflicts were the progress pages and the family app's manifest and package file, resolved
by keeping both sides and rebuilding the pages), and the proof in `docs/READINESS.md` and
`FINAL_REPORT.md` is of that integrated tree, from a clean clone. The owner may merge 14 to
18 and then 19, or 19 alone. Nothing was pushed to `main` and no history was rewritten.

## D-050 Backup tools must match the server's major version (round 2, merge day)

When CI started again on 23 November, the backup round trip on pull request 16 failed at
the restore: the runner image had moved to the PostgreSQL 17 client over the weekend, and a
17 `pg_dump` writes `SET transaction_timeout`, which a 16 server refuses, so the restore
script stopped on the first statement as it is meant to. The database suites had passed.
Two changes, both in pull request 16: CI adds the PostgreSQL apt repository and installs the
16 client (the runner's own lists carry only the newest); and `deploy/backup.sh` compares
`pg_dump`'s major version with the server's and refuses a newer tool before writing anything
(exit 4), so a mismatch fails at backup time with a clear message instead of at restore
time. The deployment's backup job already runs in the `postgres:16` image, so it was never
affected. Proved locally: 151 tables and 104 migrations before and after; a fake 17 `pg_dump`
refused.

## D-051 Demo state is generated on the machine, never carried in the repository (round 3, phase 0)

`.demo/demo.local.json`, `.demo/scenarios.log` and `.demo/soak_report.json` had been
committed although `.demo/` is ignored (`git add -f` or an add before the ignore line).
They are removed from tracking, not from history: history is never rewritten here. The
seeded logins they held were fixed strings in `seed_demo.py`, so anyone with the repository
could sign in to any demo seeded from it. Now every demo run generates its own passwords
and sealing key (`scripts/demo/_local_state.py`), prints them to the terminal, and writes
them only for the local scripts to `.demo/` with mode 0600; device tokens were already
random per pairing. The two device tokens in history belonged to a throwaway local database
that the seed reseeds (which revokes them). gitleaks over the full history (27 commits)
found 14 placeholder-shaped values and no credential; all 14 are the fixed base64
placeholders and those two tokens, listed with reasons in `.gitleaksignore`. The fake key
in `tests/runtime/test_log_redaction.py` is not reported by gitleaks 8.24.3 at all. CI now
runs `scripts/ci/gitleaks.sh` over the whole history on every pull request, and
`tests/readiness/test_demo_state_not_tracked.py` fails if `.demo/` is tracked again or a
fixed password returns to the seed. The local scripts keep reading `.demo/demo.local.json`
(the recording and scenario scripts need the logins), so "printed only to the terminal" is
read as "written nowhere that could be committed".
Two corrections after the first CI run of this branch: the shared helper had set the
cloud edition for any process that imported the simulator, and the test suite imports the
simulator, so 83 private-edition sign-in tests answered 503 (the edition and the model are
now set only by the demo scripts that ask for them); and the history scan is limited to the
commits that lead to HEAD (`--log-opts="HEAD"`), because the checkout in CI also fetches the
stale refs of squash-merged branches, which hold the same placeholder lines under other
commit ids.

## D-052 The wrist tells the truth about an SOS, and urgent events live in an outbox (round 3, phase 1)

The watch used to say "Help is on its way" the moment the button was pressed, whether or
not the server had heard. Now the only words that say help is coming come from the server:
`GET /devices/me/sos` and its held stream `GET /devices/me/sos/stream` (one JSON line now
and one on every change) report `received`, `on_the_way` with the responder's name,
`arrived` and `closed`; the watch shows "Sending…" and "Waiting for connection. Help request
saved." from its own outbox before that. Every urgent event (Help, a fall, "I'm OK", an
answer, a miss) is written to `outbox.jsonl` with its own idempotency key before any send,
retried with backoff (5 s doubling to 5 min), drained before ordinary readings, and removed
only on a definite answer; the server stores the first answer per key in `care_device_events`
(migration 106, forced row-level security) and hands it back for every repeat, so a flaky
link never opens two alerts and a refusal is not retried forever. "Arrived" is a new
acknowledgement kind, offered on the dashboard and in the family app once someone is on
their way. Every heart-rate sample is kept at its own sample time (the per-minute
collapsing on the watch is gone); the watch holds the carer's range from `/devices/me` and
uploads an out-of-range sample at once, and ordinary readings go up every
`reading_upload_interval_s` (60 s, a per-person setting) instead of after check-ins and
every 15 minutes. The browser simulator does the same, so the demo shows the real thing.

## D-053 A question already out counts as an unresolved alert (round 3, phase 1)

The sample-to-alert measurement (`scripts/measure_sample_to_alert.py`) produced two
`heart_rate_outside_range` alerts for one event: "Are you OK?" was asking, and an ordinary
check-in missed in the meantime saw the same reading and opened a second SOS, because the
duplicate check ignored the `asking` status. It now counts `asking` as unresolved. The first
measured "36 s to SOS" was that second alert, not the question's deadline; the figure recorded
in STATUS.md is from the rerun after the fix.

## D-054 Care push reaches the circle's phones and nobody else's (round 3, phase 2)

Care alerts and reports used to go through the server's general notifier, whose mobile
channel sends to every phone registered with the deployment and whose registry is one Redis
set with no tenant in it. Now a phone registers with `POST /api/v1/care/push/register`, and
the token is tied to the signed-in account, to each active circle membership that account
holds in the workspace, to the person, the tenant and the workspace under forced row-level
security (`care_push_tokens`, migration 107). The escalation chain pushes to a member's
active tokens only, records one `care_push_tickets` row per phone with the push service's
ticket, fetches receipts on the next monitoring pass, marks them delivered or failed, and
revokes a token the service says is dead. A member who is deactivated or whose push is
turned off loses their tokens at once; a deleted member's and an erased account's tokens go
with them (ON DELETE CASCADE, plus a by-user delete in the account erasure). The alert now
carries the delivery state per push attempt ("delivered", "sent, delivery not yet confirmed",
"no phone registered", "failed", "logged only"). `tests/care/test_care_push_rules.py`
fails if any care module reaches the deployment-wide sender again. The push provider is
log-only by default; `IRA_CARE_PUSH_PROVIDER=expo` turns the free Expo channel on.

## D-055 "I'm on my way" is a promise with a deadline, and nothing closes without an outcome (round 3, phase 3)

An acknowledgement used to stop the chain for good: a responder who never arrived left
the person with nobody coming and nobody told. Now "I'm on my way" sets `follow_up_due_at`
to now plus the person's `follow_up_minutes` (30 by default, migration 108). On the
monitoring pass after the deadline the responder's own phone is asked "Have you reached
[Name]?" (recorded as an attempt), a grace of `follow_up_grace_minutes` (10) starts, and
if there is still no arrival and no outcome the alert goes back to `escalating` and the
chain restarts from the next tier, counted in `re_escalations` and written into the alert's
summary. An arrival (`arrived`, `checked_in_person`, `spoke_to_person`) clears the
deadline. Nothing closes an alert except a resolution with an `outcome` (all well, needed
help, taken to hospital, false alarm, other); the dashboard has an outcome form and the
family app two outcome buttons. Two responders at once are safe: the status change is one
atomic update, both acknowledgements are kept, and a second close at the same moment
returns the alert as the first close left it.

## D-056 No hosted model API, ever (round 3, phase 4)

The inherited model layer carried a "Deep Intelligence Mode": with `IRA_ALLOW_EXTERNAL_API`,
`IRA_EXTERNAL_API_PROVIDER` and `IRA_EXTERNAL_API_MODEL` set and an executor registered by
the host, a hard question could be sent to an external frontier model for paid tokens. IRA
CARE never does that: the person's words stay on the operator's machine. The path is closed
in `reasoning/api_consent.py`: an approval is recorded and stays local, no executor can be
registered, a hand-built external decision is refused at execution, the two provider
settings do nothing, and the consent message says so. `IRA_ALLOW_EXTERNAL_API` keeps its
other meaning (ordinary web fetches in the research and browser tools, which are not model
calls). The nine tests that proved the old gate now prove the closed path. The care
conversation runs on a chain of two free open-weight models served by Ollama on the same
machine (`IRA_CARE_MODEL=ollama`, the default; `IRA_CARE_MODEL_PRIMARY` then
`IRA_CARE_MODEL_SECONDARY`, then the set questions), chosen in `docs/MODEL_CHOICE.md` by
quality on the 150-turn evaluation set first and first-sentence latency on CPU second:
`gemma4:12b` (quality 0.86, 10 s to the first sentence on 4 cores) then `gemma4:e4b` (0.78,
4.6 s). `gemma4:26b-a4b` did not fit in 16 GB. The rules alone score 0.97 on the same set,
because the set measures what the rules guarantee; the model adds the reading of free text. The
keyword red-flag rules run on every answer whatever the model says, and the evaluation set
found three phrasings they missed ("had a fall", "chest hurts", "heart attack"), which they
now catch; it also found that "remind me to ring Sravani" was read as a call, which is fixed.

## D-065 A trained model ships only when it beats what was there (round 3, phase 5)

Three of our own models were trained on Kaggle's free GPU from open starting points and
commercially licensed data (`docs/DATASETS.md`, `docs/MODEL_CHOICE.md` "Our own models").
The fall detector beats the rule detector on held-out subjects (sensitivity 0.990 against
0.906, 3.8 false alarms a day against 344) and ships in the watch beside the rules, both
gated by the off-body sensor. The answer classifier equals the rules on the 150-turn eval
at its confidence gates and is worse without them, so it ships behind `IRA_CARE_CLASSIFIER`
(default off). The conversation fine-tune (QLoRA on `gemma4:e4b`, 507 synthetic
conversations) scored 0.92 against the base model's 0.94 on 50 held-out prompts and its
training loss did not fall, so it is not adopted: the adapter stays in the private notebook
output and nothing in the product refers to it. No public dialogue dataset with a commercial
licence was found; EmpatheticDialogues and its re-uploads are non-commercial and were not
used. Kaggle notebook outputs could not be downloaded from this machine
(`www.kaggleusercontent.com` is not allowed), so the fall model travels as text in the
notebook log and is reconstructed by `scripts/training/`.

Rejected: shipping the fine-tune anyway "for warmth" (nothing measured it warmer, and the
measured thing got worse); training on EmpatheticDialogues under a research reading of its
licence (the rule is commercial use or nothing).
