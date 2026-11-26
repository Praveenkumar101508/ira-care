# 1-hour wall-clock soak on the memory fixes (26 November 2026)

The two 1-hour runs in [`WALLCLOCK_1H.md`](WALLCLOCK_1H.md) failed one criterion: the API's resident
memory grew 6.65 % and 6.45 % an hour against a 5 % limit. #123 found and fixed three causes (an
unbounded consumed-ticket set, an escalation pass that re-read every push receipt in history, and a
reference cycle per request in Starlette's `BaseHTTPMiddleware`). This is the complete run on that
code, merged with `main` (`758e353`), with the criteria unchanged.

**Result: FAIL, on memory growth alone: 5.18 % an hour.** Lower than before, still above the limit.

| | |
|---|---|
| Code | `fix/api-memory-growth` merged with `main` (758e353) |
| Machine | the shared build container, 4 CPUs, 15 GB RAM; local PostgreSQL 16 and Redis |
| Started | 11:48:12 UTC; ran 3,603 s to completion; observed coverage 1.0, no resumes |
| Load | 2,316 requests; p50 about 9 ms, p95 23.8 ms; 0 server errors, 0 client errors |
| Availability | 720 of 720 health checks |
| Alerts | 19 issued, 19 observed, 0 lost, 0 duplicated, 0 replay mismatches; all closed at the end |
| Outbox and worker | nothing stuck; heartbeat always under the 180 s limit |
| API memory | 129.2 MB after warm-up to 139.3 MB: **5.18 % an hour** (limit 5.00) |
| Postgres memory | flat |
| Files | [`wallclock_1h_fixed_summary.json`](wallclock_1h_fixed_summary.json), [`wallclock_1h_fixed_checkpoint.jsonl.gz`](wallclock_1h_fixed_checkpoint.jsonl.gz) |

## All the runs so far

| Code | Run | Observed | API growth per hour | Verdict |
|---|---|---|---|---|
| `main` before the fixes | run 1, run 2 (#117) | 1 h each | 6.65 %, 6.45 % | FAIL |
| ticket and delivery fixes | 3-hour run, killed by the container's restart | 2 h 04 | 6.23 % | incomplete |
| ticket and delivery fixes | profiling run 4 | 30 min | 1.0 % | too short to count |
| plus the middleware fix | 2-hour run, killed by the same restart | 1 h 04 | 5.29 % | incomplete |
| plus the middleware fix | profiling run 5 | 30 min | 3.0 % | too short to count |
| all three fixes, merged with `main` | **this run** | **1 h** | **5.18 %** | **FAIL** |

## What this means

- The three fixes are real: each was an unbounded growth or a cycle per request, each has a
  regression test that fails on the old code, and the growth is lower. They stay.
- Something else still grows at about 5 % an hour on this machine under this load, about 10 MB in
  the hour. The profiling runs with the collector left alone suggest much of what remains is
  collectable garbage waiting for a full collection, and glibc arena retention; neither is
  identified to a line of code yet. The next step is a longer profiling run with `memtrace.py`
  (`IRA_DEBUG_MEMTRACE`) on the fixed code, which the night did not leave time for.
- The 24-hour requirement (G2.e) is unchanged: not started, because this container restarts.
  Whether a 5 % hourly growth matters over a day depends on whether it keeps growing or levels
  off, which only a long run shows: on a founder machine, follow
  [`HOW_TO_RUN_24H.md`](HOW_TO_RUN_24H.md).
