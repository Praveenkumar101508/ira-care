# IRA CARE: threat model (STRIDE)

Written 25 November 2026 against `main`; re-reviewed the same day after round 7 (REVIEW.md,
round 7: S8 to S15). Rows added in that re-review are marked **(round 7)**. It completes the scope of the security review
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
| T9 | T | The dashboard framed by another site (clickjacking) | `frame-ancestors 'none'` (in the per-request policy), `X-Frame-Options: DENY`, nosniff, no-referrer, HSTS | `dashboard/src/lib/securityHeaders.ts`, `dashboard/src/middleware.ts` | `security_headers.test.ts`, `csp.test.ts` | controlled |
| T10 | I | Script injection steals the session token in sessionStorage | React escaping, and a per-request nonce Content-Security-Policy: only scripts with the nonce run (`'strict-dynamic'`, no `'unsafe-inline'` or `'unsafe-eval'`), and `connect-src` is this origin and the configured API only, so a token cannot be sent elsewhere | `dashboard/src/middleware.ts`, `dashboard/src/lib/securityHeaders.ts` | `csp.test.ts`, `scripts/security/csp_browser_check.mjs` | controlled; inline style attributes still allowed (REVIEW.md S7) |
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
| T38 | D | **(round 7)** A WAV upload whose header lies (an awkward sample rate, thousands of empty chunks, a truncated RF64 size chunk) makes the decoder spend about 1 GB and seconds of CPU on a 46-byte file | The conversion filter's size is checked from the header and refused above 524,288 taps before any work; at most 1,024 chunks before the data; RF64 sizes bounds-checked; the declared data size never drives an allocation (only bytes present are read) | `voice/audio_decode.py` | `test_audio_decode.py::test_a_header_rate_that_needs_a_huge_filter_is_refused_before_any_work`, `test_audio_decode.py::test_a_file_of_endless_empty_chunks_is_refused_not_looped_over`, `test_audio_decode.py::test_control_an_rf64_file_with_a_huge_declared_size_reads_only_what_is_there` | controlled (S8, S9) |
| T39 | D | **(round 7)** An oversized request body, to any route including sign-up and pairing, is read and parsed whole before a schema or rate limit can refuse it | A 16 MiB ceiling on every care request body: a larger declared length is refused before reading, a chunked body is counted and refused as it passes the ceiling; research accelerometer rows are exactly three numbers | `care/body_limit.py`, `care/server.py`, `care/schemas.py` | `test_care_body_limit.py` | controlled (S10, S11); the reverse proxy sets no limit of its own |
| T40 | I | **(round 7)** Activity estimates stored or shown without the person's consent, or read by an alert rule | `activity_sharing` consent checked on arrival and on every read; forced RLS on `care_activity_periods`; no module on the alert path reads the table; retention and erasure remove it | `care/activity.py` | `test_care_activity_db.py::test_without_the_consent_nothing_is_stored_and_the_watch_is_told`, `test_care_activity_db.py::test_only_people_who_may_see_the_person_can_read_the_trend`, `test_activity_no_alert_path.py::test_no_module_on_the_alert_path_imports_the_activity_module` | controlled |
| T41 | E | **(round 7)** The research heart-rate route reads or writes a person's records, or its result reaches an alert | The route takes no person, opens no database connection and stores nothing; device data is refused; care managers only; rate-limited | `care/heart_research.py`, `care/routes.py` | `test_heart_research.py::test_the_research_pipeline_touches_no_database_and_no_person`, `test_care_research_route.py::test_carers_viewers_and_no_role_are_refused` | controlled |

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
| T27 | T | A watch is offered an older, vulnerable image | **(round 7: was "not on `main`")** The OTA server is on `main`: a release with a lower rollback index than the watch's own is never offered, and the answer is a signed manifest (no key, no answer); verified boot's rollback index refuses an older image on the device | `care/ota.py` | `test_ota.py::test_a_release_with_a_lower_rollback_index_is_never_offered`, `test_os_update_db.py` | controlled on the server; the device side (verified boot on a real watch) is unverified (E4) |

## Device administration (deployment administrators, D-085)

| # | STRIDE | Threat | Control | Code | Tests | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T34 | E | A care manager, carer, coordinator, family member or tenant owner lifts a watch's restrictions | Only an account with a live grant in `deployment_administrators` may issue, revoke or read device instructions; the grant is written only by the operator as the migrator, and RLS refuses runtime writes even after a blanket grant (R-47) | `care/deviceadmin.py`, `scripts/deployment_admin.py` | `test_device_admin_db.py::test_no_care_or_tenant_role_is_a_deployment_administrator`, `test_device_admin_db.py::test_the_runtime_cannot_make_anyone_an_administrator_even_with_every_grant` | controlled in software; the device-owner app is not provisioned on a device (E4.f) |
| T35 | T | A forged, altered, replayed, expired or foreign instruction opens a watch, or one carries a command | ECDSA P-256 signature over the exact payload checked first; device id, 15-minute bound, single-use nonce kept across reboots; closed list of three actions, anything else refused | `watch/.../devicepolicy/DevicePolicy.kt`, `care/devicepolicy.py` | `DevicePolicyTest.kt`, `test_devicepolicy.py`, `test_device_admin_db.py::test_only_the_closed_list_and_at_most_fifteen_minutes` | controlled in software (JVM tests); not run on a device |
| T36 | E | A maintenance window stays open, or survives revoking the administrator | The window closes at expiry and at every start; a revoked instruction, or anything issued by an administrator revoked afterwards, is withheld and put on the watch's revocation list, which closes an open window at once | `care/deviceadmin.py`, `watch/.../devicepolicy/DevicePolicy.kt` | `DevicePolicyTest.kt`, `test_device_admin_db.py::test_a_revoked_administrator_can_do_nothing_and_what_they_issued_is_withdrawn` | controlled in software; the timer and boot receiver that call it on a device are not built (E4.f) |
| T37 | R | An administrator denies having opened a watch | Every instruction is stored with who issued it and why, and every step (issued, delivered, revoked, applied, refused, restored) is an insert-only audit row | `care/deviceadmin.py` | `test_device_admin_db.py::test_an_instruction_is_signed_for_one_watch_collected_once_and_fully_audited` | controlled |
| T42 | S | **(round 7)** The device-policy key, which can wipe a watch, is mishandled because no custody procedure names it | Listed in the custody document with generation, storage and rotation; the guard finds every key variable the server declares and fails if one is undocumented | `docs/security/SIGNING_KEYS.md` | `test_signing_keys.py::test_every_key_variable_the_server_declares_is_in_the_custody_document` | partly: custody is procedural (S12) |
| T43 | S | **(round 7)** A phished or stolen deployment administrator session issues a factory reset | Two people (D-089, migration 123): a factory reset is stored unsigned as a request and signed only when a different deployment administrator with a live grant approves it within 30 minutes; self-approval, a lapsed or withdrawn request, and one whose requester has since been revoked are refused and audited; revoking either administrator withdraws a signed reset from the watch; the table refuses a one-person reset whatever the code does. Every instruction is also audited, revocable, single-use and at most 15 minutes | `care/deviceadmin.py`, `supracloud-jarvis/postgres/123_device_admin_factory_reset_approval.sql` | `test_device_admin_db.py::test_a_factory_reset_is_signed_only_when_a_second_administrator_approves`, `test_device_admin_db.py::test_revoking_either_administrator_before_signing_kills_the_request`, `test_device_admin_db.py::test_the_table_refuses_a_one_person_reset_whatever_the_code_does` | partly controlled: one stolen session can no longer reset a watch, two can; no step-up authentication, because the session token carries no authentication time (S15) |

## Backups

| # | STRIDE | Threat | Control | Code | Tests | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T28 | I | A backup file or the off-site copy is read | Encrypted (AES-256, PBKDF2) before it leaves the host; only encrypted files uploaded; keys on standard input | `deploy/backup.sh`, `deploy/offsite.sh` | `test_backup_restore.py`, `test_offsite_backup.py` | controlled |
| T29 | D | Backups silently stop | openssl in the image at build time; a failed backup ends the loop | `deploy/Dockerfile.backup` | `test_backup_image.py` | controlled |
| T30 | T | A restore from a tampered or wrong file | Checksum verified; a wrong passphrase refused before the database is touched | `deploy/restore.sh` | `test_offsite_backup.py` | controlled |

## Supply chain and transport

| # | STRIDE | Threat | Control | Code | Tests | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T31 | T | A dependency with a known vulnerability ships | `pip-audit` and `npm audit` in each review; overrides for PostCSS, xmldom and the Expo build tools (S6) | `dashboard/package.json`, `mobile/package.json` | `security_headers.test.ts` | partly (round 7): production trees clean, and the dashboard's development tree too since S13 was fixed; the API image is built from a partly unpinned list the audit does not cover (S14) |
| T32 | I | Secrets committed to the repository | gitleaks on every pull request | `scripts/ci/gitleaks.sh` | — | controlled in CI (`.github/workflows/test.yml`, job `secrets`) |
| T33 | I | Traffic read or altered in transit | HTTPS through Caddy; HSTS on the dashboard | `deploy/Caddyfile` | `security_headers.test.ts` | controlled where deployed behind Caddy; certificate on a real domain unverified (G1.e) |

## Not controlled, in one place

Nothing is wholly uncontrolled. Partial: T3, T8, T27 (device side), T31, T33, T42, and T43
(a factory reset now needs two deployment administrators, D-089; there is still no step-up
authentication). T10 (script
injection on the dashboard) has been controlled since the nonce policy (REVIEW.md S7). Each is named in `REVIEW.md` or the register with what it needs.
