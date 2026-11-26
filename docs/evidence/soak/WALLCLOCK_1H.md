# Wall-clock soak: two one-hour runs (not the 24-hour requirement)

**These are one-hour runs.** Requirement G2.e asks for 24 hours of wall-clock time; that
run has not been done and G2.e stays **not started**. What these runs show is that the
harness (`scripts/soak_wallclock.py`) works against a real running stack, and what one
hour of it measured. How to do the 24-hour run: [`HOW_TO_RUN_24H.md`](HOW_TO_RUN_24H.md).

They are not the accelerated simulated day in [`SOAK.md`](SOAK.md): every second here
passed on the wall clock.

## Set-up

- Machine: the shared build container (4 CPUs, 15.7 GB, Linux 6.18, Python 3.13), commit
  `bdd3b7b` plus this branch's harness. Other work ran on the same machine at the same
  time, including a Wear OS emulator under software CPU emulation that kept one to two
  cores busy for all of run 1 and the first 21 minutes of run 2.
- Stack: `scripts/demo/api.py` (the real care, login and workspace routers with the care
  worker jobs in the same process) on 127.0.0.1:8765, PostgreSQL 16 (a fresh database with
  the full migration chain, 001 to 123), Redis 7. Started and seeded by the harness
  (`--start-demo`), message providers log-only (the harness refuses real ones).
- Load, all fictional, through the public API: 2 people with paired watches; readings
  every 5 s per watch; a check-in asked every 300 s per person and answered by the watch;
  Help pressed every 300 s per watch, the same idempotency key replayed once, acknowledged
  ("on my way") after 90 s and then resolved. Quiet hours were lifted for the two
  fictional people (`--no-quiet-hours`) so check-ins ran at night.
- Sampling every 30 s, health every 5 s.

Command (run 2):

```bash
python3 scripts/soak_wallclock.py --start-demo --no-quiet-hours --hours 1 \
  --label "1-hour wall-clock run (not the 24-hour requirement), second run" \
  --out <out> --help-every 300 --watch-match postgres_main=/usr/lib/postgresql/16/bin/postgres
```

## Results

| | Run 1 | Run 2 |
|---|---|---|
| Wall clock (UTC, 26 Nov 2026) | 01:47:27 to 02:47:29 | 02:50:01 to 03:50:03 |
| Observed coverage, gaps | 100 %, none | 100 %, none |
| Samples | 120 | 120 |
| Health checks answered 200 | 720 / 720 | 720 / 720 |
| Load requests | 2 328 | 2 332 |
| Server errors (5xx) | 0 | 0 |
| Client errors (other 4xx) | 6 (see below) | 0 (2 token refreshes) |
| Latency p50 / p95 / p99 / max | 11.0 / 20.0 / 35.8 / 449 ms | 9.7 / 21.6 / 207.9 / 680 ms |
| Help alerts asked for / in the database | 18 / 18 | 18 / 18 |
| Lost / duplicate alerts, replay mismatches | 0 / 0 / 0 | 0 / 0 / 0 |
| Alerts open at the last sample | 2 (see below) | 0 |
| Check-ins answered (asked by the harness) | 17 | 19 |
| Escalation attempts written / outbox rows | 204 / 186 | 108 / 108 |
| Outbox pending at any sample; stuck at end; retries | 0; 0; 0 | 0; 0; 0 |
| Restarts (API, PostgreSQL) | 0, 0 | 0, 0 |
| API process CPU mean | 0.55 % | 0.59 % |
| API process RSS first / last | 128.0 / 139.7 MB | 127.9 / 139.2 MB |
| RSS growth after a 10-minute warm-up | **6.65 % per hour** | **6.45 % per hour** |
| Worker heartbeat age, worst | 32.4 s | 33.1 s |
| Database size first / last (growth) | 21.2 / 21.9 MB (0.66 MB/h) | 22.3 / 22.7 MB (0.69 MB/h) |
| **Verdict** | **FAIL** (RSS growth) | **FAIL** (RSS growth) |

Every criterion held in run 2 except one: the API process's memory grew by about 6.4 %
per hour after the warm-up, above the 5 % per hour limit set before the run. One hour
cannot say whether that is a leak or a cache still filling (11 MB over the hour, and both
runs show the same slope); the 24-hour run will. The threshold was not changed after the
fact.

Run 1 found a defect in the harness, not in the stack: the family member's access token
expires after 30 minutes, and the first version did not sign in again before
acknowledging, so 2 acknowledgements and 2 resolves (and 2 check-in requests) were
refused with 401, and those 2 alerts stayed open and kept escalating, which is why run 1
wrote more escalation attempts. The harness now signs in again on a 401 (counted as
`auth_refresh`), and two criteria were added before run 2 started: no client errors,
and no alert left open at the end. Run 1's summary is the one its own version wrote.

Notes on the numbers:

- "Escalation attempts" are rows written by the real escalation chain while each alert
  waited 90 s for its acknowledgement; with log-only providers each ends as `logged` or
  `skipped`, and nothing was sent anywhere.
- The worker's own scheduled check-ins were cancelled when the harness's "ask now" ones
  replaced them (17 and 18); none was missed.
- Each person's readings pause for the 90 s of a Help cycle, so readings and check-in
  polls ran at about 1 110 each per hour rather than 1 440.

## Files

- [`wallclock_1h_run2_summary.json`](wallclock_1h_run2_summary.json),
  [`wallclock_1h_run2_checkpoint.jsonl.gz`](wallclock_1h_run2_checkpoint.jsonl.gz)
- [`wallclock_1h_run1_summary.json`](wallclock_1h_run1_summary.json),
  [`wallclock_1h_run1_checkpoint.jsonl.gz`](wallclock_1h_run1_checkpoint.jsonl.gz)

The checkpoints are the append-only JSONL files exactly as written, with the local output
directory replaced by `<out>`. `python3 scripts/soak_wallclock.py --summarise <file>`
recomputes a summary from one (after `gunzip`). They hold no logins or device tokens.
