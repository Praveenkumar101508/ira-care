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

