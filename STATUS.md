# IRA CARE: Status

**What exists, what is reused from IRA, and what is not built yet.** Nothing below is
rounded up. "From IRA" means the code was carried over from IRA by SupraCloud unchanged,
with the status IRA's own records give it; it has not yet been adapted to care use.

## Foundation (from IRA)

| Capability | Status | Notes |
| --- | --- | --- |
| Conversation (chat) | From IRA: Stable | Local language models through Ollama, with model routing and fallbacks |
| Remembering the person | From IRA: Stable | Memory save and recall with pgvector search |
| Proactive check-ins | From IRA | Scheduler, heartbeat (the assistant speaks first) and reminders; not yet care-specific |
| Alerts | From IRA | Notifier: in-app, phone push (off by default), Telegram, email |
| Daily summaries | From IRA | Briefing generator; its content is IRA's (calendar, tasks), not care reports yet |
| Speech to text | From IRA: Beta | Local faster-whisper transcription on the server |
| Spoken replies | Not active | IRA's speech engine is present but not installed, pending a licence decision. The watch's built-in Android voice can be used instead |
| Logins and roles | From IRA: Stable / Beta | Owner login, separate logins for others, roles viewer / trusted_user / family_admin, audit log |
| Approval before actions | From IRA: Stable | Outbound or destructive actions need the owner's approval |
| Privacy | From IRA: Not launched | Privacy centre with erasure; audio is never stored |
| Multi-user isolation | From IRA: Not launched | Row-level security across tenants and workspaces; built and tested, not launched |
| Billing | From IRA: Not launched | Plans, entitlements and invoices code; no real payment taken |

## Care features (planned, not built)

| Feature | Status |
| --- | --- |
| Wear OS watch app (Samsung Galaxy Watch 4 prototype) | Planned |
| Care conversations: meal check-ins and follow-ups | Planned |
| Medication check-ins and the medication confidence score | Planned |
| Fall and no-response alerts to carers | Planned |
| Carer dashboard with charts | Planned |
| Daily carer report and weekly family report | Planned |
| Care-specific privacy rules (family sees alerts, not conversations) | Planned |

## Verification (21 November 2026)

Measured on a Linux workspace with Python 3.12, without a database (tests that need one are
skipped or refuse to run without `IRA_TEST_DATABASE_DSN`):

- Every kept backend module imports, except `voice/agent.py`, which needs the optional
  LiveKit agents package (the same as in IRA).
- The API starts with 169 endpoints, all identical to IRA's.
- Full test run of `supracloud-jarvis/ira/tests`: **4,691 passed, 174 skipped, 0 failed,
  0 errors** (275 s). The skipped tests need a database or optional packages.
- Before the split, the same kept tests on unchanged IRA code gave 5,338 passed,
  197 skipped and 12 errors (database-only tests refusing to run without one).
- 165 test files and 23 test functions were not carried over, because they only check
  IRA parts that were removed (IRA's documents, CI workflows, infrastructure, launchers,
  release tooling, or removed features). Nine audit lists had the names of removed
  files taken off; three counts (two minimums, one exact) were set to IRA CARE's measured numbers.

## Known gaps

- No CI workflow was carried over. Tests run locally only.
- `supracloud-jarvis/start-ira.sh` still expects IRA's web interface; start the backend
  with `--skip-frontend`, or run `uvicorn main:app` from `supracloud-jarvis/ira`.
- `supracloud-jarvis/INSTALL.md` is IRA's install guide and refers to some IRA documents
  that were not carried over.
- Database tests have not been run for IRA CARE yet. They need PostgreSQL 16 with pgvector.
