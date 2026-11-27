# Where the API's remaining memory growth came from (round 9, 26 November 2026)

After the three fixes in #123 the complete 1-hour soak still failed its memory criterion alone:
API resident memory grew 5.18 % an hour against a 5 % limit
([`WALLCLOCK_1H_FIXED.md`](WALLCLOCK_1H_FIXED.md)). That page guessed at collectable garbage waiting
for a full collection and at glibc arena retention, neither traced to code. This page records the
profiling run that found the cause, the mechanism, the fix, and the confirmation run.

**Finding.** The pool's connections churned. asyncpg closes any connection idle for 300 seconds and
does not keep `min_size` after that, so between bursts the pool shrank to one or two connections and
then reopened four or five at once, roughly every ten minutes. Each reopening is a TLS handshake, a
256 KB read buffer and a freshly filled statement cache; the closed connections' memory went back to
glibc, which kept it as free-but-kept heap (3.9 MB to 14.7 MB over the run) rather than returning it
to the kernel. Resident memory stepped up at each burst and was flat between bursts. Prepared
statements expired on the same 300-second timer and were re-prepared, each expiry leaving a small
reference cycle, which was most of what a forced full collection found and freed; freeing it returned
no resident memory at all.

**Fix.** `utils/db.py`: `max_inactive_connection_lifetime=0` (a connection, once opened, is kept for
the life of the process, bounded by `max_size=20`) and `max_cached_statement_lifetime=0` (a prepared
statement stays until the cache's LRU bound of 100 evicts it). Regression tests in
`tests/runtime/test_db_pool_keeps_connections.py`.

## Method

- Database `ira_memsoak` (its own; the shared test database untouched), migrated to 123; local
  PostgreSQL 16 and Redis on the shared 4-CPU build container; Python 3.13; asyncpg 0.30.
- `scripts/soak_wallclock.py --start-demo --no-quiet-hours --hours 1.5` with the usual load (two
  fictional watches reporting every 5 s, a family check-in every 5 min, Help every 10 min with its
  replay, acknowledge and resolve), 16:48:02 to 18:18:05 UTC: 3,760 requests, 0 server errors.
- The demo API started by the harness with `IRA_DEBUG_MEMTRACE` (scripts/demo/memtrace.py):
  tracemalloc on with 5 frames, a record every 120 s, the collector left alone
  (`IRA_DEBUG_MEMTRACE_COLLECT=0`) and every fifth record followed by a forced full collection that
  describes its garbage by type and origin (`IRA_DEBUG_MEMTRACE_PROBE_COLLECT_EVERY=5`). Baseline at
  120 s; 44 records. Records: [`round9_profiling_90min_memtrace.jsonl.gz`](round9_profiling_90min_memtrace.jsonl.gz);
  harness summary: [`round9_profiling_90min_summary.json`](round9_profiling_90min_summary.json).
- Alongside, `pg_stat_activity` sampled every 30 s for the `ira_app` connections to the soak
  database (count and `backend_start`), to see the pool from the server's side.
- The code read for each suspect is listed at the end.

tracemalloc with 5 frames costs memory of its own (its trace table is not in the traced figure), so
the profiling run's RSS is not comparable with the evidence runs; the harness's slope for it
(3.02 % an hour) is reported for completeness only. The confirmation run has no tracing.

## What the records show

Figures in MB. `traced` is tracemalloc's live Python-allocated bytes; `glibc in use` and
`glibc free, kept` are `mallinfo2` (all arenas); `gc objects` is the tracked-object count. `conns`
is the pool's connections as the server saw them at that minute.

| t (min) | RSS | traced | glibc in use | glibc free, kept | gc objects | conns | note |
|---|---|---|---|---|---|---|---|
| 2 (baseline) | 142.7 | 6.1 | 43.3 | 3.9 | 192,977 | 5 | |
| 6 | 157.1 | 8.6 | 45.7 | 5.2 | 244,594 | 6 | 4 connections opened at 16:54:00 (+1 MB of 256 KB TLS read buffers) |
| 10 | 161.9 | 8.6 | 46.6 | 6.9 | 245,711 | 6 | forced collection: 1,292 unreachable, RSS unchanged |
| 12 | 163.6 | 8.1 | 44.9 | 9.7 | 195,253 | 2 | 4 connections closed at 17:00 (idle 300 s): traced and in-use fall, free-kept rises |
| 16 | 163.9 | 8.4 | 45.3 | 9.7 | 195,941 | 2 | flat while nothing reconnects |
| 20 | 168.7 | 8.8 | 47.9 | 10.3 | 198,728 | 6 | 4 opened at 17:05:00: +4.7 MB; forced collection 4,095 unreachable, RSS unchanged |
| 29 | 168.7 | 9.0 | 45.9 | 12.4 | 196,392 | 2–3 | flat for nine minutes with no reconnect |
| 33 | 169.6 | 9.9 | 47.2 | 11.8 | 195,599 | 7 | 5 opened at 17:17–17:18: +0.9 MB (the kept-free heap served most of it) |
| 45 | 170.6 | 9.9 | 47.0 | 12.9 | 195,477 | 6 | 4 opened at 17:28: +1.0 MB |
| 60 | 171.4 | 8.1 | 47.0 | 13.7 | 196,848 | 6 | opened at 17:40 and 17:46 |
| 75 | 172.1 | 7.3 | 46.3 | 15.1 | 197,028 | 2–6 | |
| 89 | 172.7 | 10.1 | 47.4 | 14.7 | 197,072 | 6 | last record |

The 51,000 extra gc objects between minutes 4 and 10 are the tracer's own baseline snapshot (tuples
of traces), promoted to the oldest generation and gone after the first forced collection; they are
not the API's. Threads stayed at 2 throughout, so glibc had one arena and `MALLOC_ARENA_MAX` has
nothing to act on.

Reconnect bursts seen by the server (new `backend_start` values): 16:54, 17:05, 17:17, 17:18,
17:28, 17:40, 17:46, 17:51, 18:03, 18:14 — ten bursts, about 40 connections opened in 90 minutes
for a pool whose steady need was two to six. Between bursts the count fell back to two or three as
the 300-second idle timer closed the rest.

### Allocation sites that grew (last record against the baseline, tracemalloc by line)

| growth | objects | site | what it is |
|---|---|---|---|
| 1,536 KB | 12 | `asyncio/sslproto.py:278` | `bytearray(256 KB)` read buffer per TLS connection (six buffers, object and buffer counted apart); traceback through `asyncpg/connect_utils.py:886` (connect) |
| 649 KB | 12,072 | `asyncio/sslproto.py:800` | bytes handed from the TLS layer to asyncpg's protocol: row descriptions and codec data of prepared statements |
| 483 KB | 7,452 | `asyncpg/connection.py:470` | `PreparedStatementState` objects: the statement caches being refilled after reconnects and expiries |
| 49 KB | 314 | `asyncpg/connection.py:432` | statement names (`_get_unique_id`) |
| 48 KB | 406 | `asyncio/base_events.py:817` | `call_later` timer handles: one per cached statement (its expiry) |
| 16 KB | 141 | `care/escalation.py:290` | the monitoring pass's readings list (released each pass; small) |
| 14 KB | 295 | `care/repository.py:450` | `record_report` rows (released each request; small) |

Nothing in the server's own code grows. Every growing site is a connection or a prepared statement
being made again. The two `care/` sites are per-pass working data whose size at the end is the
same order as at the start.

### Live object types

No type in the API's own code grew. Between records the counts of `dict`, `function`, `list`,
`ReferenceType` and pydantic's `FieldInfo`, `SchemaValidator`, `ModelField` were constant to within
a few hundred (pydantic builds nothing per request). `TimerHandle` stood at about 600 with six
connections: one expiry timer per cached statement, about 100 per connection, which is the cache's
size.

### What a forced full collection found

Seven probes, 1,292 then 4,021 to 4,847 unreachable objects each. Types: `_StatementCacheEntry`,
`TimerHandle`, `Context` and `method` in equal numbers (135 then about 400 each), the
`PreparedStatementState` and `RecordDescriptor` they held, and the tuples and lists inside those.
Origin of every cycle but a handful: `asyncpg/connection.py:2575 _StatementCache._on_entry_expired`
(the expired entry and its cancelled timer handle refer to each other). RSS after each forced
collection equalled RSS before it (161.9, 168.7, 168.7, 169.6, 170.6, 171.4, 172.1, 170.8). So the
cyclic garbage of the fixed code is small, comes only from statement expiry, and freeing it returns
nothing: the "garbage waiting for a full collection" hypothesis from #123 does not hold for the
remaining growth. The collector's thresholds stay as they are.

### RSS minus traced

`RSS - traced` went 136.6 to 162.6 over the run. The glibc heap in use was flat (43 to 48 MB) and
its free-but-kept part grew 3.9 to 14.7 MB: memory the closed connections freed (OpenSSL state,
asyncpg buffers, the 256 KB read buffers) that glibc could not trim because newer live allocations
sat above it. The rest of the difference is tracemalloc's own table. `mallinfo2`'s mmap figure was
constant at 4.2 MB. `sys._debugmallocstats` returned nothing inside the API process (its stderr is
the harness's log file), so pymalloc arenas were not measured directly; the flat object count says
they did not grow.

## Mechanism

1. asyncpg's `PoolConnectionHolder` arms a `max_inactive_connection_lifetime` timer on every
   release and terminates the connection when it fires; it does not keep `min_size` connections
   afterwards (`asyncpg/pool.py`, `_deactivate_inactive_connection`, `acquire`).
2. The care jobs run every minute at :00 and the two watches report every 5 s, so about every ten
   minutes a few requests and both jobs needed connections at the same second, and the pool opened
   four or five at once; five minutes later all but one or two had been idle long enough to be
   closed.
3. Each new connection: a new `SSLContext` and TLS handshake (the local server has `ssl = on` and
   asyncpg's default is `sslmode=prefer`, so even loopback is TLS), a 256 KB `bytearray` read
   buffer, asyncpg's protocol buffers, then about 100 prepared statements as the working set is
   prepared again.
4. On close, that memory is freed into glibc's main arena. The arena's top is pinned by younger
   live allocations, so the freed chunks stay as free-but-kept bytes; the next burst is served partly
   from them and partly from fresh pages. Resident memory therefore steps up per burst by a
   shrinking amount (4.7, 0.9, 1.0 MB in this run) and never comes down.
5. Independently, each prepared statement expires 300 s after it was prepared
   (`max_cached_statement_lifetime`) and is prepared again on next use, leaving one small cycle per
   expiry for the collector.

## Fix (commit `4ed13b0`)

`supracloud-jarvis/ira/utils/db.py`: the pool is created with `max_inactive_connection_lifetime=0`
and `max_cached_statement_lifetime=0`, with the measurement above in the comment. A connection is
opened once and kept; the pool is bounded by `max_size=20`, and asyncpg still reconnects on acquire
when a connection has been lost (`is_closed()`), so a database restart is handled as before. The
server's SQL is a fixed set of statement texts (parameters are bound, identifiers are literal), so
the per-connection cache of 100 holds the working set with no reason to expire entries.

Tests, `tests/runtime/test_db_pool_keeps_connections.py`:

- `test_pool_is_created_with_no_idle_or_statement_expiry`: the keyword arguments `init_pool`
  passes to `asyncpg.create_pool`. Negative control run: with the two lifetimes set back to 300
  the test fails (`assert 300 == 0`), restored afterwards.
- `test_an_idle_connection_is_kept_and_its_statements_stay_prepared` (database): a real pool with
  the server's settings; after an idle of 1.5 s the next acquire is the same server process
  (`pg_backend_pid()`) and the statement prepared before the idle is still in the cache.
- `test_control_short_lifetime_closes_the_idle_connection` (database): the mechanism the setting
  removes: with a 0.3 s lifetime the same idle yields a different server process.

Not changed, and why:

- No collector threshold or `gc.freeze()`: forced collections freed 1 to 5 thousand objects and no
  resident memory.
- No `MALLOC_ARENA_MAX` or trim threshold: the process has two threads and one arena; the kept-free
  heap is a symptom of the churn, which is what the fix removes. If the confirmation run still
  shows glibc retention without churn, a measured allocator setting is the next step.
- asyncpg's `max_queries=50000` (a connection is replaced after that many queries) is left at its
  default: at this load each connection reaches it after several hours, one connection at a time,
  which is a bounded, rare reconnect rather than a periodic burst.

## Suspects checked and cleared

| suspect | how checked | result |
|---|---|---|
| per-request logging handlers or filters | grep for `addHandler`/`addFilter` outside tests; `core/runtime/logging_config.py` | one handler and one filter on the root logger at start; nothing per request |
| `lru_cache` on methods or unbounded keys | grep in `care api app auth utils worker core config.py` | all `maxsize` 1 to 8 (`care/activity.py`, `activity_estimate.py`, `heart_research.py`, `api/middleware/auth.py`, `utils/yaml_config.py`, `config.py`) |
| asyncio tasks kept in sets | grep `create_task`/`ensure_future` in the care path | none on the care request path; `care/talk.py` and `care/conversation.py` tasks are awaited and not exercised by this load |
| asyncpg prepared statements with many distinct SQL strings | f-string SQL grep; `PreparedStatementState` count and `TimerHandle` count in the records | six f-strings, all with fixed table or column names; the cache sits at its 100-entry bound per connection; growth is refills after reconnects and expiries, not new texts |
| pydantic model building per request | `FieldInfo`, `SchemaValidator`, `SchemaSerializer`, `TypeAdapter`, `ModelField` counts | constant across all 44 records |
| push, dispatch and escalation passes re-reading history | `care/escalation.py:281-404`, `care/delivery.py` after #123 | bounded reads (`LIMIT`, time windows, only attempts without a receipt); the pass's sites are small and released |
| SSE or stream buffers in `care/sos.py` | code read; load profile | not exercised by this load (no streams opened); idempotency replies are stored in the database, not in memory |
| httpx clients per request | grep `httpx.AsyncClient(` | created per call in `care/push.py` and `care/providers.py` only when a provider is configured; none is in the soak, so none were created; no httpx site in the records |
| exceptions or tracebacks retained by logs | forced-collection garbage types | no `traceback`, `frame` or exception types in the garbage after #123's middleware fix |
| gc thresholds leaving cyclic garbage | seven forced full collections with `DEBUG_SAVEALL` | 1 to 5 thousand objects, all from statement expiry; RSS unchanged after each |
| glibc arenas per thread | `threads` in every record | 2 threads throughout; one arena |
| pool connection churn | `pg_stat_activity` every 30 s; `sslproto.py:278` site; glibc free-kept | **the cause**: ten reconnect bursts in 90 minutes, RSS steps at each, free-kept heap 3.9 to 14.7 MB |

## Confirmation run

See [`WALLCLOCK_1H_ROUND9.md`](WALLCLOCK_1H_ROUND9.md): the complete 1-hour wall-clock soak on the
fixed code, with no tracing and the criteria unchanged.

## What remains unexplained

- The per-burst step shrank as the kept-free heap grew (4.7 MB, then about 1 MB), so the growth
  curve of the old code is sub-linear; where it would level off was not measured, and need not be
  now that the bursts are gone.
- pymalloc's arena count was not read inside the API process (`sys._debugmallocstats` wrote nothing
  through the redirected descriptor there, though it works in a plain process); the flat
  object count stands in for it.
- The profiling run's own criteria: two health checks of 1,046 were missed and p99 latency reached
  2.7 s during it. The machine was shared with other work at the time and tracemalloc with five
  frames slows every allocation; the confirmation run, without tracing, is the one that counts.
