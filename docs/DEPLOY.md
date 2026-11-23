# Deploying IRA CARE on one machine

A step-by-step guide for a single virtual machine in a UK region. Nothing in this
repository creates a cloud account or a resource; you do each step yourself, and every
step is reversible. Ready to host is not hosted: this has been built and checked locally
(the backup and restore cycle runs in CI), **not yet run on a rented machine**.

## 1. The machine

- Ubuntu 24.04 LTS, 2 vCPU, 4 GB memory, 40 GB disk, in a UK region of the provider you
  choose. Open ports 22 (SSH, your address only), 80 and 443.
- A DNS name (for example `care.example.org`) pointing at the machine's address. Caddy
  obtains the HTTPS certificate for that name by itself once port 80 is reachable.
- Install Docker Engine and the compose plugin from Docker's own repository (`docker
  --version` and `docker compose version` both answer).
- Data stays on the machine: PostgreSQL, Redis, the ledger journal and the backups are
  Docker volumes on its disk. Choosing a UK region keeps the data in the UK; nothing in the
  stack sends it elsewhere unless you configure an SMS, mail or payment provider.

## 2. Get the code

```bash
git clone <your copy of this repository> ira-care
cd ira-care/deploy
cp .env.example .env
```

## 3. Fill in `deploy/.env`

| Variable | What it is | How to set it |
| --- | --- | --- |
| `IRA_CARE_DOMAIN` | The DNS name of the machine. Caddy serves HTTPS for it and links in messages point at it. | `care.example.org` |
| `IRA_ENVIRONMENT` | `production`, `staging` or `development`. Test payments are refused in the first two. | `production` |
| `POSTGRES_DB` | The database name. | `ira_care` |
| `POSTGRES_SUPERUSER_PASSWORD` | The database's own superuser, used by the first-run role script and the backup job. | `python3 -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `IRA_APP_PASSWORD` | The runtime roles `ira_app` and `ira_worker`; everything they read is under row-level security. | as above |
| `IRA_MIGRATOR_PASSWORD` | `ira_migrator`, which applies the migration chain when the server starts. | as above |
| `IRA_SECRET_KEY` | Signs sessions and tokens. Changing it signs everyone out. | as above |
| `IRA_ADMIN_PASSWORD` | The private edition's owner account. Required by the settings even in the cloud edition; not used for care sign-in. | as above |
| `IRA_CONNECTOR_ENCRYPTION_KEY` | 32 random bytes, base64. Seals the access notes (and connector credentials). Losing it loses the notes. | `python3 -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"` |
| `IRA_CARE_MODEL` | `off` (templates), `fake` (demos), or `local` (IRA's model layer, which needs a model server you run). | `off` to start |
| `IRA_CARE_OPS_PHONE` | Who is told when the care jobs stop. Log-only until an SMS provider is configured. | your on-call number |
| `IRA_CARE_SMS_PROVIDER` | `log` or `twilio`. The adapter never dials emergency numbers. | `log` until you have keys |
| `IRA_MAIL_TRANSPORT` | `null`, `console` or `smtp`. Sign-up confirmations and invitations go this way. With `smtp`, also set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`. | `null` until you have a mail account |
| `IRA_CLOUD_REGISTRATION_ENABLED` | `true` opens `/care/signup`. | `false` until you are ready |
| `IRA_PAYMENT_PROVIDER` | Empty (off), `test` (development only) or `stripe` (needs `IRA_STRIPE_SECRET_KEY` and `IRA_STRIPE_WEBHOOK_SECRET`). The care plans carry no price until you set one. | empty |
| `BACKUP_PASSPHRASE` | Encrypts the nightly backups. **Keep a copy somewhere other than this machine**; without it a backup cannot be restored. | as above |
| `BACKUP_KEEP_DAYS` | How many nightly backups to keep on the machine. | `14` |
| `BACKUP_HOUR_UTC` | The hour the backup runs. | `02` |

The compose file also fixes `IRA_EDITION=cloud`, the four care jobs for the worker and the
database host names; those are not meant to be changed.

## 4. Start

```bash
docker compose up -d --build
docker compose ps
```

What happens: PostgreSQL starts and, on its first run only, creates the application roles
(`deploy/postgres-init/01-roles.sh`); the API container applies the migration chain as
`ira_migrator`, then serves; the worker starts the care jobs; the dashboard builds and
serves; Caddy obtains the certificate and serves `https://<domain>` (the dashboard) and
`https://<domain>/api/...` and `/auth/...` (the server); the backup container waits for its
hour. Every container has a health check and `restart: unless-stopped`.

Check:

```bash
curl -fsS https://<domain>/health
curl -fsS https://<domain>/health/jobs
```

The first says the process, the database and Redis are up. The second says the care jobs
have run in the last five minutes; the dashboard shows a red "monitoring paused" banner
when they have not, and the worker's health check fails.

## 5. The first organisation

With `IRA_CLOUD_REGISTRATION_ENABLED=true` and mail configured, a care organisation starts
itself at `https://<domain>/signup`. Without mail, the confirmation link appears in the API
log (`IRA_MAIL_TRANSPORT=console`); `docker compose logs api | grep verify` finds it. Then
sign in, name the organisation, add a person, invite the family, pair the watch
(`docs/DEVICE_SETUP.md`).

## 6. Backups and the restore test

Every night `deploy/backup.sh` writes `/backups/ira-care-<stamp>.sql.gz.enc` inside the
`backups` volume: a plain `pg_dump`, gzip, then AES-256-CBC with PBKDF2 under
`BACKUP_PASSPHRASE`, with a sha256 beside it. Copy that folder off the machine (the
volume is at `/var/lib/docker/volumes/ira-care_backups/_data`).

The client tools must be the server's own major version (the `backup` service uses the
`postgres:16` image for that reason); a newer `pg_dump` writes settings an older server
refuses, so the script refuses the mismatch before writing anything.

Run one now and prove it restores:

```bash
docker compose exec backup sh /usr/local/bin/backup.sh
docker compose exec postgres createdb -U postgres ira_care_restore_test
docker compose exec backup sh -c 'f=$(ls -t /backups/*.enc | head -1); sh /usr/local/bin/restore.sh "$f" "postgresql://postgres:$PGPASSWORD@postgres:5432/ira_care_restore_test"'
docker compose exec postgres psql -U postgres -d ira_care_restore_test -c "select count(*) from schema_migrations"
docker compose exec postgres dropdb -U postgres ira_care_restore_test
```

CI does the same on every pull request (`.github/workflows/test.yml`, "Backup and restore
round trip"): dump the test database with `backup.sh`, restore it into an empty database
with `restore.sh`, and compare the table count and the migration ledger.

To restore for real: stop the API and worker, create an empty database, restore into it,
point `POSTGRES_DB` at it, start again.

## 7. Updating

```bash
git pull
docker compose up -d --build
```

Migrations apply on start. Never run two API containers of different versions against the
same database at once.

## 8. Operations notes

- Logs: `docker compose logs -f api worker`. Secrets are redacted by the logging layer.
- The soak test (`scripts/soak_24h.py`) simulates a day for 200 people against a test
  database and records job timings; see `docs/progress/STATUS.md` for the measured numbers.
- Disk: PostgreSQL grows with readings (about 300 rows per person per day at a reading every
  five minutes); retention removes readings past each person's consented period.
- Nothing here dials emergency services, sends SMS or takes payment unless you configure a
  provider and its keys. All defaults are log-only.
