# IRA CARE: threat model (STRIDE)

Written 25 November 2026 against `main`. It completes the scope of the security review
(`REVIEW.md`, requirement C-22.c): what can go wrong, per component, under the six STRIDE
headings (Spoofing, Tampering, Repudiation, Information disclosure, Denial of service, Elevation
of privilege), and what controls it today.

Each row names the code that controls the threat and the test that proves it, or says
**not controlled**. `supracloud-jarvis/ira/tests/readiness/test_threat_model_traceability.py`
fails if a named file or test does not exist. It proves the links are real, not that the
controls are sufficient. This is an engineering threat model, not a penetration test (C-22.d,
blocked on a contracted tester).

**Assets:** the person's safety (an alert must get through), their health-adjacent data and
words, the care circle's accounts, and the integrity of what the watch and the dashboard show.

**Trust boundaries:** watch ↔ API (device token); phone and dashboard ↔ API (user session);
API ↔ PostgreSQL (row-level security as `ira_app`); worker ↔ providers (SMS, calls, push,
email, monitoring centre); providers → API (signed callbacks); operator → backups and the
off-site bucket.

## Watch

| # | STRIDE | Threat | Control | Code | Tests | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T1 | S | Someone pairs a watch to a person they should not reach | Single-use pairing code from a manager, hashed at rest, 15-minute life; guesses rate-limited per address | `care/devices.py`, `care/routes.py` | `test_care_public_rate_limits.py`, `PairingCodeTest.kt` | controlled |
| T2 | S | A stolen device token keeps working after unpairing | Token revoked at unpair on every route; the circle told once | `care/devices.py` | `UnpairingTest.kt`, `test_care_api_db.py` | controlled |
| T3 | I | The watch's stored queue and outbox read from the device | Lines sealed with a Keystore key; no plain-text fallback for sensitive data when sealing fails | `watch/.../SecureFiles.kt` | `SecureStorageTest.kt`, `DegradedStorageTest.kt` | controlled in software; hardware-backed keys unverified on a device (C-19.b) |
| T4 | T | A replayed or duplicated Help opens two alerts | Idempotency key per urgent event; the server replays its stored answer | `watch/.../Outbox.kt`, `care/sos.py` | `OutboxTest.kt`, `test_care_sos_db.py` | controlled |
| T5 | E | The watch sends a location outside an SOS (tracking) | The server accepts a location only with a Help or fall | `care/location.py` | `test_care_location_db.py::test_a_watch_cannot_send_a_location_outside_an_sos` | controlled |

## Phone (family app)

| # | STRIDE | Threat | Control | Code | Tests | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T6 | I | After sign-out the next person on the phone sees the previous account's data or pushes | Sign-out clears the session, caches and push registration | `mobile/src/api/client.ts` | `app_signout.test.tsx`, `push_signout.test.ts` | controlled |
| T7 | S | A long-lived session token is reused | Short-lived tokens, renewal, durable logout | `mobile/src/api/client.ts` | `session.test.ts`, `test_global_logout_durable.py` | controlled |
| T8 | I | Speech audio processed by the phone maker's cloud recogniser | Only text reaches IRA; the permission prompt says the phone listens; the DPIA records it | `mobile/src/voice/voice.ts` | `voice.test.ts` | partly: the platform's processing is outside our control (DPIA section 8) |

## Dashboard

| # | STRIDE | Threat | Control | Code | Tests | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T9 | T | The dashboard framed by another site (clickjacking) | `frame-ancestors 'none'`, `X-Frame-Options: DENY`, nosniff, no-referrer, HSTS | `dashboard/src/lib/securityHeaders.ts` | `security_headers.test.ts` | controlled |
| T10 | I | Script injection steals the session token in sessionStorage | React escaping only; no script-src policy yet | `dashboard/src/lib/session.ts` | — | **not controlled** beyond escaping (REVIEW.md residual) |
| T11 | E | Demonstration pages reachable in production | 404 unless the build opts in | `dashboard/src/middleware.ts` | `demo_pages.test.ts` | controlled |

## Care API

| # | STRIDE | Threat | Control | Code | Tests | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T12 | E | A member of one circle reads another person in the same organisation | Person-level guard on every person route; answers "not found" | `care/authz.py` | `test_care_person_guard.py`, `test_care_person_access_db.py` | controlled |
| T13 | E | A viewer or carer performs a manager's action | Workspace role checked before any query; staff roles listed with the routes that enforce them | `care/authz.py` | `test_care_staff_roles.py`, `test_care_plans_db.py` | controlled |
| T14 | D | Unauthenticated routes flooded (sign-up, pairing) | Per-address rate limits; a refused request never reaches the database | `care/routes.py` | `test_care_public_rate_limits.py` | controlled |
| T15 | R | A carer denies having read a record | Every read written to the access log, visible to managers, kept beyond erasure | `care/audit.py` | `test_care_audit_coverage.py` | controlled |
| T16 | I | Message text or health details written to logs | Log-only provider never logs the message; redaction of care exception logs | `care/providers.py` | `test_care_safety_answers.py::test_the_log_only_provider_never_logs_the_message` | controlled |
| T17 | T | A language model's reply turns doubt into "OK" or a dose into "taken" | A conservative reader decides; the model can only add doubt | `care/conversation.py`, `care/checkins.py` | `test_care_safety_answers_db.py` | controlled |
| T18 | S | Default or published secrets in a deployment | The server refuses to start with a published credential | `core/runtime/published_credentials.py` | `test_published_credentials.py` | controlled |

## Worker and providers

| # | STRIDE | Threat | Control | Code | Tests | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T19 | D | The worker stops and nobody is told | Heartbeats; `/health/jobs`; "monitoring paused" on every screen; "All well" withheld | `care/ops.py` | `test_care_continuous_db.py`, `view.test.ts` | controlled |
| T20 | T | A forged provider callback marks a message delivered | Callbacks refused unless the URL token and the provider's signature check out; states only move forward | `care/delivery.py` | `test_care_delivery_db.py::test_a_callback_that_is_not_ours_or_not_signed_changes_nothing` | controlled |
| T21 | D | A worker crash loses or duplicates an alert message | Durable outbox; one send per message across workers | `care/escalation.py` | `test_care_delivery_db.py::test_two_workers_at_once_send_each_message_exactly_once` | controlled |
| T22 | S | Messages sent to a number outside the circle, or to emergency services | Recipients only from the circle; emergency numbers refused | `care/providers.py` | `test_care_monitoring_rules.py` | controlled |

## Monitoring-centre webhook

| # | STRIDE | Threat | Control | Code | Tests | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T23 | S | A forged acknowledgement from a "centre" closes an alert | HMAC-signed, time-limited acknowledgements; forward-only states; off unless configured | `care/providers.py`, `care/routes.py` | `test_care_email_and_centre_providers.py`, `test_care_email_and_centre_db.py::test_tier_3_goes_to_the_centre_and_only_its_signed_acknowledgement_moves_it` | controlled; no real centre contracted (G3.f) |

## PostgreSQL

| # | STRIDE | Threat | Control | Code | Tests | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T24 | I | One organisation reads another's rows | Forced row-level security on every workspace table; the runtime role cannot bypass it | `docs/cloud/evidence/DATABASE_TENANCY_CLASSIFICATION.json` | `test_3c06_seventeen_tables.py`, `test_care_isolation_rls.py`, `test_classification_manifest.py` | controlled |
| T25 | T | A migration silently changed after it was applied | Applied migrations hashed and checked in CI | `scripts/check_migration_integrity.py` | `test_migration_runner_separation.py` | controlled |
| T26 | D | The database is down | API answers 503 cleanly; jobs resume; alerts escalate once afterwards | `care/escalation.py` | `test_care_outages_db.py` | controlled |

## OTA update server

| # | STRIDE | Threat | Control | Code | Tests | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T27 | T | A watch is offered an older, vulnerable image | Not on `main` yet: the OTA server (E4.d) is in an open pull request | — | — | **not controlled** on `main` |

## Backups

| # | STRIDE | Threat | Control | Code | Tests | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T28 | I | A backup file or the off-site copy is read | Encrypted (AES-256, PBKDF2) before it leaves the host; only encrypted files uploaded; keys on standard input | `deploy/backup.sh`, `deploy/offsite.sh` | `test_backup_restore.py`, `test_offsite_backup.py` | controlled |
| T29 | D | Backups silently stop | openssl in the image at build time; a failed backup ends the loop | `deploy/Dockerfile.backup` | `test_backup_image.py` | controlled |
| T30 | T | A restore from a tampered or wrong file | Checksum verified; a wrong passphrase refused before the database is touched | `deploy/restore.sh` | `test_offsite_backup.py` | controlled |

## Supply chain and transport

| # | STRIDE | Threat | Control | Code | Tests | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T31 | T | A dependency with a known vulnerability ships | `pip-audit` and `npm audit` in the review; overrides for PostCSS and xmldom; Expo CLI advisories remain | `dashboard/package.json`, `mobile/package.json` | `security_headers.test.ts` | partly: the phone app's build tooling has known advisories (REVIEW.md residual) |
| T32 | I | Secrets committed to the repository | gitleaks on every pull request | `scripts/ci/gitleaks.sh` | — | controlled in CI (`.github/workflows/test.yml`, job `secrets`) |
| T33 | I | Traffic read or altered in transit | HTTPS through Caddy; HSTS on the dashboard | `deploy/Caddyfile` | `security_headers.test.ts` | controlled where deployed behind Caddy; certificate on a real domain unverified (G1.e) |

## Not controlled, in one place

T10 (no script-src policy), T27 (OTA server not on `main`), and the partial items T3, T8, T31 and
T33. Each is named in `REVIEW.md` or the register with what it needs.
