# The deployment in containers, 2026-11-25

What was run: the two IRA CARE images built from `deploy/`, the whole stack started from
`deploy/docker-compose.yml` with `compose.staging.yml` and the new `compose.nomodel.yml`
(Postgres, Redis, the care server, the care worker, the dashboard, Caddy over HTTPS, the
backup service), fictional data only, messages to the log-only provider. Exact numbers:
`containers.json`. Machine: one shared 4-CPU, 15 GB Linux machine also running other builds.

Until now the staging proof (`clean_clone_proof.json`) ran the server, worker and dashboard
from the clone, with only Postgres and Redis in containers. This is the first time the
images themselves ran, and it found two defects that stopped them working at all.

## Defects found and fixed

| | What happened | Fix | Test |
|---|---|---|---|
| G-a | **The API image could not be built.** `fetch_encoder.py` worked out its default destination three directories above itself before reading `--dest`; the Dockerfile runs it from `/tmp`, so it failed with `IndexError`. | The default is computed only when no `--dest` is given. | `tests/readiness/test_image_build_scripts.py` (fails on main) |
| G-b | **A fresh database stayed empty.** The image copies the migrations to `/app/postgres-migrations`; the migrator looked beside the repository (`/postgres` in the image), found nothing, printed "nothing to apply" and the server started. Every care request then failed (`relation "job_runs" does not exist`). | `IRA_MIGRATIONS_DIR` names the directory, and the image sets it. With no files found, applying is refused and the status is never "up to date". | `tests/runtime/test_migrations_dir.py` (5; all fail on main). In the container: 114 migrations applied to an empty database. |
| G-c | The dashboard's runtime image carried the base image's own npm, npx and corepack, never used to serve the app: 10 of its 12 HIGH image-scan findings. | Removed in the runtime stage. | trivy: 12 HIGH before, 2 after (PostCSS, fixed by the override in PR #72). |

## How the images were built here

This sandbox's network policy refuses the operating systems' package mirrors
(`deb.debian.org`, `dl-cdn.alpinelinux.org`: HTTP 403 at the egress proxy) and re-signs TLS.
`scripts/staging/sandbox_build.sh` builds from the real Dockerfiles with exactly two
changes, printed in the script: the OS package step becomes a minimal `curl` stand-in for
the health checks, and the proxy's CA is mounted as a build secret for download steps only
(so it is in no layer). Everything else, including pip, npm, the encoder download, the user,
the entrypoint and the health checks, ran as written. **The `apt-get` and `apk add` steps of
the real Dockerfiles were therefore not exercised here.** A deployment builds with
`docker compose build` and needs none of this.

| Image | Size | Runs as | Build |
|---|---|---|---|
| `ira-care/api:local` | 1.55 GB | `ira` (uid 10001) | 58 s (pip layer cached from an earlier attempt) |
| `ira-care/dashboard:local` | 352 MB | `ira` (uid 10001) | 95 s |

## Results

| Check | Result |
|---|---|
| Stack up from empty volumes | 19 s; 114 migrations applied; every service healthy |
| `/health`, `/health/jobs`, dashboard `/login` through Caddy | 200, 200 after the worker's first pass, 200 |
| Care flow over HTTPS (`scripts/staging/containers_smoke.py`) | 13 of 13 steps: sign in, create a person, register and pair a watch, simulated readings, Help, the same Help retried with its key (replayed, not repeated), exactly one alert on the carer's list, acknowledged, resolved, operations status, dashboard |
| Database stopped 20 s | every answer a clean 503 in at most 20 ms (no hangs); care API back 1.1 s after the database started |
| API container restarted while five watches pressed Help | each watch got seven 502s from Caddy, kept its key, then 201; exactly one open alert per person |
| Worker stopped | `/health/jobs` 503 and the carers' operations status "monitoring paused" 296 s after the stop (5-minute window); healthy 62 s after it started again |
| Backup and restore (`deploy/backup.sh`, `deploy/restore.sh`) | 156 KB encrypted backup; restored into a new database in 4 s; 154 tables, 1,685 rows, every count identical; a wrong passphrase refused (exit 3) |
| Image scan (trivy 0.58.1, HIGH/CRITICAL) | API 0 critical, 54 high, all in Debian base packages, 1 with a fix available; dashboard 2 high after G-c |

## Load (`scripts/staging/containers_load.py`)

Paired virtual watches sending five simulated readings per request, back to back, each
pressing Help once, through Caddy over HTTPS, 30 s per level:

| Watches | Requests/s | Report p50 / p95 / p99 (ms) | Help p50 / max (ms) | Errors |
|---|---|---|---|---|
| 10 | 131.6 | 67 / 142 / 184 | 96 / 128 | 0 |
| 25 | 124.1 | 188 / 312 / 433 | 210 / 269 | 0 |
| 50 | 118.3 | 393 / 730 / 950 | 435 / 1,069 | 0 |

Throughput is flat and latency grows with concurrency: the API container ran at 83 to 95 %
of one core (it runs one uvicorn process) and the database at about 60 %. This is one shared
4-CPU machine with the load generator on it: a regression baseline, **not** a capacity figure
for production. More server processes would raise it, but the rate limiter keeps its counts in
the process, so that needs a shared store first.

## Not done, or not done the production way

- The OS package steps of both Dockerfiles (see above).
- **The backup service cannot encrypt without network at start**: its command runs
  `apk add openssl`, ignores a failure, and every backup would then fail. Here the real
  scripts were run from the host against the container's database instead. A backup image
  with openssl installed at build time is the fix; it is not made here because it could not be
  built and checked in this sandbox.
- The conversation model (Ollama) was left out; check-ins used their set questions.
- HTTPS used Caddy's local certificate for `localhost`, not a public certificate.
- No real SMS, call or e-mail provider; no real device.

Every container and volume was removed afterwards.
