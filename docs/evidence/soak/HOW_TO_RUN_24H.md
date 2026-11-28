# How to run the 24-hour wall-clock soak (register G2.e)

The 24-hour soak has **not been run**. This page says how to run it with
`scripts/soak_wallclock.py` on a **remote Linux server**.

**Owner restriction (27 November 2026): never on the owner's personal Mac.** It has too little
storage, and the owner has ruled it out for all IRA CARE testing, hosting and evaluation.

**Where it can run, at no cost:**

- **A standard GitHub runner, up to 5.5 hours:** the `wall-clock soak (manual)` workflow
  (`.github/workflows/soak.yml`; Actions, then "Run workflow"). GitHub-hosted jobs stop at 6 hours
  (<https://docs.github.com/en/actions/reference/limits>, read 27 November 2026), so this is a
  longer run than the 1-hour one, never the 24-hour result.
- **The 24 hours themselves:** a Linux server that stays up for a day, from a sponsor or a
  university (Essex research computing has been asked about), or registered as a GitHub
  self-hosted runner, whose jobs may run for up to 5 days (same page). No provider has been
  signed up to and nothing paid for. A one-hour run of the same harness on the
build container is in [`WALLCLOCK_1H.md`](WALLCLOCK_1H.md); it is evidence that the
harness works, not the 24-hour result.

`scripts/soak_24h.py` is a different thing: it runs a *simulated* day in a few minutes
with an explicit clock ([`SOAK.md`](SOAK.md)). Do not present its numbers as a
wall-clock soak.

## What the run does

For `--hours 24` of real time it keeps the demo stack busy with fictional load through
the public API: two fictional people's watches report readings every 5 seconds, a family
member asks for a check-in every 5 minutes and the watch answers it, each watch presses
Help every 10 minutes (with a replay of the same idempotency key), and the family member
acknowledges after 90 seconds and then resolves the alert. It samples every 30 seconds:
health (every 5 seconds), process restarts, CPU and RSS of the API process, database size
and row counts, the outbox (`care_alert_dispatch`), dispatch retries, the worker heartbeat
file and request latency. Each sample is appended to `checkpoint.jsonl` and fsynced.

Messages are never sent: the harness refuses to start if any real provider variable
(Twilio, SMTP, monitoring centre, Expo push) is set, and removes them from the
environment of the API it starts. All people, numbers and addresses are invented.

## Pass criteria (decided before the run)

| Criterion | Threshold |
|---|---|
| Observed coverage of the wall clock | ≥ 99 % (otherwise the verdict is INCOMPLETE) |
| Availability (health checks answered 200) | ≥ 99.9 % |
| Server errors (HTTP 5xx) on load requests | 0 |
| Lost Help alerts | 0 |
| Duplicate Help alerts (extra rows, replay answered with another alert) | 0 |
| Restarts of the watched processes | 0 |
| RSS growth after a 10-minute warm-up (least-squares slope) | ≤ 5 % per hour |
| p95 latency of load requests over the run | ≤ 1000 ms |
| Worker heartbeat age at every sample | ≤ 180 s |
| Outbox rows unfinished for more than 300 s at the end | 0 |

The verdict is PASS only if the run completed and every criterion held. An interrupted
run is INCOMPLETE, never PASS.

## 1. Check the machine

At least 2 CPU cores, 4 GB of free memory and 5 GB of free disk are plenty; the run
itself uses well under 1 CPU and about 200 MB of memory.

On the Linux server:

```bash
nproc
free -h
df -h .
egrep -c '(vmx|svm)' /proc/cpuinfo   # > 0: the CPU offers virtualisation (needed for the Android emulator, not for the soak)
ls -l /dev/kvm                       # present: KVM is usable
```


## 2. Prerequisites

- PostgreSQL 16 (with the `vector` extension available) and Redis 7, both local.
- Python 3.12 with the backend's test requirements:

```bash
cd supracloud-jarvis/ira
python3 -m venv ../.venv && . ../.venv/bin/activate
pip install -r requirements-test.txt
pip install --no-deps --require-hashes -r requirements-nodeps.txt
```

- A fresh database for the soak only (the demo seed replaces its own tenant):

```bash
export PGPASSWORD=postgres
psql -U postgres -h localhost -c "CREATE DATABASE ira_soak"
psql -U postgres -h localhost -d ira_soak -f ../postgres/provisioning/001_database_roles.sql
psql -U postgres -h localhost -d ira_soak -c "ALTER ROLE ira_migrator PASSWORD 'ira_migrator'; ALTER ROLE ira_app PASSWORD 'ira_app'; ALTER ROLE ira_worker PASSWORD 'ira_worker';"
python3 -m scripts.migrate apply --dsn postgresql://ira_migrator:ira_migrator@localhost:5432/ira_soak
python3 -m scripts.check_migration_integrity
redis-cli ping
```

## 3. Keep the machine awake for 24 hours

- Linux: `systemd-inhibit --what=idle:sleep --why="IRA CARE soak" <the command below>`

Also switch off automatic updates and restarts for the day.

## 4. Run it

From the repository root, in `tmux` or `screen` so a closed terminal does not stop it:

```bash
export IRA_TEST_ADMIN_DSN=postgresql://postgres:postgres@localhost:5432/ira_soak
export IRA_TEST_DATABASE_DSN=postgresql://ira_app:ira_app@localhost:5432/ira_soak
export POSTGRES_DB=ira_soak
systemd-inhibit --what=idle:sleep --why="IRA CARE soak" \
  python3 scripts/soak_wallclock.py --start-demo --no-quiet-hours --hours 24 \
    --label "24-hour wall-clock soak" --out .demo/soak24 \
    --watch-match postgres=/usr/lib/postgresql/16/bin/postgres
```

`--no-quiet-hours` gives the two fictional people an empty quiet-hours window so check-ins
are asked and answered through the night as well; without it they are skipped from 22:00
to 07:00, as for a real person. The output directory is inside `.demo/`, which git
ignores; it holds the generated logins (mode 0600) as well as the evidence.

To watch progress: `tail -f .demo/soak24/checkpoint.jsonl | cut -c1-200`, or at any time
`python3 scripts/soak_wallclock.py --summarise .demo/soak24/checkpoint.jsonl`.

## 5. If it is interrupted

Run the same command with `--resume .demo/soak24/checkpoint.jsonl` (and the same
exports). The run keeps its original start and planned end, restarts the demo API if it
is down, and records a `resume` line with the length of the gap. Gaps count against
observed coverage, so a long interruption makes the verdict INCOMPLETE: start a new run
rather than present it as 24 hours.

## 6. What to commit

Copy `summary.json` and `checkpoint.jsonl` (no logins are in either) to
`docs/evidence/soak/` as `wallclock_24h_summary.json` and `wallclock_24h_checkpoint.jsonl`
(compress the checkpoint with `gzip` if it is over a few megabytes), write
`WALLCLOCK_24H.md` with the machine (from step 1), the wall-clock start and end, the
verdict and the table of criteria, and only then move G2.e on, citing those files.

## The compose stack instead of the demo API

`--base-url http://127.0.0.1:8000 --credentials <seed-format JSON> --admin-dsn <dsn>`
points the harness at a stack that is already running, with `--watch-match` for its
processes and `--heartbeat-file` if the worker's heartbeat file is reachable. The full
server in `deploy/docker-compose.yml` currently answers 403 to cloud accounts on `/api/*`
(the delegated-scope gate, recorded in STATUS.md), so the care load needs the demo API
until that is resolved.
