# Off-site encrypted backups: the restore drill (register G2.h)

Run on 25 November 2026 at `eec19a0` (with this branch's scripts), by
`scripts/staging/offsite_drill.sh`. The numbers are in [`offsite.json`](offsite.json).

## What ran

1. A scratch database, `ira_offsite_src`, given the full migration chain (115 migrations,
   155 tables) as `ira_migrator`.
2. `deploy/backup.sh`, exactly as the backup container runs it, with `OFFSITE_S3_BUCKET` set:
   `pg_dump | gzip | openssl enc` (AES-256-CBC, PBKDF2, 200 000 iterations) to
   `ira-care-<stamp>.sql.gz.enc`, its checksum, then `deploy/offsite.sh push`, which uploads
   both with `curl --aws-sigv4` (AWS Signature V4).
3. The machine's own backup folder deleted.
4. `deploy/offsite.sh latest` and `fetch`: the newest object downloaded into a different
   folder and its checksum verified.
5. `deploy/restore.sh` into an empty database, `ira_offsite_dst`.
6. The same file restored with the wrong passphrase into another empty database.

| | Source | Restored from the off-site copy |
|---|---|---|
| Tables | 155 | 155 |
| Migrations applied | 115 | 115 |
| Rows across all tables | 238 | 238 |

The off-site object was 92 816 bytes of ciphertext. Backup to restored database took 2 s.
The wrong passphrase was refused (exit 3) and wrote 0 tables. The scratch databases were
dropped afterwards. The keys and passphrase were throwaway values made for this run only.

## What the bucket was

**Not a real S3 service, and not MinIO.** The MinIO image could not be pulled here: quay.io
is refused by this sandbox's network policy, and the Docker Hub mirror does not carry it.
The bucket was a local S3 stand-in, moto's server (`moto[server]` from PyPI, Apache-2.0), on
`127.0.0.1`. It implements the S3 API (bucket creation, PUT, GET, ListObjectsV2) but it does
**not check request signatures**, so this drill proves the scripts' requests, file handling,
encryption and restore, not that a real provider accepts our SigV4 signatures. curl's
`--aws-sigv4` is curl's own implementation (curl 7.75 and later), used here with an
`http://127.0.0.1` endpoint, which the script allows only with `OFFSITE_ALLOW_HTTP=1`; a
deployment must give an `https://` endpoint.

## Defect found and fixed (REMEDIATION G5)

`backup.sh` wrote the checksum file with the backup's absolute path in it, and
`restore.sh` checks with `sha256sum -c`, so a backup copied to any other folder or machine
(an off-site copy, exactly) was refused as a "checksum mismatch". The checksum now names
the file relative to its own folder. `tests/readiness/test_offsite_backup.py` fails on the
old line.

## Still to do on a real deployment

- An S3-compatible bucket at a provider (owner's choice and cost), with a lifecycle rule
  for how long old copies are kept, and access keys that can write but not delete.
- The drill re-run against that bucket, which also proves the provider accepts the signed
  requests.
