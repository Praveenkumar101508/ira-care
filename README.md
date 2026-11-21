# IRA CARE

IRA CARE is a proactive AI companion for older people who live independently. It starts
the conversation itself, checks in about meals and medication, follows up when something
seems wrong, and alerts carers and family. Family members see alerts and summaries, not
private conversations.

**Stage: foundation only.** This repository holds the backend foundation IRA CARE is built
on. The care-specific features (the watch app, care conversations, the medication
confidence score, fall and no-response alerts, the carer dashboard and the reports) are
planned, not built. [STATUS.md](STATUS.md) lists exactly what exists and what does not.

## Where the foundation comes from

The foundation is taken from **IRA by SupraCloud**, a private, local-first AI assistant
built by Praveen Kamineti. Only the parts IRA CARE needs were kept:

- **Conversation:** IRA's chat engine, with local language models and model routing
- **Proactive behaviour:** the scheduler, the heartbeat (the assistant speaks first),
  reminders and briefings
- **Alerts:** notifications through the app, phone push, Telegram and email
- **Memory:** remembering the person, their routines and past conversations
- **Voice:** speech-to-text on the server (spoken replies are not active yet; see STATUS)
- **People and permissions:** separate logins for the person, carers and family, with
  roles (viewer, trusted user, family admin) and an audit log
- **Safety and privacy:** approval before any outbound or destructive action, a privacy
  centre with erasure, audio never stored, multi-user isolation

Parts of IRA that IRA CARE does not need (coding agents, interview preparation, meeting
capture, the desktop overlay, IRA's web interface and its build and release tooling)
were removed.

## Layout

| Path | What it holds |
| --- | --- |
| `supracloud-jarvis/ira/` | The backend (FastAPI, Python 3.11 or 3.12) and its tests |
| `supracloud-jarvis/postgres/` | Database migrations (PostgreSQL 16 with pgvector) |
| `supracloud-jarvis/scripts/` | Setup and operations scripts carried over from IRA |

The folder name `supracloud-jarvis` is kept from IRA so that existing paths, imports and
tests keep working. It may be renamed later in a dedicated change.

## Running the tests

```bash
cd supracloud-jarvis/ira
python3.12 -m venv ../.venv
../.venv/bin/pip install -r requirements.txt -r requirements-test.txt
../.venv/bin/python -m pytest -q
```

Tests that need a database run only when `IRA_TEST_DATABASE_DSN` points at a UTF-8
PostgreSQL 16 database with pgvector; without it they are skipped or refuse to run.

## Licence

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
