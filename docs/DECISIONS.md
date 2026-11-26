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

## D-057 A visit is a promise with a window, and the record of it is kept (round 3, phase 6)

A scheduled visit (`care_visits`, 109) names the carer, the window and the tasks. The
server, on its own clock on every monitoring pass, marks a visit with no recorded arrival
*late* `visit_late_minutes` after the window opened (15 by default: a notice to the
coordinator, never an SOS) and *missed* at the window's end (urgent: it escalates through
the care plan like any other alert). Each transition also asks the person an extra check-in
in plain words ("Your carer has not arrived yet. How are you doing?"), so nobody waits
unnoticed; no answer then follows the ordinary missed-check-in path. An arrival, recorded by
the carer on the phone or the dashboard, closes those alerts with the arrival as the
outcome, tells the watch "[Name] is here", and pauses scheduled check-ins ten minutes at a
time while the carer is in the room: IRA does not interrupt a visit with a question. A
departure records the outcome, notes, which tasks were done and a handover for the next
carer. That is the record of care given, so the rollback refuses to drop a table holding
an arrival or an outcome, and a visit that has started cannot be moved or re-described.
The watch is told nothing about lateness: "coming" reads the same whether the carer is on
time or late, because the lateness is the coordinator's problem, not the person's worry.
The timeline on the dashboard names the source of every line (the watch, a carer, IRA,
the server) so a reader can tell the person's own word from a carer's note from the
system's inference. Rows belong to the person cared for and go with the person and the
tenant; `care_visit_tasks` go with the visit.

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

## D-058 IRA is personal to the person, and the person holds the pen (round 3, phase 7)

A communication profile (`care_communication_profiles`, 110) says how IRA speaks to one
person: pace (the watch's speech rate), prompting level (whether and how soon a question is
repeated), and which channels are used (voice, touch, vibration; a safety question is always
spoken). The person or their representative edits it; a carer or administrator may too, and
the record says who did. Familiar topics, topics to avoid and the person to offer when a
worry repeats are stored only once the person's `topics` consent is recorded; without it the
lists are cleared, so nothing is used that the person did not agree to. When the same worry
comes back (worry words now, and two earlier turns today that share most of their words), IRA
does not argue, correct or diagnose: it acknowledges, offers a familiar topic if there is one,
and offers to let the named person know. The rule lives in `care/conversation.py` so the
evaluation set exercises it (12 turns added: repeated worries and timed reminders; the first
mention of a worry is answered, not redirected). A model sentence that names an avoided topic
is dropped before it is spoken.

Reminders are real (`care_reminders`): "remind me to ring Sravani at three" is read on the
person's own clock (three in the afternoon, tomorrow if it has passed; "in 20 minutes"; "this
evening") and becomes a question the watch asks at that time, in IRA's words, instead of the
scheduled question due then. The answer is read plainly: yes, later (asked again in ten
minutes), or not today; no answer twice is "missed". A medication reminder confirms in two
steps ("Have you taken it?" then "You've taken X. Is that right?") before a dose is written
down as taken, because a single misheard "yes" must not become a record. Hydration prompts
are a per-person setting (`hydration_every_minutes`, off by default, at least an hour) in
waking hours. The person or their representative can read, change or clear what IRA
remembers about any day; an edit is marked with who made it. A concern alert carries a
summary labelled "summary by IRA" and a link to the transcript row it came from; the
person's words stay in the transcript, shown only to a role with their consent. On the watch
the fall countdown also listens: "I'm OK" ends it as the button would, "help" asks now, and
no other words are read into anything.

## D-059 One watch app for Google's Wear OS and for our own AOSP watch (round 3, phase 8)

Our own watch will run plain Android (AOSP) with no Wear OS licence and no Google Play
services. The app therefore has two build flavours of one code base: `wearos` for development
boards and shop watches (readings through Health Services, which is Google's) and `aosp` for
our hardware (readings through the Android sensor framework, against the contract in
`docs/hardware/SENSOR_HAL.md`). Every platform dependency sits behind an interface: sensors
(`SensorSource`: Health Services, the framework, or a scripted simulated driver that marks every
reading "simulated" and is never chosen unless the build asks), location (the platform
`LocationManager`, one fix per SOS), speech (`Speaker`, `Listener`, with `SpeechCapabilities`
saying whether the image has an engine), haptics (`Haptics`), connectivity (`Connectivity`),
and push (`ServerChannel`: the server's own held connection to the wrist; the app has never
used Firebase and now cannot by accident). CI builds and unit-tests both flavours and runs
`scripts/ci/check_watch_aosp_deps.sh`, which fails the build if the `aosp` runtime classpath
names Play Services, Firebase, the Health Services client or the Wearable library. The
Jetpack libraries (Compose for Wear OS, AndroidX) stay in both flavours: they are open-source
and run on AOSP without any Google service. The sensor arithmetic (unreliable heart-rate
samples dropped, step totals to deltas with a reboot read as zero, off-body to wrist state)
is in `SensorEventMapper`, with no Android in it, so the JVM tests prove it for both drivers.

## D-060 Our watch exists first as a script, a model and a blueprint (round 3, phase 9)

The hardware is designed in the open before any board exists: `scripts/hardware/build_watch.py`
builds the watch in Blender from parameters in millimetres, with every part a named object
that carries what it does for the person and which feature uses it, renders it with Cycles on
the CPU (hero, front, side, back, exploded with labels, cross-section, on a wrist with its
dock, a 15-second turntable), and exports glTF; `docs/hardware/viewer.html` is one
self-contained page (three.js, MIT, embedded with the model) that turns, zooms, explodes and
names parts on a phone. The display in the model shows the app's own home screen, drawn from
the watch app's layout (the words, colours and buttons of `MainActivity.kt`). Components are
candidates, each with a reason, an alternative and the manufacturer's page; the figures were
read from the manufacturers' own pages where their sites allowed it and are marked *not
verified* where they did not (Analog Devices, ST, NXP, Knowles, Panasonic and several others
refuse automated readers): a hardware engineer verifies every row by hand before a board is
drawn. Two findings from that research shape the design: the wearable-class Qi transmitter
controllers found are end-of-life, so the dock needs a current part or our own controller; and
the simplest AOSP-capable compute path is Qualcomm's W5+ Gen 1 under NDA documentation,
with a module-based alternative for freedom. `BLUEPRINT.md` is written for an endorsing body
in plain English, with every figure labelled measured, read-from-source or estimated, and a
UK certification roadmap researched from gov.uk, legislation.gov.uk, EUR-Lex, ETSI, the IEC
webstore, GCF, PTCRB and GSMA on 24 November 2026. The intended purpose decides whether the
watch is a consumer product or a medical device; kept to the language of
`docs/compliance/INTENDED_PURPOSE.md`, it is a consumer product. Nothing in this phase is
claimed built: the renders are of a model, the power budget is an estimate, and the folder
says so.

## D-061 One app, three experiences, and the look is the person's own (round 3, phase 10)

There is one Android and iOS app. Who you are to the people in it decides what it shows
(`GET /me/experience`): the person wearing the watch (a circle member whose relationship is
"self") gets the wearer's home (Help first, the next visit, reminders, Talk to IRA, who sees
what); family get the person (the alert that needs someone first, with "I'm on my way",
updates, the watch's last sync, a switch between the people they belong to); a carer, a
clinician, a care-organisation member or an administrator gets today's visits with "I've
arrived", the outcome and the handover. Four tabs carry everything: Home, Care (the care
record), Alerts, Settings. The appearance is the person's own and travels with their account
(`care_user_preferences`, 111; `/me/preferences`): a wallpaper (a colour, one of four
built-in pictures drawn by the app, or their own photo as a data URL of at most 500 KB), light
or dark or the phone's choice, text size, high contrast, less motion, and the order of their
home cards. Two rules do not bend to it: words stay readable (a scrim over any picture or
photo, and a plain colour that clashes with the text gets one too, proved by the contrast
checks in `appearance.test.ts`) and the urgent actions keep their fixed colours (Help, the
alert banner, "I'm on my way"), drawn first whatever order the person chose. Help from the
phone (`POST /people/{id}/help`) opens the same SOS the watch would, marked "phone", after a
confirmation step. A photo wallpaper is kept on the server only (the phone's secure store is
too small for it); everything else is also cached on the phone so the app looks right before
the server answers. Account erasure removes the appearance with the account.

## D-062 The dashboard is a care record, read in the order a coordinator needs it (round 3, phase 11)

The overview answers four questions at a glance, live (`GET /overview`, refreshed every 30 s):
who needs attention now and why (an alert waiting for someone; a responder who said they were
coming and has not arrived; a visit that is late or did not happen; no check-in answered
since the usual wake time), which visits are due or overdue, which alerts await a response and
who is on the way, and which watches to check (not synced within the person's setting, off
the wrist, low battery, sensor permission off). The person's record opens on a one-line
headline (alerts first, then the watch, the last check-in and the next visit) and ten tabs:
overview, profile (address and, for managers only, the entry instructions behind a button
whose every reading is written to the access log; during an alert the people called see them
on the alert instead), health and wellbeing (each reading with when it was recorded and when
it reached the server, and called fresh, stale or unavailable in those words), visits and care
plan, the live timeline with the source of every line, alerts with delivery state, follow-up
deadline and outcome, IRA (how IRA speaks, reminders, what IRA remembers, the person's own
words only under consent, and the carers' handovers), reports, search and access. The daily
and weekly reports now carry the visits (done, missed, late, tasks, outcomes, handovers), the
reminders (confirmed, skipped, unanswered; a dose confirmed in two steps) and what IRA
remembers, each line a summary by IRA and never the person's words; a report still refuses
to say a diagnosis.

## D-063 One product look, and nothing a developer wrote on any screen (round 3, phase 12)

One design system (`docs/design/DESIGN_SYSTEM.md`): the dashboard's CSS tokens are the phone
theme's values (light and dark; the dashboard follows the system setting, the phone the
person's synced setting), the same radii, spacing, pills, buttons and 40 px targets. The watch
in the browser moved from `/simulator` to `/watch`: a round black screen inside the phase-9
case with the red help button, the words IRA says, I'm OK, Help and Talk to IRA, on a clean
page without the dashboard header, paired by the code typed on the face. Everything a
presenter needs (server, device key, the demonstration's hardware: a heart rate, no movement,
a fall, taking the watch off; the location sent with an SOS; what the watch said and heard)
lives on `/presenter`, which drives the watch in another tab of the same browser over a
BroadcastChannel and is never in a recording. `/simulator` redirects to `/watch`.

Developer words left the screens: "tier N" became "called first / second / last" and "family
and carers told"; the server address sits behind "Advanced", closed by default, on every
sign-in screen; the access log names roles and what was looked at in words, not identifiers
and table names; the demonstration people are Ada Hartley and Bert Lowe (made up), the
organisation "Lowbridge Care", with no "Demo" label anywhere. Two static tests
(`dashboard/src/__tests__/no_developer_words.test.ts`, `mobile/src/__tests__/no_developer_words.test.ts`)
fail the build if a visible string in any page, component, screen or shell contains API,
token, "tier N", JSON, inject, simulator, "Demo Person", a raw identifier word or an address;
the presenter page is outside the rule.

Every screen was captured at phone (390 × 844, 2×) and desktop (1280 × 800) by
`scripts/screens/capture.mjs` into `docs/screens/` and reviewed. The review found and fixed:
the dashboard header overflowing a phone; the watch page carrying the dashboard header; five
person-record tabs wider than a phone (tables now scroll inside their card); the phone app's
record tabs overflowing (they wrap); the overview route crashing on an untyped timestamp
parameter (a phase-11 defect the screenshots caught); the access log listing every entry
with raw role names (now the latest 100, in words); and "workspace admin" in the header
(now "care manager"). Not changed: the watch keeps saying "This is now resolved." while the
server reports a closed SOS, as the real watch does.

Rejected: a separate "demo mode" switch on the watch page (the controls would still be one
tap away in a recording); hiding the presenter behind a password (it is a local page with no
secrets, and a password would be one more thing to type on stage).

## D-064 The demonstration names the two people who build IRA CARE, and no one else real (round 3, phase 13)

The demo seed, scenarios, recording, screenshots and demo script now name Praveen Kamineti
as the care coordinator (the dashboard's care manager, in Ada's and Bert's circles as the
carer called first) and Sravani Perni as family (Ada's daughter, Bert's niece, the family
member called first). The people being looked after stay made up, with one name each: Ada
Hartley and Bert Lowe. Ada also has her own account, linked to her circle with the
relationship "self", so the phone app's wearer experience can be signed into and recorded.
E-mail addresses are at example.com, telephone numbers in the UK range reserved for fiction
(+447700900000 to +447700900003) and addresses in Lowbridge, a town that does not exist.
`docs/BUILD_RULES.md` says so under "Demonstration data" and
`supracloud-jarvis/ira/tests/test_demo_data_rules.py` checks every demonstration file for
other e-mail domains, other telephone numbers, other full names, "Demo Person" and the old
placeholders. The 68 screenshots in `docs/screens/` were retaken with this data; the wearer's
four screens are new.

Rejected: using the builders' real e-mail addresses or numbers in the seed (the rule against
real contact details holds for the builders too); a third real name for the viewer account
(it is "Care viewer", a role).

## D-066 Staging is a clean clone brought up the deployment's own way, and the proof is the journey (round 3, phase 14)

`scripts/staging/clean_clone_proof.sh` clones this repository's HEAD into an empty directory
and brings IRA CARE up as `docs/DEPLOY.md` and `deploy/docker-compose.yml` do: the
deployment's PostgreSQL and Redis containers (`deploy/compose.staging.yml` only maps their
ports to the developer's machine), a fresh Python 3.12 environment with the server image's
requirements, the migration chain applied by the image's own entry point as `ira_migrator`,
the care server and the care worker as the image starts them, the dashboard built and served
standalone as its image serves it, the phone app exported for the web, the demo organisation
seeded, then `e2e/` against it: five journey tests (the coordinator adds a person and makes
a pairing code; the watch pairs with the code typed on its face; the server asks a real
check-in that the watch answers, through the real model; a heart rate outside the range opens
an alert the family member answers on the phone with "I'm on my way", "I've arrived" and an
outcome, and the watch says each of those; a visit is planned, arrived at and handed over) and
two outage drills with timings (Redis stopped under a help press: the server reports
unhealthy at once, the watch says "Sending…" and keeps the request, the request reaches the
care team as soon as Redis returns; PostgreSQL stopped: unhealthy at once, healthy half a
second after it returns, no restart). Every step's timing and result goes to
`docs/evidence/staging/clean_clone_proof.json`, `e2e_results.json` and `outages.json`.

What the proof is not: the two application images are not built here and Caddy does not get
a public certificate, because this machine's network allows no package index inside a
container build and has no domain name. Everything those images run is run from the same
files; building them is listed as needing the owner's machine.

The proof found six defects before any person did, each fixed in this phase with a test
where one fits: (1) the compose file had an invalid volume block (the deployment would not
have started); (2) the server image applied migrations at start without the migration
tool's authorisation, which it refuses in production, so the container would have exited:
now the operator's `IRA_MIGRATE_ON_START=true` in `deploy/.env` carries that authorisation
through the entry point, and with it off the operator migrates by hand; (3) the care server's
browser-origin rules did not allow the watch's `X-Idempotency-Key` header, so a browser watch
could never send help, a fall or an answer (`test_care_server.py`); (4) pressing Help while
"Are you OK?" was being asked left that question pending, so the watch was asked again after
the SOS (`test_care_sos_db.py`); (5) the watch kept saying "This is now resolved." for as
long as the server reported the closed SOS, and a carer's arrival could not be seen behind
it: the face now says it for a minute and puts a carer in the room first (`face.test.ts`);
(6) an empty stored server address made the watch poll the wrong host; it falls back to the
default.

The film (`docs/film/ira_care.mp4`) is made from this stack by `scripts/film/record_scenes.mjs`
(Playwright recordings of the watch, the phone and the dashboard; the presenter's tab drives
the watch and is never in frame) and `scripts/film/make_film.sh` (the Blender turntable as the
opening, the renders as the closing, the narration in `docs/film/NARRATION.md` spoken by Piper,
MIT, with the `en_GB-cori-high` voice whose training data is public domain; H.264, 1280 × 720,
5 minutes 30 seconds, 3.9 MB).

Rejected: faking the stack with the demo API (`scripts/demo/api.py` allows browser origins
the real server does not; the proof must run `care.server`); a screen recording made by hand
(it could not be remade, and it would not have found the six defects).

## D-067 CI runs what a change can affect, and never runs an identical tree twice (round 3, phase 15)

Before this phase every pull request and every push to `main` ran the whole workflow: about
21 billed minutes a run (the database suite 13, the unit suite 6, the three client jobs and
the small jobs the rest), so about 42 minutes a phase, half of them spent re-testing on
`main` the very tree that had just passed as the pull request
(`docs/evidence/ci/minutes.json`). Three rules now apply, none of which removes a test:

1. **Change detection inside the workflow** (`scripts/ci/changed_areas.sh`): the detect job
   lists the files the event changed; a client job runs when its directory (or something
   shared: the workflow, the CI scripts, the migrations, the deploy files) changed, and the
   backend jobs run when backend files or something shared changed. A documentation-only
   change runs the detect job and the secret scan. An unknown base (a new branch, a force
   push) counts as "everything changed".
2. **An identical tree is not tested twice** (`scripts/ci/already_tested.sh`): on a push to
   `main`, the detect job asks the Actions API for the last thirty successful pull-request
   runs and compares their heads' trees with the pushed commit's tree. A squash merge of a
   pull request that was up to date with `main` has the same tree, so the heavy jobs skip;
   any difference, doubt or API error runs them all.
3. **Caches**: pip (now also for lint), npm, Gradle, and the Next.js build cache.

Superseded runs were already cancelled (`concurrency`). The expected cost is about 21
minutes per phase instead of 42, less when a pull request touches one area. The measured
numbers after the change are recorded in `docs/evidence/ci/minutes.json` from the first runs
on the new workflow (the phase 15 pull request and the push that merges it).

Rejected: path filters in the workflow's `on:` block (GitHub then reports no check at all
for a skipped required job, which blocks merging or, worse, invites marking the job
non-required); a nightly schedule instead of per-push runs on `main` (a broken `main` would
be found hours late); a larger or self-hosted runner (paid, or the owner's machine).

## D-068 Every readiness row is in one of three states, and CI checks it (round 3, phase 16)

A readiness row used to say things like "built", "should work" or "tested in CI", which a
reader could not act on. Every row of `docs/READINESS.md` now carries exactly one of three
states, and `scripts/check_docs_truth.py` fails CI (job "Readiness tells the truth") if a row
says anything else or names a proof file that is not in the repository:

- **works (proved by …)**: an automated test, a recorded run or a measured number, named in
  the row, that anyone can run or open.
- **needs hardware (…)**: built and unit-tested, never run on the real device; the row says
  which device.
- **needs the owner (…)**: only the owner can do it (an account, a key, a review by a
  qualified person, a machine with a domain name); the row says what.

A row that is partly one state and partly another is split into two rows. The same check
runs over the tables of `docs/progress/FINAL_REPORT.md`. The sweep also corrected states that
had gone stale as pull requests merged (round 1 and round 2 rows in `docs/progress/STATUS.md`
still read "open" or "stacked") and recorded in the final report why the two tests that fail
on the build machine fail (D-004; the egress guard needs a name server).

Rejected: a fourth state such as "partly works" (it is the state that let rows go vague);
checking only `READINESS.md` (the final report makes the same kind of claim and is read more).

## D-069 The conversation model stays loaded, is asked less, and the faster model leads (round 4, phase 1)

Round 3 measured about 10 s to IRA's first sentence with `gemma4:12b` and 4.6 s with
`gemma4:e4b` on the 4 vCPU deployment machine. Round 4 set a target of first spoken words
within 1.5 s (p50) and the first full sentence within 3 s (p50) on the same machine, with
red-flag recall still 1.00 and quality within 0.03 of the best measured model. What was
measured and what was kept is in `docs/MODEL_CHOICE.md` ("Round 4"); the decisions are:

1. **The model is kept loaded.** Every Ollama request carries `keep_alive`
   (`IRA_CARE_MODEL_KEEP_ALIVE`, 30 minutes), the care server loads the primary at start, and
   the worker's `care_model_warm` job touches it every ten minutes. A cold load cost 12 to
   16 s on the first turn after a quiet spell; now no turn pays it.
2. **One model loaded at a time** (`OLLAMA_MAX_LOADED_MODELS=1` in the compose file). With
   three models resident the kernel killed Ollama's model server twice on the 16 GB
   machine. The secondary loads only when the chain falls back, which costs one load on
   that turn; the worker warms only the primary.
3. **A shorter prompt and a smaller context window** (`IRA_CARE_MODEL_NUM_CTX`, 1024). Gemma 4
   uses sliding-window attention, so Ollama cannot reuse the previous turn's prompt cache and
   every prompt token is processed again at roughly 100 tokens a second on four cores. The
   system prompt is shorter, the memory handed to the model is capped at 240 characters and
   each context line at three items, and the language is named in words ("British English")
   rather than as a code.
4. **The faster model is primary where quality allows.** The 162-turn evaluation was re-run
   for every candidate on the new prompt; the chain is chosen on quality first within the
   0.03 band, then latency. The chosen pair is in `care/conversation.py` (`DEFAULT_PRIMARY`,
   `DEFAULT_SECONDARY`) and in the compose file.
5. **Sentences reach the client as they are spoken.** The talk route used to collect the
   whole reply and only then write the sentence lines; it now writes each sentence the moment
   the guard passes it, so the watch speaks the first sentence while the model composes the
   second.
6. The Ollama image in the compose file moves from 0.13.0 to 0.40.18: Gemma 4 needs 0.30.9
   or later, and the staging proof ran against 0.40 on this machine.

Measured and not kept: `num_thread` 4 (no better at the median, worse at the 95th
percentile); the quantisation-aware 4-bit build `gemma4:e4b-it-qat` (2.0 s to the first
sentence, but quality 0.844, 0.065 below the best, outside the band); `qwen3.5:2b` as the
secondary (quality 0.834; it writes the mood words as a string, so no answer fitted the form).
No quantisation below 4 bits is published for these models in Ollama's library. Rejected
without measuring: a hosted model API (D-056 stands).

## D-070 An instant acknowledgement from the rules, spoken before the model's first sentence (round 4, phase 1)

A person who has just spoken should hear something within a second or so, the way a
listener says "mm" before answering. The model's first sentence takes two to four seconds on
four CPU cores whatever the prompt, so when the model path is taken the rules choose one
short line and the watch speaks it at once: "Let me think, Ada." for a question, "I see, Ada."
or "I'm listening, Ada." for something said, "I'm here, Ada." when the words carry worry. The
model's sentences follow. The lines say nothing about the content, so they can never be wrong,
never carry a question, and never replace a rule reply (a call, the day's plan, the tablets, a
reminder, a red flag: those are instant already). The acknowledgement is spoken before the
reply and recorded in the transcript as part of what IRA said, but it is not one of the reply's
sentences: the guard, the eval's sentence counts and the "two or three sentences" limit apply
to the model's words alone. `IRA_CARE_INSTANT_ACK=off` switches it off.

Rejected: speaking the model's first clause at a comma (an unfinished sentence read aloud by
a synthesiser sounds like a stutter, and it would make the sentence count depend on
punctuation); a fixed single line (repeated every turn it grates; four variants per kind, chosen
at random, do not).

## D-071 Scheduled check-in questions are composed a few minutes before they are due (round 4, phase 1)

The watch collects a due check-in and the server composes its question from the person's
context with the model, which took the model's seconds before the watch could speak. The
scheduler's pass (every minute) now composes the question of any check-in due within the next
five minutes and keeps the words on the row while it is still "scheduled"; when the watch
collects it, the prepared words are asked at once (`prepared: true` in the response). The
choice of question is made for the scheduled time, not the time of composing, so a dose due at
ten is asked about at ten whether the words were composed at five to or at ten. Safety
questions ("Are you OK?"), the morning check and reminder questions keep their own words and
need no preparation. A check-in the scheduler did not reach in time (the worker was down, the
watch asked early) is composed on collection exactly as before.

Rejected: a new column for the prepared words (the row's `question_text` is empty until the
question is composed, so an empty text already says "not yet prepared", and a prepared row
still reads correctly in every report); preparing every check-in at scheduling time (fifteen
to twenty minutes early the context can change: a dose confirmed, a visit started).

## D-072 One product look for the watch, and an exploded view built from the real stack (round 4, phase 2)

**Choice.** The watch has one finish wherever it appears: a graphite case (`#2B2E33`, darker
`#1C1F22` for the bezel and back), a coral Help button (`#E2574C`), a slate strap (`#2F3A44`)
and the app's own colours on the screen (I'm OK green, Help red, Talk to IRA blue on black).
The colours live once, in `LOOK` in `scripts/hardware/build_watch.py`; `parts.json` carries
them to the viewer and `dashboard/src/app/globals.css` uses the same values for the browser
watch. The exploded view is computed from each layer's real height: from the back cover up,
every layer sits 7 mm above the top of the one below, so nothing hides inside the case; the
order is cover glass, display, display frame, case with buttons, logic board, battery, sensor
board, back cover. The same offsets go into `parts.json` (`explode_mm`) so the viewer explodes
identically. Labels are drawn after rendering, dark text on pale panels, with a leader line
that ends in a dot placed on the top face of the part it names; the strap, clasp and floor are
left out of that one view. Lighting is softer (key 14 W, fill 5 W, rim 10 W, exposure -0.3) so
the graphite reads as metal rather than a washed-out grey. The turntable is 20 seconds.

**Why.** The round 3 renders mixed a bright steel case, a pure red button and labels whose
lines ended on the wrong parts, with the logic board inside the case. A single look and a stack
derived from geometry make every image agree with the others and with the description in
`docs/hardware/BLUEPRINT.md`.

**Checked.** Every render, the turntable frames and the viewer (headless Chromium, exploded)
were reviewed at full size before merging.


## D-073 Only a loaded model is asked inside a turn; the browser watch speaks each sentence as it arrives (round 4, phase 3)

**Found while recording the film against the real model on staging.** Five faults, each
now covered by a test that fails without its fix:

1. **A cold chain never loaded.** Once Ollama had unloaded the primary (keep-alive ended,
   a restart, or the second model pushed it out under `OLLAMA_MAX_LOADED_MODELS=1`), each
   turn started loading `gemma4:e4b` (about 16 s on four cores), gave up after its few
   seconds, and Ollama aborted the load with the request; the chain then started
   `gemma4:e2b`, whose load pushed the first out and was aborted in turn. Ollama's log
   showed the two loads alternating every 3 to 9 seconds and no turn ever reached a model:
   every reply came from the set lines. **Choice:** before asking a model, the chain asks
   Ollama what is loaded (`/api/ps`, a few milliseconds). A model not in memory is skipped;
   the chain starts one background load of its primary, given its whole time, and that
   turn answers from the set lines at once instead of after the timeouts. The next turn
   finds the primary loaded. If Ollama cannot say (an error), the model is tried as before.
2. **The browser watch held IRA's first words back.** The server streams one JSON line per
   sentence (the instant acknowledgement first, D-070), but the browser watch read the whole
   body before speaking, so the acknowledgement was heard with the model's sentence, about
   6 s late. **Choice:** `readTalk` reads the body as a stream and speaks each line as it
   arrives. The Wear OS watch already did.
3. **"Have you taken your the evening tablet yet?"** A medication or routine whose name
   starts with "the", "your", "my", "a" or "an" read badly after "your". **Choice:** that
   word is dropped from the name when it is put into the question.
4. **Warming the model made the first turn load it again.** Round 4's warm request (D-069)
   sent only the model's name, so Ollama loaded it with its default context of 4096 tokens;
   the first turn then asked for 1024 and Ollama loaded it again ("runner options changed"),
   longer than the turn could wait. **Choice:** every request, the warm one included, sends
   the same load options (`num_ctx`, and `num_thread` when set).
5. **Three seconds was too short for a real turn's first token.** The round 4 evaluation's
   prompts are short; a talk turn for a person with a care record is about 270 tokens, read
   again in full on every turn (Gemma 4's sliding window leaves Ollama no prompt cache), and
   its first token came after 3 to 6 s on four cores, so the 3-second budget handed every turn
   to the set lines. **Choice:** the primary's first-token budget is 8 s
   (`IRA_CARE_MODEL_FIRST_TOKEN_S`). The person has already heard the acknowledgement at once;
   measured on staging after these fixes, the model's first sentence follows in 3.5 to 5.5 s.

## D-074 A GPU profile: gemma4:12b then gemma4:e4b, both loaded, measured on Kaggle's free GPU (24 November 2026)

**Asked for.** The owner chose the GPU option and asked for it to be set up with Kaggle.

**What Kaggle is used for, and what not.** Kaggle's free GPU measures the profile; it does not
run it. A Kaggle session ends after at most twelve hours, has a weekly GPU quota, and accepts no
incoming connection, so the watch and the apps could reach it only through a third-party tunnel
with its own account and key (against "no paid services, no secrets"); and every conversation
would leave the care team's machine (against the privacy promise, D-056). The profile runs on
the care team's own server with an NVIDIA GPU.

**Choice.** `deploy/compose.gpu.yml`, added to the compose command, gives the model server the
GPU, keeps the models loaded side by side (`OLLAMA_MAX_LOADED_MODELS=2`) and makes the chain
`gemma4:12b` then `gemma4:e4b`, chosen by their own settings (`IRA_CARE_GPU_MODEL_PRIMARY`,
`IRA_CARE_GPU_MODEL_SECONDARY`) so a `.env` written for the CPU does not carry its models over.
Without the file the stack is the CPU stack, unchanged.

**Measured.** The backend's own code (uploaded to Kaggle as a private dataset by
`scripts/training/run_kaggle.py gpu_eval`) played the 162-turn evaluation through
`care.conversation` on a Tesla T4 with Ollama 0.40.18, every candidate loaded with the turn's own
context; three passes for the 12B chain, `gemma4:12b` and `gemma4:e4b`, two for the rest
(`docs/evidence/eval/gpu/`). Red-flag recall was 1.00 in every pass. The 12B-then-E4B chain had
the best quality (0.903 on average: 0.896, 0.904, 0.910) and read check-in answers right 12 of
15 times in every pass, against 9 for the E4B-then-E2B chain and 8 to 9 for `gemma4:e4b` alone;
its first sentence came at 0.92 s at the median and 1.14 s at the 95th percentile (averages of
the passes).

**Why not the faster chain.** The round 4 rule on the CPU took the fastest model within 0.03 of
the best, because the CPU missed the time targets; the E4B-then-E2B chain (0.895, 0.27 s) is
within that band. On the GPU every candidate meets every time target with room to spare (the
first words come from the rules at once in either case), so time no longer separates them and
the reading of check-in answers does: it is what the care record and the alerts are built on.


## D-075 An alert message is queued, then sent; its state is what the provider said (remediation A2, 25 November 2026)

The escalation chain no longer sends inside the transaction that decides who is told. It writes
each message as a `queued` row of `care_alert_attempts` plus an outbox row
(`care_alert_dispatch`), commits, and `care/delivery.py` sends after. Due alerts are claimed with
`FOR UPDATE SKIP LOCKED` and outbox rows with a two-minute lease, so two workers never tell the
same tier twice (before: three workers at once sent every text and call twice); each send has
its own 20-second limit and at most 8 run at once, so one slow provider cannot hold up another
person's alert; a failed send is retried three times with a growing wait, then recorded failed.
Delivery is at least once: a worker that dies between the send and the record may send again.

`care_alert_attempts` stays insert-only (`test_readings_and_attempts_cannot_be_rewritten`). What
a provider reports is a new row of `care_alert_attempt_events`, also insert-only; a message's
state is its furthest event. A Twilio 2xx is `accepted`, not delivered. Twilio's status
callback (`POST /care/providers/twilio/status/{tenant}/{workspace}/{attempt}`) is believed only
when both our HMAC token in the URL and Twilio's signature over the full URL and form verify;
the URL names the tenant and workspace, so the row is found under that tenant's own row-level
security, with no unscoped lookup table. A repeated or late callback is recorded and moves
nothing backwards. None of these states is a person having read the message: only "I'm on my
way" in the app is.

Two new settings turn the callbacks on, alongside the three in D-016:
`IRA_CARE_PUBLIC_BASE_URL` (the deployment's public `https://` address) and
`IRA_CARE_CALLBACK_SECRET` (a random secret for the URL token). Without either, no callback URL
is given to Twilio and a message stays `accepted`, shown as "delivery not confirmed".

## D-076 A caller sees only the people whose circle they belong to (remediation C-22, 25 November 2026)

D-010 decides what a caller may do from their workspace role. It said nothing about which
people they may do it for, and one care organisation's workspace holds many people
(`PRODUCT_SPEC.md`), while inviting a family member or carer into one person's circle makes
them a workspace `member` (`care/onboarding.py`). So a daughter invited for her mother could,
through the API, see and act on everyone else the organisation cares for. Measured on the code
before this change: of 62 routes that name another person or one of their things, none
answered "not found", and 29 succeeded. Among them: raising an SOS in the other person's name,
acknowledging their alert, cancelling their visit, changing what IRA remembers about them, and
reading their readings and medication.

The rule now:
- A workspace administrator (the care manager) and the deployment owner see every person in
  the workspace.
- Everyone else sees only the people whose circle they are an active member of.

It is enforced as route dependencies (`person_guard`, and `resource_guard` for a route named by
an alert, visit, reminder, routine, medication, consent, report, device or circle member). A
person outside the caller's circles is refused as "not found", so their existence is not
revealed. `/people`, `/alerts` and `/overview` are filtered the same way. Leaving a circle
ends access at once.

A static test fails if any route naming a person or one of their things lacks the guard. A
database test calls every such route for a person outside the caller's circle and expects 404.

Consequence: a care-organisation staff member who is not a workspace administrator must be
added to a person's circle to see them. The circle is the assignment.

## D-077 The SisFall-trained fall model is research-only; release builds use the fall rules (owner decision, 25 November 2026)

The watch fall model was trained on SisFall from Zenodo record 22212281. That record is labelled
CC BY 4.0 but is not the authors' own release (creator "None", published 2026-08-31), so the
dataset's commercial terms are unconfirmed (PR #65, `docs/DATASETS.md`). The owner does not
accept uncertain dataset licensing risk.

Decision: the model is excluded from every commercial configuration until its rights are
confirmed in writing by the SisFall authors or a replacement trained only on commercially
licensed data passes evaluation.

How: the model files moved from `watch/app/src/main/assets` to `watch/app/src/debug/assets`.
Release builds, and so the IRA CARE OS image (which takes the `aospRelease` APK), carry no model;
`TfLiteInference.load` returns null and the rule detector runs alone, as it already did whenever
no model was present. Debug builds keep the model for engineering demonstrations, labelled as
research-only. `FallModelReleaseTest` fails if the model reappears where a release build would
take it.

Cost: the rule detector is noisier on the same evaluation windows (344 false alarms a day against
the model's 3.8, `docs/MODEL_CHOICE.md`); its sensitivity was 0.906 against 0.990. A replacement
model trained on commercially licensed wrist data is being prepared (register F4).

The copy of the model previously published on Kaggle under Apache 2.0 by
`scripts/training/run_kaggle.py` is an owner action: only the account holder can withdraw it.

**Correction (25 November 2026, D-082).** The paragraph above is wrong: no copy of the model was
ever published on Kaggle. It is kept as written; what was verified, and how:
- The original training run's own log (`docs/evidence/training/fall.json`, written by
  `scripts/training/kaggle_fall_model.py`, which `run_kaggle.py` only pushes) ends with the upload
  being refused: `KAGGLE_MODEL_UPLOAD_SKIPPED BackendError: Permission 'models.create' was denied`.
- Listing the account's models with the Kaggle API (account `Praveen0116`, 25 November 2026)
  returned "No models found".
- The training notebook `praveen0116/ira-care-fall-model-training` is private (kernel metadata
  `is_private: true`), and so are the datasets `praveen0116/ira-care-code` and
  `praveen0116/ira-care-training-data` (API `is_private: True`).

So no public Kaggle copy of the SisFall-trained model exists and there is nothing to withdraw.
Copies taken by third parties cannot be ruled out in general, but no public upload ever succeeded.
The upload call has been removed from the script, and a readiness test now fails if any training
script publishes model weights or makes a dataset public (D-082).


## D-078 A care plan is a versioned record written by care managers; staff roles are the three workspace roles, named (remediation F5, 25 November 2026)

A care plan says what a person's care is for and how it is given: goals, the support needed, the
daily routine with who does each part (a carer, family or the person), the risks to watch for,
what to do in an emergency, and when the plan is next reviewed. It is kept as one row per
version in `care_plans` (migration 116), never rewritten: a change is a new version with the
reason for it, so the plan's history is always there to show. Only a care manager (a workspace
administrator) writes a plan. Anyone who may see the person (D-076) reads it, and each read is in
the access log. Two managers saving at once cannot overwrite each other: a save that started
from an older version is refused.

Staff roles are not a fourth permission system. The three workspace roles are named as a care
organisation names its staff:
- `workspace_admin` is the care manager;
- `member` is a carer, who sees only the people in their circles (D-076);
- `viewer` is a viewer.

`care/authz.py` lists each action with the routes that enforce it and the level they check
(`ACTIONS`), and a test fails if a route's check and that list disagree. `/care/me/permissions`
returns the list for the caller. The dashboard shows only what it says. Until it answers, the
dashboard shows nothing that changes the record. Hiding a button is a courtesy; the server
checks every request.

Not decided here: a rota, a caseload view per carer, or roles finer than these three.

## D-079 The care-journey demonstration is the 22-step list (owner decision, 25 November 2026)

The register named "the 32 steps" for the live demonstration, and the master directive asked for a
22-step care journey "plus the previous 32-step master demonstration". No list of 32 steps was ever
in the repository. The owner decided: **the 22-step list is the one to use.**

The 22 steps are the directive's section 16 list. Each one runs, in order, against the real care
API and PostgreSQL in `supracloud-jarvis/ira/tests/tenancy/test_care_journey_db.py` (#79), and
`docs/demo/CARE_JOURNEY_22.md` says what each step shows and which assertion proves it. The earlier
scripted scenarios and the twelve-step browser demo (`docs/demo/RERUN.md`) remain as they are.
Requirement J.d records the journey; nothing is counted for a 32-step list.

## D-080 Fall model v3, trained only on licensed data, ships in release builds in "recover" mode (25 November 2026)

D-077 kept the SisFall-trained model out of release builds until "a replacement trained only on
commercially licensed data passes evaluation". The owner then set the gate: no sensitivity lost
against the rule detector, and the gate is not to be weakened to release a model.

v3 (`docs/ai/fall/MODEL_CARD_V3.md`) is trained only on BITS-2 and UCI HAR (both CC BY 4.0). On
held-out subjects, against the rule detector on the same 316 simulated falls and the same
daily-activity recordings, the shipped pipeline (the model's path plus the unchanged rules) caught
246 falls against the rule's 195 (sensitivity 0.7785 against 0.6171) with at most 10.30 alarms per
hour of daily activity against 52.84. The design and the threshold rule were fixed before any
held-out result was seen, and every threshold was chosen inside the training folds.

Decision: v3 goes into `watch/app/src/main/assets`, so every release build and the IRA CARE OS
image carry it; `RecoveringFallDetector` runs it. The rules keep raising every fall they raise
today. A suspected fall from either path starts the same 30-second countdown that asks first.
Debug builds keep the v1 research model for the comparison. `FallModelReleaseTest` pins the one
file that may ship, by provenance, mode and SHA-256. (Superseded for debug builds by D-082: no
build of any kind now carries v1; debug builds carry v3 as release builds do.)

What this does not decide: real use. The measurement is on young volunteers simulating falls with
a research band. Before anyone relies on it, the owner and a clinical reviewer must approve it,
and it must be measured on the IRA watch (requirement F4.g, needs a physical watch).

## D-081 The care coordinator is a carer named by the care manager; search covers names and records, never words (requirements D3.g, D3.i, 25 November 2026)

D-078 named the three workspace roles and left "roles finer than these three" undecided. A care
organisation needs a fourth: the **coordinator**, who plans visits for everyone and sees every
carer's caseload, without being able to change who is cared for, the care team, watches, safety
settings or care plans.

Decision: the coordinator is **not a new workspace role**. A care manager names a `member` of the
workspace coordinator (`care_staff_designations`, migration 117; `PUT /care/staff/{user_id}/coordinator`).
Adding a fourth value to `workspace_memberships.role` would have touched the identity migrations, the
invitation rules and the cloud role map for one care feature; a care-side designation touches none.

- **What it changes:** `authz.sees_everyone` is true for a care manager, the deployment owner and a
  coordinator, so a coordinator sees every person in the workspace (D-076's rule, widened only for the
  people the manager named) and can plan, move and cancel visits for them with the carer's existing
  act-level routes. `GET /care/caseload` shows every carer's caseload; `GET /care/visits` every visit.
- **What it does not change:** the workspace role is still `member`, so every manage-level route still
  refuses (people, circle, watches, settings, medication lists, consents, care plans, naming
  coordinators). Words under consent follow the caller's own circle role, and access notes stay with the
  care manager or a tier 1 or 2 contact during an alert, so seeing a person does not open what they said
  or their key safe. A designation on a viewer or a manager has no effect; it counts only on a `member`.
- **Screens by role:** a care manager sees every caseload, the full visit plan and the coordinator
  switch; a coordinator sees every caseload and the full visit plan and can plan visits for anyone; a
  carer sees "My caseload" and the visits of the people they look after; a viewer sees their own
  caseload (usually empty) and no action. Every screen asks the server, and every route checks again.

Search (`GET /care/search`) covers people by name, visits by purpose or carer, and alerts by kind,
across the people the caller may see. It never searches check-in answers, transcripts or care-plan
text: those stay behind the per-person history search, where consent applies. What is typed is matched
literally (LIKE wildcards escaped, passed as a parameter), searches are rationed to 30 a minute per
address, and each search is written to the access log once per person it returned, with the kinds of
record found and never the words typed.

## D-082 No build of any kind carries the SisFall-trained model; debug builds use v3 (owner decision, 25 November 2026)

D-077 took the SisFall-trained fall model v1 out of release builds and kept it in debug builds for
engineering demonstrations. The owner reviewed whether uncertain SisFall-derived artefacts should
also stay out of debug and demonstration builds, which are shown to people and can be passed on.

Decision: yes. v1 is removed from every build variant, debug included, so no build of any kind can
carry it until SisFall's original rights are established. Debug builds carry fall model v3 (D-080)
exactly as release builds do, with the rule detector as the fallback.

How:
- `watch/app/src/debug/assets/fall_model.tflite` and `fall_model.json` are deleted. Every variant
  (`wearosDebug`, `aospDebug`, `wearosRelease`, `aospRelease`) now takes v3 from `src/main/assets`.
- `FallModelSpec.mayRun` and `TfLiteInference.load`: the watch runs a model only if its spec names
  the licensed v3 provenance and the file's SHA-256 is the reviewed v3 file. Anything else (v1, an
  unlabelled or unreviewed model) is refused and the rules run alone.
- `FallModelReleaseTest` fails if any source set other than `main` carries a fall model, or if any
  file in any source set's assets is SisFall-derived (the v1 files by SHA-256, or anything naming
  SisFall). With the v1 spec put back in `src/debug/assets` it fails.
- `scripts/training/kaggle_fall_model.py` no longer tries to upload the model to Kaggle (the
  original attempt was refused; see the correction under D-077), and the conversation training
  script keeps its adapter in the private kernel's output instead of uploading it. A readiness test
  (`tests/readiness/test_training_scripts_publish_nothing.py`) fails if any training script calls a
  model-publishing function or pushes a dataset or kernel with public visibility.

Kept: v1's evaluation evidence (`docs/evidence/training/fall.json`, `docs/MODEL_CHOICE.md`) as a
historical research record. The v1 weights file itself (`ira_fall_model.tflite`, SHA-256
`8182ea37154ab52e1d9383b3de42add2a2bb902cafb38daa6056f767d84ae6cb`) is removed from the tree, so no
copy of weights with unconfirmed rights sits beside the product; git history keeps it, and
`FallModelReleaseTest` refuses that hash in any build.

Cost: none in the field; debug builds now behave as release builds do. The side-by-side comparison
of v1 and v3 on a device is no longer possible from a debug build.

## D-083 PyAV does not ship; the server transcribes WAV only (owner decision, 25 November 2026)

faster-whisper, the server's speech recogniser, declares PyAV (`av`) as a hard dependency, and
PyAV's wheels bundle FFmpeg built with the GPL-2.0-or-later encoders `libx264` and `libx265`
(the open finding in `docs/security/LICENCES.md`). The owner decided to remove that risk.

Decision: PyAV is not installed anywhere IRA ships. faster-whisper is installed with
`--no-deps` (`requirements-nodeps.txt`), the lock drops `av` through a resolver override, and
`voice/whisper_engine.py` puts IRA's own decoder (`voice/audio_decode.py`, standard library and
numpy) where faster-whisper expects PyAV's, so `import av` is never attempted.

- **What the server decodes:** WAV only (PCM or float, any rate and channel count), converted
  to 16 kHz mono with a filtered polyphase resampler. That covers every source in this
  repository: the watch and the phone recognise speech on the device and send text, the
  dashboard does not record, the wake-word listener and the lip-sync pass use WAV, and the
  LiveKit plugin passes raw PCM.
- **What it gives up:** uploaded MP3, M4A/AAC, Ogg, Opus, WebM, FLAC and video are refused with
  a message naming the format and asking for WAV (`/voice/transcribe` answers 415). They are
  not sent to an outside service instead, even when external providers are allowed: they used
  to be transcribed on the machine, and sending them out would widen what leaves it without
  anyone deciding to.
- **Why not another decoder:** every option that reads compressed formats either is FFmpeg or
  bundles LGPL native code (`soundfile` with `libsndfile`, which also carries `mpg123` and
  LAME). None is needed by a client that exists. If one is needed later, recording WAV on the
  client is the first choice; a reviewed LGPL exception for `libsndfile` is the second.
- **Accuracy:** unchanged. On LibriSpeech test-clean, run before and after in sequence on the same machine, the WER was 4.16 % on both paths with identical errors (132 / 19 / 8); `docs/ai/speech/RESULTS.md`. The 4.24 % recorded earlier came from another package set and stays as the historical benchmark.

What would change it: a client that has to send compressed audio and cannot record WAV.

## D-084 IRA Care Activity leaves the watch only as hourly summaries, under its own consent, and never alerts (requirements F7.f to F7.k, 25 November 2026)

IRA Care Activity (WISDM-trained; `docs/ai/activity/MODEL_CARD.md`)
has run on the watch since F7.d, but nothing used its answer. The owner asked for an end-to-end
integration that never presents the estimate as confirmed behaviour or a medical state and never
raises an alert from it.

Decision:

- **Summaries, not windows.** The watch keeps every 10 s window to itself and sends only hourly
  seconds per label (the model's six classes, `unknown`, `unavailable`) and per reason
  (`low_confidence`; `off_wrist`, `sensor_gap`, `too_short`, `sensor_fault`), with the model's
  version and SHA-256 and the source (`device` or `simulated`). Nothing is filled in: time the
  watch did not record stays "not recorded". Contract: `docs/ai/activity/DATA_CONTRACT.md`.
- **Its own consent kind, `activity_sharing`** (migration 121), following the existing pattern
  (`topics` was added the same way in 110). `health_data_family` was not reused: it is about family
  seeing readings, not about a new kind of inference leaving the watch, and reusing it would have
  started sending for people who never agreed to this. The watch queues only while the server says
  the consent is granted; the server checks again on arrival and on every carer read.
- **Its own table, `care_activity_periods`**, not `care_vitals`. Every safety rule reads
  `care_vitals`; a separate table is what makes "no alert path reads it" checkable
  (`tests/care/test_activity_no_alert_path.py`). Same tenancy, row-level security, retention and
  erasure as the readings.
- **Carers see four groups, not classes:** active (walking, stairs), still (sitting, standing),
  other arm movement (eating_drinking, other), and could not tell, not worn or not recorded. The
  class `eating_drinking` is never shown by name: the model confuses sitting and eating
  (`MODEL_CARD.md`), and "food_intake" stays what the person says at a check-in. Every view
  carries "Estimated from the watch's motion sensor; not a measurement of health."
- **The wearer** sees one small line ("On the move today: about N min (the watch's estimate)")
  when nothing else is on the screen.

What this does not decide: accuracy on the IRA watch or with older adults, and the battery cost of
the 20 Hz gyroscope. Both need a physical watch (F7.k and the model card's physical validation
list). The software cost per window was measured on the JVM and is in the model card.

## D-085 Deployment administrators are a grant the operator writes, never a care role; they send a watch one signed instruction from a closed list (requirements E2.m, E2.o, 25 November 2026)

The watch's device policy (E2.m) keeps developer options, factory reset and safe-mode boot locked
and accepts only server-signed instructions to open them. #88 built both ends and left the middle
open: who may ask the server to sign one. A care workspace role must never imply it. A care manager
runs a care organisation; opening the operating system of a watch on somebody's wrist is fleet
maintenance, and the people who do it work for whoever operates the deployment.

Decision: a **deployment administrator** is an ordinary signed-in account whose user id holds a live
row in `deployment_administrators` (migration 122). Nothing else confers it.

- **Identified by the existing sign-in.** No second login, token or key: the account authenticates
  exactly as everyone else does and is resolved to a Principal by `require_tenant_principal`. The
  check (`care/deviceadmin.py`) reads only the grant table, never the workspace role, the tenant
  role, the scopes or `Principal.is_owner`. A care manager, coordinator, carer, viewer, the person
  themself, a tenant owner and a principal flagged as the deployment owner are all refused (403), and
  the refusal is logged.
- **Granted the way OS releases are published.** The operator runs `scripts/deployment_admin.py`
  with the migrator's credentials, as `scripts/os_release.py` publishes releases (E4.d). The runtime
  role can write no grant: row-level security admits writes only from `ira_schema_owner`, which the
  migrator reaches with `SET ROLE`, so even a provisioning step that grants `ira_app` everything
  (R-47) does not let a request make its caller an administrator. A live grant is readable only on a
  connection bound to that same account, so the runtime can ask "is this caller an administrator?"
  and cannot list who is.
- **Revoked by the operator, effective on the next request.** The account can issue, revoke and read
  nothing more; whatever it issued that has not expired is withheld from the watch and put on the
  watch's revocation list, which closes a window it opened.
- **What it can do: three actions, one watch, at most 15 minutes.** `MAINTENANCE_WINDOW` lifts the
  developer-options restriction until the instruction expires; `RESTORE_RESTRICTIONS` puts every
  restriction back now; `FACTORY_RESET` wipes the watch (the reset #88 already allowed through a
  signed instruction). There is no field for a command, an argument or a restriction name; the API,
  the table and the watch each refuse anything else. Each instruction names one paired watch by its
  tenant, workspace and device id, and is worked on a connection bound to that workspace, so a
  mismatched triple is not found.
- **Signed as #88 signed it.** ECDSA P-256 over SHA-256 of the exact payload string, with a
  single-use nonce, verified on the watch with the key built into the image. The key is a separate
  one from the OTA manifest key and is loaded the same way (`IRA_DEVICE_POLICY_KEY`; none
  configured means 503, never an unsigned instruction). Ed25519, which the OTA manifests use, was
  not chosen because not every Android release the device-owner app may run on verifies it.
- **Audited.** Every instruction keeps who issued it and why; every step (issued, delivered,
  revoked, applied, refused, restored) is an insert-only row with its actor.
- **Amended 25 November 2026 by D-089:** `FACTORY_RESET` is no longer signed on one
  administrator's say-so. It is a request that a second, different deployment administrator must
  approve within 30 minutes; only then is it signed (migration 123). The other two actions are
  unchanged.

What this does not decide: making the device-policy app the device owner (provisioning, E4.f, needs
an image), the timer and boot receiver that call the policy on a device, a dashboard screen for
deployment administrators (the routes exist; the screen does not), and who in the operating
organisation should hold the grant (an owner decision).

## D-086 Heart-rate research mode: raw PPG from recorded or simulated signals, returned and never stored (requirement F3.g, 25 November 2026)

Current Wear OS watches give an app finished heart-rate values, not raw PPG, so the learned
candidate selector cannot run on a person's watch (F3.d stays blocked). The owner asked for the
whole raw-PPG pipeline to be built now, in research mode, on recorded (PPG-DaLiA) or simulated
signals, clearly separated from real measurements and unable to trigger an alert.

Decision: **an endpoint that processes a research recording and returns the result, with no
database write at all**, rather than a research table.

- **Where:** `care/heart_research.py` (data contract, windowing, signal quality, inference,
  provenance, built-in samples), `POST /care/research/heart/run` and
  `GET /care/research/heart/samples`, and the dashboard page `/research/heart`.
- **Provenance labels:** every heart reading's source is one of `simulated`,
  `research_recording` or `device`. The research path accepts and produces only the first two:
  `device` input is refused by the request schema and again by the pipeline, and a result object
  cannot be built with any other source, or marked as a measurement or as able to alert.
- **Why no table:** nothing about a research run belongs to a person, and a table would be one
  more place a research value could be read back as a reading. The route takes no person, opens
  no database connection and writes nothing; the result says `stored: false`. The readings table
  already refuses both research sources (`care_vitals.source` CHECK allows `watch`, `phone`,
  `manual`, `simulator`), so a future change that tried to store one would fail in the
  database; `tests/tenancy/test_heart_research_db.py` proves that, and that a research run
  writes no reading, alert, access-log or check-in row while the same values from a paired
  watch do open a safety check. No migration was needed.
- **Never an alert:** no module on the alert path (continuous rules, monitoring, ask-first,
  escalation, SOS, falls, the repository, the worker and the others listed in
  `tests/care/test_heart_research.py`) can reach `care/heart_research.py` through its imports;
  only `care/routes.py` uses it. A negative control shows the check catches an indirect import.
- **Who:** care managers and the deployment owner only (`run_heart_research`, manage level),
  bound to their verified workspace; carers and viewers get 403. Runs are limited to 10 a minute
  per address and to 600 s of signal per call, and run off the event loop.
- **Screens:** the research page is reached from the top bar (care managers only), is headed
  "Research — not a measurement of this person", and is never linked from a person's record;
  the person page has no research content (a dashboard test checks both). The page refuses to
  draw any result that is not marked research, unstored and non-alerting.
- **Thresholds** (motion rejection at 0.5 g band-limited accelerometer RMS, 2% sampling-rate
  tolerance, a gap at 1.5 sample periods, 1 s start offset) were fixed before the held-out
  evaluation and not tuned on it.

What this does not decide: using the learned selector on a person. That needs a watch that
gives the app raw PPG (F3.d) and validation on the IRA watch and older adults (F3.e).

## D-087 The API image drops three Python modules that link GPL and Sleepycat libraries (25 November 2026)

The first scan of what the API image contains (`scripts/supply_chain.py image`) found that the
base image's CPython links GNU readline and GDBM (GPL-3.0-or-later) and Berkeley DB 5.3
(Sleepycat) through the standard-library modules `readline`, `_gdbm` and `_dbm`. Nothing in the
server imports them.

Decision: `deploy/Dockerfile.api` deletes the three extension modules, and the build fails if
one is still importable. The libraries themselves stay in the image as Debian packages beside
our code (bash uses readline); that is aggregation, which the policy in
`docs/security/LICENCES.md` records rather than judges. What the policy judges is what our
components link, and none links these any more.

- **What it costs:** `import readline` fails (no line editing in an interactive Python prompt
  inside the container), and `dbm` falls back to its pure-Python `dbm.dumb` store. Neither is
  used by the server, the worker or the migrations.
- **Why not another base image:** the official Python images all build these modules; building
  our own Python, or moving to a distroless base, is a larger change than three deleted files.

What would change it: code that needs GNU readline or GDBM at run time, which would then need a
reviewed exception or a different library.

## D-088 The production API image installs a hashed subset of the lock; every artefact carries its notices (25 November 2026)

The supply-chain scan (D-087) found two things it could only record. The API image installed
`requirements-test.txt`, so pytest, moto and moto's dependencies (Werkzeug, responses, xmltodict)
shipped in production and every unpinned package floated to whatever PyPI served on the day of
the build; and the notices files stayed in the repository rather than travelling with the
artefacts they describe.

Decision:

- `deploy/Dockerfile.api` installs `requirements-api.lock` and then `requirements-nodeps.txt`,
  both with `--no-deps --require-hashes`, and the build runs `pip check` (only faster-whisper's
  PyAV may be missing, D-083). `requirements-api.lock` is a strict subset of `requirements.lock`
  (same versions, same hashes), derived by `scripts/api_image_lock.py` from
  `requirements-api.txt`, which names what the care server and worker load. The full lock is not
  installed whole: it is the whole server's tree, with torch-dependent packages
  (sentence-transformers, speechbrain) and browser automation the care image never loads.
- boto3 stays in the image: `IRA_KEK_PROVIDER=aws-kms` is an implemented production key
  provider (`core/crypto/aws_kms.py`). moto, its test double, goes.
- onnxruntime and tokenizers move from the image-only pins 1.20.1 and 0.21.0 to the lock's 1.31.0
  and 0.22.2, the versions `requirements.txt` already named and CI's tests already ran. The
  red-flag classifier was run inside the new image with them.
- The image scan treats a difference between the API image's Python packages and its locks (or
  the committed snapshot) as a failure, not a note.
- Each image copies its notices file to `/usr/share/doc/ira-care/THIRD_PARTY_NOTICES.txt`; the
  dashboard shows its own at `/licences`, the family app under Settings, and each watch build
  under About. TypeScript is left out of the dashboard's standalone output.

What would change it: a care module that needs a package outside `requirements-api.txt` (the
image build's `pip check` and the import check in CI fail until it is added and the subset lock
regenerated).

## D-089 A factory reset needs two deployment administrators (security review S15, threat T43, 25 November 2026)

The round 7 security review (S15) found that one deployment administrator with an ordinary
session could have the server sign `FACTORY_RESET` for a watch (D-085). A phished password or a
stolen session was enough to wipe the safety device on a person's wrist, and nothing asked for a
second factor or a second person. The obvious control, step-up authentication, is not available:
the session token carries no authentication time (no `iat` or `auth_time` claim; a refresh
re-stamps the token), so the server cannot tell a session signed in a minute ago from one kept
alive by refreshes for a week, and `Principal.authentication_strength` is never filled in.

Decision: a **two-person rule for `FACTORY_RESET` only** (`care/deviceadmin.py`, migration 123).

- **A request, not an instruction.** Issuing a reset stores a row in state `pending_approval`
  with no nonce, payload, signature, issue or expiry time (the route answers 202). There is
  nothing a watch could be given, and the watch's collection only ever reads signed rows.
- **A second, different, live administrator approves it within 30 minutes**
  (`POST /care/deployment-admin/device-instructions/{id}/approve`). Only then is it signed, with
  a fresh issue time and the 1 to 15 minute life the requester asked for. The requester
  approving their own request is refused (403). A request past its deadline lapses unsigned; a
  withdrawn request, or one whose requester's grant has been revoked since, cannot be approved
  (409). An account without a live grant cannot approve at all (403).
- **Revoking either administrator kills it.** Before signing, as above. After signing, the
  watch is handed neither the requester's nor the approver's instruction once either grant is
  revoked; its nonce goes on the watch's revocation list. A reset signed on one person's say-so
  before migration 123 is treated the same way: withheld and put on the revocation list.
- **The table enforces it too.** A trigger refuses a reset inserted in any state but
  `pending_approval`, approval by the requester or by nobody, approval after the deadline,
  approval of a withdrawn request, approval once either grant has been revoked since the
  request, any other change to a request, and going back from signed; CHECK constraints refuse a
  half-signed request, a request for any other action, and a deadline more than 30 minutes
  after the request. A bug in the code therefore cannot sign a reset for one person.
- **Audited.** Three new insert-only events: `requested`, `approved` and `approval_refused`
  (with the reason). A lapse is not an event (nothing happens); the history shows it as
  `status: lapsed`.
- **`MAINTENANCE_WINDOW` and `RESTORE_RESTRICTIONS` are unchanged:** one administrator, signed
  at once. A maintenance window cannot destroy anything, and restoring restrictions only makes a
  watch safer.

What this does not decide: step-up authentication. It stays open (S15, T43) until the session
token records when its holder last proved who they are (and with which factor); `security/reauth.py`
can already re-check a password, and TOTP exists, so the next step is an authentication-time
claim and a recent-authentication check on the approve and issue routes. Who should hold the
grant, and whether two holders are on call at once, is an owner decision; a deployment with a
single administrator can no longer reset a watch remotely.

## D-090 The conversation fine-tune of 25 November 2026 is not promoted; the base model stays (requirements F2.e to F2.g)

A QLoRA fine-tune of `google/gemma-4-E4B-it` on the care-conversation dataset 1.1.0 was trained on
Kaggle (2 x T4, private kernel) and judged against the base model on the held-out test split and the
product evaluation, under a promotion gate written into the script before the run.

Decision: **keep the base model.** The fine-tune raised the product evaluation's quality from 0.839 to
0.908 and the test split's product pass rate from 0.903 to 0.983, but it stopped flagging help on 6 of the
9 held-out help answers (escalation 0.818 to 0.273) and said "my instructions" in 2 of 20 injection turns
(injection resistance 1.0 to 0.9). The gate allows no safety measure to fall, whatever the quality gain.
The adapter stays in the private kernel output and is not shipped or published. Record:
`docs/ai/conversation/FINETUNE_CARD.md`, `docs/evidence/training/conversation_finetune_2026-11-25.json`.

## D-091 The advice guard refuses telling the person what they have, not every "you have" (hazard H7, 25 November 2026)

The final verification of round 7 found the guard's pattern `you (probably )?have\b` refusing
harmless sentences: in three of eight real-model talk replies the person heard only the first
sentence, because the second was "I hope you have a peaceful evening, Edith." It also refused the
product's own check-in question "Did you have your lunch yet?" (`docs/ai/conversation/DATASET_CARD.md`).

Decision: replace that alternative with `care/conversation.py::_tells_them_what_they_have`, which
refuses anything it cannot place as plainly social:

- any "you have" or "you've got" followed in the same sentence by a health word ("I hope you
  have a fever" is refused);
- any hedged or inferred "you have" ("you may have", "you probably have", "sounds like you
  have", "I think you've got") unless what follows is plainly social ("you must have been so
  proud");
- any plain "you have" that is not after a wish or a question ("I hope you have", "Did you
  have") and not before something plainly social ("you have a beautiful garden", "you have
  family visiting", "you have done so well").

The earlier pattern let hedged diagnoses through ("you may have a urine infection", "you might
have the flu", "sounds like you've got a virus"); these are now refused. The price: a few harmless
hedged sentences are refused too ("you may have missed a bus", "I wish you could have seen the
sunset"). A labelled corpus pins all three groups (`tests/care/test_care_advice_guard_corpus.py`):
the earlier pattern fails 26 of its 33 must-allow sentences and 13 of its must-refuse sentences;
removing the rule fails 44 must-refuse sentences. On the deterministic model the product
evaluation's scores are unchanged (162 turns, identical results). Over every reply recorded in
`docs/evidence/eval/` (2,212 sentences), the new rule refuses three sentences the old one passed,
all harmless and hedged ("you may have missed a bus", "You must have been very tired to say that",
"I'd be delighted if you could have a cup"), and changes nothing else. Those recorded replies were
all spoken, so they cannot show what the old rule refused and the new one allows; the corpus does. The hazard log (H7) and the claims
register name the corpus. A Clinical Safety Officer's review of the rule is still owed, as for the
rest of the hazard log.

## D-092 A fall detected during a running countdown is never dropped: one incident, the same deadline, asked again after an OK (hazard H4, 26 November 2026)

The cross-dataset test on WEDA-FALL (`docs/ai/fall/CROSS_DATASET_WEDA.md`) found that 20 of the
24 falls the shipped pipeline missed were detected (model probability 0.996 to 1.000) while an
earlier alarm's 30 s countdown was running, 1.4 to 5.2 s before the fall. `FallCountdown` ignored
any detection while it counted, and the server refused a second fall start with 409, which the
watch's outbox treats as final. A person who tapped "I'm OK" for the first prompt got no
countdown and no escalation for the fall itself.

Decision: a fall countdown is one **incident** with its own id, and a detection is never dropped.

- **The same impact seen twice** (impacts within 1.5 s, `FallCountdown.SAME_IMPACT_MS`, the time
  both detectors already treat as the impact itself) is counted on the incident and changes
  nothing else: no new report, no new countdown.
- **A separate detection** during the countdown is attached to the incident: the wrist vibrates
  and asks again ("I noticed another possible fall. Are you OK?"), the server records it on the
  alert it already holds, and **the deadline does not move**. With no answer, help is asked for
  at the original deadline, by the watch and, if the watch has gone quiet, by the server.
- **"I'm OK" covers only what was detected before the question it answers.** If a separate
  detection came after the question, the OK is reported for the first incident and the person is
  asked again at once with a fresh countdown and a new incident id. Each fresh countdown needs a
  new explicit OK, so nothing is put off without the person answering, and the fall's own
  escalation comes at most two countdowns after it (bounded by the first countdown's end plus one
  countdown).
- **An unclear answer cancels nothing** (as before); **Help escalates at once**, whatever else
  was detected.
- **Offline and retries:** every event carries the incident id and an idempotency key derived
  from it (`fall-<id>-d<n>`, `fall-<id>-ok`, `fall-<id>-help`); the outbox holds a key once and
  now sends strictly in order, so an incident's OK always reaches the server before the next
  incident's start (before, a newer event could overtake one that was backing off). One drain at
  a time on the watch.
- **On the server** (`care/falls.py`): at most one unresolved fall alert per person; a start while
  one is unresolved is attached to it (`details.detections`), never refused, and leaves
  `next_action_at` alone; "I'm OK" resolves only for the latest incident attached (an older one is
  recorded and answered 409, and the alert keeps asking until its original deadline); Help or the
  end of a countdown while a fall SOS is open returns that SOS instead of opening a second; every
  change runs under a per-person transaction lock, so concurrent events open at most one SOS.

Not changed: the thresholds, the model, the rule detector (still the backstop when the model is
missing or refused), and the countdown length. The price: a person who keeps moving after an
alarm (clapping, several table hits, getting up and sitting again) may be asked a second time
after saying "I'm OK". On synthetic signals a clapping bout gives at most one more question per
OK (`docs/compliance/CLINICAL_SAFETY_CASE.md`, H4). The owner asked for the missed fall to be
fixed first; alarm fatigue from hand impacts is a separate finding for the clinical reviewer.

The historical WEDA-FALL figures were produced before this change and are not re-run until the
dataset's licence is confirmed; `scripts/eval/fall_cross_weda.py --countdown d092` mirrors the new
behaviour for that re-run. Tests: `FallCountdownOverlapTest.kt` (written against the API before
the fix: five of its seven tests fail on the old code), `FallCountdownIncidentTest.kt`,
`tests/tenancy/test_care_fall_overlap_db.py` (all six fail on the old server),
`tests/readiness/test_cross_dataset_eval.py`.

## D-094 Heart research mode: when the learned selector is unsure, the window is unavailable, not answered by the classical estimator (requirement F3.g, 26 November 2026)

The research pipeline (`care/heart_research.py`, D-086) reported the classical estimate whenever
the learned selector's probability was below 0.5 and the classical confidence was at least 0.3. Two
evaluations found those answers poor: on held-out PPG-DaLiA subject S1 they were 16.23 bpm out
(against 4.59 for the confident learned answers), and on the independent GalaxyPPG data 29.27 bpm
on the Empatica E4 and 39.44 bpm on the Galaxy Watch 5 (against 2.19 and 1.63).

Decision: **when the learned selector has run and is unsure, the window is unavailable** with the
reason `learned_low_confidence` (or `learned_no_candidates` if it found no candidate peak), and no
number. The classical candidate is still shown in the window's detail, as a candidate, never as the
answer. The classical estimate is still reported, labelled as a fallback with why, only where the
learned selector cannot run at all: no model (`no_model`) or no accelerometer (`no_accelerometer`).
Nothing else changes: thresholds, the motion gate, the provenance, and the flags
(`is_measurement: false`, `may_alert: false`, `stored: false`).

**An agreement check was considered and rejected.** The alternative was to keep the classical
answer when it is within 5 bpm of the learned candidate (5 bpm being the selector's own definition
of a correct candidate, fixed in advance). The rule for choosing was fixed before running: adopt it
only if, pooled over the 14 PPG-DaLiA subjects other than S1 (leave-one-subject-out fold models;
S1 was not used to decide, and GalaxyPPG was not used at all), the windows it would add score no
worse than the confident learned answers. They did not: 13.68 bpm on 3,377 windows, against 3.31
bpm on 39,632. When the two methods agree while the selector is unsure, they are often both
following the same wrong peak (a motion harmonic). Record: `docs/ai/heart/fallback_rule_analysis.json`,
`scripts/training/heart_fallback_rule.py`.

| 14 subjects without S1 (LOSO) | MAE (bpm) | Coverage | Reported windows over 10 bpm out |
| --- | --- | --- | --- |
| Previous rule (classical fallback) | 7.09 | 78.6% | 16.1% |
| Agreement within 5 bpm (rejected) | 4.12 | 71.6% | 9.2% |
| Unavailable (adopted) | 3.31 | 66.0% | 6.3% |

**Re-measured on held-out S1** (`docs/ai/heart/research_pipeline_eval.json`, same fold model,
SHA-256 `31670471ecfc…`): reported 7.19 bpm on 3,565 windows (77.5%) before, 4.59 bpm on 2,768
(60.1%) after; the 797 windows no longer answered (16.23 bpm) are now `learned_low_confidence`.
**On GalaxyPPG** (not re-run, because the change only relabels windows and does not change what the
estimators compute, so the "learned" rows of the existing run are exactly the new rule's output):
on the E4, 3.56 bpm on 86.4% of the windows with a reference before, 2.19 bpm on 82.0% after; on
the Galaxy Watch 5, 7.75 bpm on 78.8% before, 1.63 bpm on 66.0% after
(`docs/ai/heart/CROSS_DATASET_GALAXYPPG.md`).

What it costs: fewer answers. Coverage falls by 12.6 percentage points on the 14 PPG-DaLiA
subjects, 17.3 on S1, 4.4 on the GalaxyPPG E4 and 12.8 on the Galaxy Watch. In research mode
"unavailable" is the honest answer there. What would change it: a better fallback shown on separate
data (not S1 and not GalaxyPPG, which have now both been used to judge this one).

## D-095 A deployment administrator must have proved who they are recently: `auth_time` and `amr` token claims, `POST /auth/step-up`, and a recent-authentication check on device instructions (closes the open part of D-089; security review S15, threat T43, 26 November 2026)

D-089 left step-up authentication open because the session token carried no authentication time
and a refresh re-stamped it. Decision: every access and refresh token now carries `auth_time`
(Unix seconds of the holder's last proof with a credential) and `amr` (`pwd`, `otp`, `recovery`),
set at `/auth/token` and `/auth/cloud/login` from what was actually checked, copied unchanged by
`/auth/refresh`, and carried onto the Principal (`auth_time`, `amr`, `authentication_strength` =
mfa, password or empty). A token from before the claims decodes and works everywhere except where
recent authentication is asked for, where it counts as never recently authenticated.
`POST /auth/step-up` (rate-limited as sign-in) re-checks the password of the token's own subject
through `security/reauth.py`, requires a current code when a second factor is enabled, counts
failures against the sign-in lockout, records an auth event and returns a new access token only
(same `sub`, `tid`, `uid`, `ver`; `auth_time` now); no refresh token, so the proof cannot be kept
alive by refreshing. `security/stepup.py` refuses (403, `error_code: step_up_required`, with
`reason`, `max_age_seconds`, `auth_age_seconds`, `how`) unless the proof is at most
`IRA_STEP_UP_MAX_AGE_SECONDS` old (60 to 3600, default 600, refused at start otherwise) and, when
the account has an enabled factor, included `otp` or `recovery`; an unreadable enrolment is 503,
never "no factor". Applied to issue, approve and revoke of device instructions after the grant
check, so a non-administrator gets its old 403 and learns nothing; the history needs no step-up;
`GET /care/deployment-admin/me` tells the dashboard what to offer. The two-person rule for a
factory reset stays exactly as D-089 wrote it. What this does not do: tell a phished password and
factor from the holder's; two administrators' credentials phished within the window can still
reset a watch, and a step-up token stolen inside the window works for the rest of it. Audit events
reuse the 061 vocabulary (`sign_in_refused` and `sign_in_succeeded` with a `step-up` detail), so no
migration. Evidence: `tests/test_step_up_authentication.py` (31),
`tests/tenancy/test_device_admin_step_up_db.py` (9), `tests/tenancy/test_step_up_endpoint_db.py`
(7); `docs/security/STEP_UP_AUTHENTICATION.md`. Register E2.p. T43 is controlled; S15 fixed.

## D-096 The deployment administration screen shows nothing to anyone without the grant, and confirms who you are only when the server asks (requirements E2.o and D3.l, 26 November 2026)

D-085 left "a dashboard screen for deployment administrators" undecided. Decision: the dashboard
gets `/admin/devices`, gated by one question to the server (`GET /care/deployment-admin/me`), not
by the stored role. Without `deployment_admin: true` the page renders a one-line refusal and a link
back, no field, button, watch or history, and the navigation shows no link; the answer is kept in
memory for the sign-in, never in storage, so a revoked grant disappears on the next request. The
screen offers exactly the three actions, 1 to 15 minutes, a reason, Approve only on a request
awaiting approval and Revoke with a reason on live rows; a factory reset is explained as a
two-person request and a 202 is shown as waiting for a second administrator; the server's refusals
(self-approval among them) are shown verbatim. Step-up follows D-095's contract: any 403 whose body
names `step_up_required` opens a panel for the password (and the authenticator code when enrolled),
the returned access token replaces the stored one through the same path a renewal takes, and the
one refused action is retried exactly once; a wrong password or a second refusal retries nothing.
`dashboard/src/__tests__/device_admin.test.tsx` (22 tests; 160 in the dashboard suite);
`docs/ops/DEPLOYMENT_ADMIN_SCREEN.md`; `e2e/deployment_admin.spec.ts` is written and type-checked,
not yet run against a stack. What this does not decide: who holds the grant (D-085, the operator
script) and the Playwright run on staging.

## D-097 D-004 resolved: the published-credential table is rebuilt from this repository's own history and checked against it (26 November 2026)

`tests/test_published_credentials.py::test_the_table_matches_a_fresh_scan_of_history` needed a
scanner that was never carried over. Decision: `scripts/security/published_credential_digests.py`
scans every committed version of every environment file on every ref (deleted versions and side
branches included) for the six covered names, treats a value as published when it is not empty
and not a placeholder in the sense of `core.runtime.environment.is_placeholder` (the start-up
rule, so the CI test values are placeholders), digests each distinct value with the module's
salted scrypt and prints provenance only. `--include-inherited` merges the module's table, which
is the source project's provenance: IRA CARE's history (142 commits, 12 environment-file
versions) publishes nothing, so the six digests the module ships are entirely inherited. The test
proves the scanner on a synthetic repository with planted values and, on a full clone, that
nothing this history publishes is missing from the refusal table and that the scan merged with
the inherited provenance equals the table; it still skips on CI's shallow clone and runs in the
integrated verification. Rejected: retiring the test (it is the control that keeps the table
honest) and reading the source project's history (private). `docs/security/CREDENTIAL_HISTORY_SCAN.md`.

## D-098 Environment-dependent backend tests run in CI; the inherited missing-file tests are recorded, not deleted (register K.a and K.b, 26 November 2026)

CI's database job gains a Redis 7 service (`IRA_TEST_REDIS_URL`) and a password file for the three
provisioned roles (with `PGPASSWORD` unset for the test step, because it took precedence over the
roles' own passwords), so the two durable-logout tests and the six migration role-separation tests
run there instead of skipping. The three PyAV exclusion tests gate on the installed faster-whisper
distribution (`importlib.util.find_spec`) instead of importing it, which needed PyAV, so the proof
that PyAV never ships runs wherever faster-whisper is installed. The unit job installs the OCR
tools and names every skip (`-ra`). Running the logout tests found one stale locator in test 14b
(it took the first `scope == "account"` block, which PR #22 had moved); the product was correct and
the test now finds the guard nearest the token-version bump. The destructive failover test passed
against a database of its own (5 passed) and stays opt-in. Seven inherited tests skip for files of
the source project that IRA CARE does not have and does not need
(`docs/progress/INHERITED_TEST_SKIPS.md`); retiring them is an owner decision in D-007's form.
Remaining optional-dependency skips (livekit-agents, torch, scipy, Playwright, brain-affect, the
Cortex gateway) are listed there for decision. The faster-whisper pin and its hashes were verified
correct in four clean environments; the install failure seen once on the build machine did not
reproduce and the pin is unchanged.

## D-099 The OS product layer is checked on the host against an AOSP reference, and the defects a first build would hit are fixed (register E2.a to E2.e, E2.k, E3.a, E3.c to E3.e, E6.a, 26 November 2026)

No build host exists (BUILD.md), so what can be checked without one is checked:
`os/scripts/aosp_reference.py` reads names, types and constants (never code) from the public Git
mirrors of `frameworks/base`, `build/make`, `hardware/interfaces` and `frameworks/native` into
`os/reference/aosp_reference.json`, recording what it could not reach (`device/google/cuttlefish`
and `packages/apps/Launcher3` have no reachable mirror; android.googlesource.com is refused by the
build machine's network). `tests/readiness/test_os_product_layer.py` (73 tests) and
`test_os_sensor_sim.py` (3) then check the merged aosp-flavour manifest against the privapp
allowlist and AOSP's protection levels, the first-boot grants against the runtime permissions
requested, every `PRODUCT_COPY_FILES` source and every `PRODUCT_PACKAGES` module, the product
registration and lunch choices, every overlaid framework resource and its type, the launcher
override and the HOME and lock-screen declarations, the boot animation (the generator run from the
command line, the zip stored uncompressed, `desc.txt` matching the frames), the sensors HAL
service against the contract (types, rates, modes, the AIDL version in `Android.bp`, the VINTF
fragment and AOSP's default), the simulated signal model compiled and run on the host (heart rate,
accelerometer and gyroscope, off-body), BUILD.md's commands against the scripts and variables that
exist, and the supplier register's fourteen rows. Defects fixed: a `PRODUCT_COPY_FILES` source
that does not exist in AOSP (`android.hardware.type.watch.xml` is now IRA's own file), the
sensors HAL version and VINTF fragment, a one-shot flag value, and stale names in the documents
(`docs/os/HOST_CHECKS.md` lists each). What none of this proves: that the image builds, boots, or
that a person sees what the files describe; E2.h onwards stay blocked on the host, and E1.a stays
a document.

## D-100 The watch emulator journey grows from eleven steps to the care journey; a synthetic fall exists in the debug build only (register D1.i, 26 November 2026)

`scripts/emulator/watch_smoke.py` keeps its eleven steps and continues through nine guarded groups
(`--steps`, `--until`): medication reminder with an unclear answer not recorded and a two-step
confirmation, a check-in answered by touch, a fall countdown with a second detection attached and
not overlapped (D-092), Help during a fall, the offline queue delivered once, a reboot with the
queue surviving, a sensor permission revoked and regranted, unpair and re-pair, and the
accessibility of every dumped screen. The fall is injected by `am broadcast` to
`SyntheticFallReceiver`, declared and present in `watch/app/src/debug` alone: `SyntheticFallReleaseTest`
(4) and `tests/readiness/test_watch_synthetic_fall.py` (4) fail if any other source set names the
receiver or its action, or if `main` calls `FallInjection.inject`. The pure helpers are tested with
recorded dumps and fake transports (`test_watch_smoke_journey.py`, 12), and the API side of every
group was played against the demo API (`--api-only`). The groups have not run on an emulator: the
build machine has no KVM and the workflow dispatch is refused to the session's integration, so the
owner runs `emulator.yml` (journey `care-journey`, steps `all`) from the Actions tab and the
results are added to `docs/evidence/emulator/WEAR_EMULATOR_JOURNEY.md`. Until then D1.i keeps its
eleven-step evidence and the groups are written, not proved.

## D-101 Sign-out with a refresh token ends the session row; the phone app gains a Plan tab and never shows "all well" for an unreachable server (requirements C-14.a, D2.c, G2.i, 26 November 2026)

Found while writing the phone journey's secure sign-out step: `/auth/logout` with a refresh token
blocklisted the token but, in the cloud edition, left its session row live, so the person's
session list still showed the device. Decision: the row is ended too (`revoke_session_by_jti`),
best effort after the blocklist write and logged loudly if it fails, because the token is already
dead (`tests/tenancy/test_logout_with_refresh_token_ends_session.py`). The phone app tells the
server on sign-out before clearing its state, shows the person's medication and care plan on a
Plan tab, and shows an unreachable server as such rather than as all well (`person_plan`,
`app_signout`, `shell` and `client` tests; 91 in the suite). The authenticated Android journey
itself (`phone_smoke.py` beyond the login screen) is not yet written; the HTTPS decision for the
test backend is still to be taken when it is.

## D-103 A confirmation stage after a suspected fall, off by default: a still wrist is always asked, a wrist that kept its orientation with the hand carrying on, or a burst of impacts, is not (requirement F4.i, 26 November 2026)

The detectors decide from the impact alone; what separates a slap from a fall arrives in the
seconds after it: the direction gravity reads on the wrist changes, and the arm is quiet or stirring
rather than carrying on with a task. Decision: `FallConfirmation.kt`, between a detector's
`SuspectedFall` and the countdown's question, decides four seconds after the impact from the
orientation change (mean gravity 2.0 to 0.5 s before against 1.5 to 4.0 s after), the post-impact
motion (standard deviation of |a|) and the impacts within three seconds. A wrist still afterwards
(under 0.15 g) is always asked, whatever the orientation did; three or more impacts with the
orientation within 30 degrees, or an orientation within 20 degrees with the hand moving over 0.35 g,
is not asked; anything in between is asked. Saturation of an axis is flagged, never a reason. The
model path's question comes up to two seconds later; the rule path loses nothing. The stage is
mirrored line for line in `scripts/eval/fall_confirmation.py` and the two are pinned to each other.
On 160 synthetic falls and 220 synthetic hand-impact recordings (`SYNTHETIC_SCENARIOS.md`, a
regression corpus, not accuracy) the shipped pipeline caught 154 falls with and without the stage and
alarms fell from 88 to 62. **It ships off** (`fall_confirm_before_asking`, default false): it has
been measured on synthetic scenarios only, and nothing that could lose a real fall ships on before
the permitted recorded falls (BITS-2, UCI HAR, not on the build machine) have been run with the
gate the model card uses. Rejected: tuning the thresholds to the synthetic set (it would fit
invented signals), and re-running WEDA-FALL (licence unresolved, F1). `FALSE_ALARM_INVESTIGATION.md`.

## D-104 Round 9 scenario families with a hold-out set; the red-flag rule learns the older person's idiom and the dose reader never confirms a broken-off, indefinite or other-medicine answer (requirements F5.d, C-1.b, 26 November 2026)

170 original turns in seven families (`scripts/eval/conversation_round9_scenarios.py`), 129 in a
development set and 41 held out by position with a header saying it is never tuned on. Before the
fixes the development set passed 87 of 129: the red-flag rule did not know "can't stand up", "taken
a tumble", "the room is spinning", "knocked myself out", "my legs have gone", "I'm on the floor",
"come over all funny", "can't catch my breath", "face drooping", "went down like a sack", "blood
everywhere", "words coming out wrong", "my arm won't work"; and the dose reader read "Yes I took
the— hang on, the door", "Yes yes y—", "I took something" and "The painkillers yes" as yes.
Decision: `RED_FLAGS` gains those phrases with the person as subject (a thing that has "gone all
funny" is not a flag); `read_dose_answer` reads a broken-off answer (a dash, or a turn to the door,
the phone, "hang on"), an indefinite object, or a medicine not in the question's subject as
`unclear`. After: 129 of 129, and the 162-turn set unchanged turn for turn with red-flag recall 1.0
(`docs/evidence/eval/round9/set162_before_fake.json`, `set162_after_fake.json`). A new runner check,
`no_secrecy_promise`: IRA never promises to hide something from the care circle; what the family
sees is the consent settings' decision. The hold-out set, run once: 37 of 41; the four misses ("hit
my head" twice, "the evening ones" to a question about the morning tablets, and the word "Help" in
a question about the Help button read as a call for help) are recorded, not fixed, and are the
next development set's first entries. Rejected: asserting the hold-out set in a test (it would
become a tuning target). `docs/ai/conversation/ROUND9_SCENARIOS.md`.

## D-105 The phone journey runs on the debug build with its JavaScript bundled, never on a release build with its network policy loosened; the iOS simulator reads a development-only journey probe (requirements D2.g, D2.h, 26 November 2026)

The release APK refuses cleartext HTTP, so it cannot reach the demo server at 10.0.2.2, and a
certificate the release build would trust would have to be written into the emulator's system
store, which on API 34 is an updatable APEX. Decision: the authenticated Android journey
(`scripts/emulator/phone_smoke.py` with `--api`) runs on the **debug** build, whose generated
manifest already allows cleartext (Expo's own debug source set), with its JavaScript bundled by
`scripts/emulator/android_bundle_debug.sh` (one line inside `react { }`, `debuggableVariants = []`);
the release APK is still built and still runs the sign-in-screen smoke, so a release policy change
would be seen. The journey's steps: sign in, person card, record and plan against the API, a Help
alert raised by the watch and acknowledged in the app, dark mode kept across a restart, offline and
back without "all well", sign-out with every session revoked and nothing on screen, the wearer's
Talk, the carer's isolated home. On iOS, where there is no uiautomator, the Debug `.app` is built
with its JavaScript bundled (`FORCE_BUNDLING=1`) and `mobile/src/journeyProbe.ts` logs one line per
screen change in development bundles only (`__DEV__`; a release bundle logs nothing, pinned by a
test); `scripts/simulator/ios_journey.py` boots a simulator, installs and launches the app with its
console attached and records the sign-in screen, that the process still runs and that no crash
report appeared. Neither journey ran on the build machine (no KVM, no Xcode): the pull request's
native checks run both on GitHub's standard runners, and the evidence pages record their results.
