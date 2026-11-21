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

## D-007 Inherited database tests that fail in this repository (phase 1)

Running the database suites for the first time found 9 tests that could not pass here.
One, in `tests/tenancy/test_p4_scheduler.py`, wanted a dedicated worker entry point
(`scripts/staging/run_scheduler.py`) that was not carried over; IRA CARE needs one, so it
was written and the test passes. The other 8, in `tests/readiness` and `tests/callers`,
check parts of the source project that were not carried over: two release documents under
`docs/cloud`, the "untestable items" record, and four removed routes (files, agents,
technical library, self knowledge). They cannot pass without re-adding what was removed.

Decision: the CI database job runs every database suite unchanged, so these 8 show as
failures in the `Backend database tests` job. No test is edited, skipped or deleted, and
the job is not narrowed around them: hiding them would be the one thing the build rules
forbid. The 8 are listed in `progress/STATUS.md`. **The owner decides** whether to retire
them (as the split already did for 165 test files of the same kind) or to restore what
they test; until then the database job stays red on exactly these 8 and nothing else.

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
