# IRA CARE: operations runbooks

For the person running a single-machine deployment (`docs/DEPLOY.md`). Every command
below runs from the repository root on the server, against `deploy/docker-compose.yml`
(services: `postgres`, `redis`, `api`, `worker`, `dashboard`, `caddy`, `backup`, and
`ollama` when the model runs locally). Every script named here is in the repository, and
`supracloud-jarvis/ira/tests/readiness/test_runbooks.py` fails if one is renamed or
removed.

These runbooks are drafts: no one has yet run them on a live deployment. The first
real incident should update them.

## 0. The two health checks

```bash
curl -fsS https://<domain>/health
```

```bash
curl -fsS https://<domain>/health/jobs
```

`/health` says the API is up. `/health/jobs` says the care jobs (check-ins, the
escalation chain, the message outbox) ran recently; it answers non-2xx when they have
not. A care manager also sees it: the dashboard shows "monitoring paused" and the person
page stops saying "All well" (REMEDIATION F4).

## 1. Incident response

1. **Is anyone at risk right now?** Open the dashboard's alerts list. Any alert that is
   open and unacknowledged is the first job, whatever the incident is: phone the circle
   yourself if messages are not getting out. The system never dials the emergency
   services (`care/providers.py`); if nobody can be reached, call 999.
2. **What is broken?** The two health checks above, then `docker compose ps` (Caddy's
   own health there means its site is loaded, read from its admin API inside the
   container; it does not prove the certificate is valid: the `curl` above does) and
   `docker compose logs --since 30m api worker`. Message bodies and people's words are
   never in the logs (REMEDIATION A5); names and numbers are masked.
3. **Are messages getting out?** `GET /api/v1/care/ops/status` (signed in as a care
   manager) carries `delivery.queued_overdue` and `delivery.failed_recent`. Messages
   wait durably in the outbox (`care_alert_dispatch`) and are sent when the provider
   returns; nothing queued is lost by a restart.
4. **Contain, fix, recover** with the runbooks below.
5. **Record and report** under `docs/compliance/INCIDENT_PROCESS.md`: what happened,
   who was affected, what was done, and whether it is reportable (a personal-data breach
   to the ICO within 72 hours; a safety incident to the Clinical Safety Officer).

## 2. The worker stopped

Symptom: `/health/jobs` fails; the dashboard says monitoring is paused.

```bash
docker compose ps worker
```

```bash
docker compose logs --tail 200 worker
```

```bash
docker compose restart worker
```

Then watch `/health/jobs` turn healthy (one pass, about a minute). Nothing is lost while
the worker is down: check-ins due in the gap are asked late, the escalation chain picks
up every alert whose next step is due, and the outbox sends what is queued. If it will
not stay up, keep the dashboard open and watch alerts by hand until it does.

## 3. The database is down

Symptom: the API answers 503 quickly; `/health` fails; the worker logs connection errors.

```bash
docker compose ps postgres
```

```bash
docker compose logs --tail 200 postgres
```

```bash
docker compose restart postgres
```

The API and worker reconnect on their own (the staging drill in
`docs/evidence/staging/CONTAINERS.md`: clean 503s during a 20-second outage, back 1.1 s
after the restart). Nothing is sent while the database is down, and alerts escalate
once afterwards, never twice (REMEDIATION E3, E4). If the data itself is damaged, go to
the restore runbook.

## 4. Restore

Always into an **empty** database first, then check it, then switch.

From the machine's own backups:

```bash
docker compose exec postgres createdb -U postgres ira_care_restore
```

```bash
docker compose exec backup sh -c 'f=$(ls -t /backups/*.enc | head -1); sh /usr/local/bin/restore.sh "$f" "postgresql://postgres:$PGPASSWORD@postgres:5432/ira_care_restore"'
```

From the off-site copy, when the machine's own backups are gone
(`deploy/offsite.sh`, drill in `docs/evidence/staging/OFFSITE.md`):

```bash
docker compose exec backup sh -c 'n=$(sh /usr/local/bin/offsite.sh latest); sh /usr/local/bin/offsite.sh fetch "$n" /backups/fetched && sh /usr/local/bin/restore.sh "/backups/fetched/$n" "postgresql://postgres:$PGPASSWORD@postgres:5432/ira_care_restore"'
```

Check it:

```bash
docker compose exec postgres psql -U postgres -d ira_care_restore -c "select count(*) from schema_migrations"
```

`restore.sh` refuses a damaged file or a wrong passphrase before touching the database.
To switch, stop `api` and `worker`, rename the databases with `ALTER DATABASE ... RENAME
TO ...` as the `postgres` user, and start them again. Anything written after the backup
was taken is lost: say so in the incident record.

## 5. Key rotation

Do one key at a time, write down when, and keep the old value in the password manager
until everything that depended on it has expired.

| Key (`deploy/.env`) | What it protects | How to rotate | What it breaks |
|---|---|---|---|
| `IRA_SECRET_KEY` | Signs every sign-in token (JWT) | Set a new value, then `docker compose up -d api worker` | Everyone is signed out and signs in again. The server refuses to start with a value that has ever been published (`core/runtime/published_credentials.py`). |
| One account's sessions | That account's tokens | `POST /auth/logout/all` as that user | Only that account; refresh tokens issued before it stop working too. |
| A watch's device token | The watch's connection | A care manager revokes the device (`POST /api/v1/care/devices/{device_id}/revoke`), then pairs it again with a new code (`POST /api/v1/care/people/{person_id}/devices`) | That watch until it is paired again: it cannot send Help or readings in between, so do it with the person or a carer present. |
| `BACKUP_PASSPHRASE` | Every backup file | Set a new value, then `docker compose up -d backup` and run `docker compose exec backup sh /usr/local/bin/backup.sh` once | Nothing, but older backups still need the **old** passphrase: keep it until the oldest of them is past `BACKUP_KEEP_DAYS` (and the off-site lifecycle rule). |
| `IRA_CARE_TWILIO_AUTH_TOKEN` | Twilio's API and its callback signatures | Make a secondary token in the Twilio console, set it, `docker compose up -d api worker`, then promote it in Twilio | Callbacks signed with the old token during the switch are refused, so those messages stay "accepted" rather than moving to "delivered". |
| `IRA_CARE_CALLBACK_SECRET` | The token in every provider callback address | Set a new value, then `docker compose up -d api worker` | Callbacks for messages sent before the change are refused; their state stays "accepted". Rotate at a quiet hour. |
| `IRA_CARE_ARC_SECRET` | Requests to and acknowledgements from a monitoring centre | Agree the change time with the centre, then set it on both sides together | Acknowledgements signed with the old secret are refused until both sides match. |
| `OFFSITE_S3_ACCESS_KEY`, `OFFSITE_S3_SECRET_KEY` | Writing backups to the off-site bucket | Make a new key pair at the provider, set it, `docker compose up -d backup`, then delete the old pair | Nothing, if the new pair is set before the old one is deleted. |

Connector credentials have their own tool: `supracloud-jarvis/ira/scripts/rotate_connector_keys.py`.
