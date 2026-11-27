# 1-hour wall-clock soak on the pool fix (round 9, 26 to 27 November 2026)

The complete 1-hour soak of the API after the pool change of
[`MEMORY_INVESTIGATION_ROUND9.md`](MEMORY_INVESTIGATION_ROUND9.md) (connections and prepared
statements kept rather than expired and reopened), with the criteria of
[`HOW_TO_RUN_24H.md`](HOW_TO_RUN_24H.md) unchanged. The previous complete run on the #123 fixes
failed on memory alone at 5.18 % an hour ([`WALLCLOCK_1H_FIXED.md`](WALLCLOCK_1H_FIXED.md)).

**Result: PASS on every criterion. API resident memory grew 1.996 % an hour.**

| | |
|---|---|
| Code | the round 9 branch at the pool fix (`utils/db.py`: `max_inactive_connection_lifetime=0`, `max_cached_statement_lifetime=0`) |
| Machine | the shared build container, 4 CPUs, 15 GB RAM; local PostgreSQL 16.15 and Redis 7; database `ira_memsoak` of its own, migrated to 123 |
| Started | 23:03:37 UTC, 26 November; ran 3,601.8 s to completion; observed coverage 1.0, no gaps, no resumes |
| Load | 2,588 requests; p50 9.5 ms, p95 15.9 ms, p99 24.1 ms, max 325 ms; 0 server errors, 0 client errors, 0 connection errors |
| Availability | 720 of 720 health checks |
| Alerts | 11 Help requests issued, 11 observed, 0 lost, 0 duplicated, 0 replay mismatches; all closed at the end |
| Outbox and worker | 0 stuck outbox rows; dispatch retries 0; heartbeat age at most 32.7 s against the 180 s limit |
| Restarts | API 0, PostgreSQL 0 |
| API memory | 128.2 MB after warm-up to 134.1 MB at the end (max 134.1): **1.996 % an hour** (limit 5.00) |
| PostgreSQL memory | flat at 29.6 MB |
| Database | 23.2 to 23.6 MB (0.35 MB an hour); rows grew as the load wrote them (vitals 1,284 to 3,762, check-ins 70 to 114) |
| Files | [`wallclock_1h_round9_summary.json`](wallclock_1h_round9_summary.json), [`wallclock_1h_round9_checkpoint.jsonl.gz`](wallclock_1h_round9_checkpoint.jsonl.gz) (no logins in either) |

## All the complete 1-hour runs

| Code | API growth per hour | Verdict |
|---|---|---|
| `main` before any fix (#117, two runs) | 6.65 %, 6.45 % | FAIL |
| the three fixes of #123 | 5.18 % | FAIL |
| the pool fix of round 9 (this run) | **1.996 %** | **PASS** |

## What this means, and what it does not

- The memory criterion that stood between the product and the 24-hour soak is met on a 1-hour
  run: the reconnect bursts that stepped the resident memory up are gone, and what remains is
  under the limit with margin. Whether 2 % an hour levels off or continues only a long run shows.
- **This is not the 24-hour soak (G2.e).** That needs an uninterrupted day on a machine the owner
  controls, following `HOW_TO_RUN_24H.md` exactly. The build container cannot provide it: its
  daemons are stopped whenever the session is idle (two earlier attempts at this very run died
  that way at 18 and 43 minutes, and only a run held open by a tracked process completed).
- Load profile, data and providers are the harness's: fictional people, log-only messages, no
  model server. Nothing here is a capacity test (G2.f).
