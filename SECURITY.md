# Security Policy

## Threat Model

SupraCloud IRA is a **single-owner personal AI assistant** *by design* — there is no
partial-trust tier. Any authenticated session is the owner; there is no supported mode
for other authenticated users with a reduced trust level. The threat model is:

| Actor | Trust Level | What they can do |
|---|---|---|
| Owner (Praveen) | Full trust | All features, all data, all system commands |
| Unauthenticated requests | Zero trust | Blocked at nginx + FastAPI |
| LLM-generated content | Untrusted input | Sandboxed, no auto-deploy |
| Webhooks (Cal.com, external) | Signed only | Verified via HMAC-SHA256 |

## Authentication

- **Primary**: JWT Bearer tokens (HS256); short-lived access token (30 min) + refresh token (7 days), both issued at `POST /auth/token`; jti-based per-token revocation on logout; per-user revoke-all by bumping a Redis token version counter (invalidates all outstanding access tokens instantly)
- **Two-factor**: TOTP (time-based one-time password); enrol at `POST /auth/totp/enroll`, which requires the account password, and also a current code when a factor is already on; once a secret is enrolled and enabled, login at `/auth/token` refuses the token if a valid TOTP code is not supplied; a replacement factor takes effect only after `POST /auth/totp/verify` accepts a code from it, and the old factor works until then; TOTP and re-authentication failures count toward account lockout
- **Brute-force**: per-account progressive lockout — 5 consecutive failures trigger a 15-minute lock, escalating to a 24-hour cap; per-IP rate limit (5 req/min) enforced at the login endpoint
- **Voice**: ECAPA-TDNN biometric speaker verification (cosine similarity ≥ 0.75)
- **Service-to-service**: Long-lived JWT with `sub=ira-voice` for voice pipeline
- **Dev mode**: All auth bypassed — NEVER enable `DEV_MODE=true` in production

## Sensitive Data

IRA stores the following sensitive data:
- Conversation history (PostgreSQL `messages` table)
- Voice biometric profile (PostgreSQL `voice_profiles` table — 192-dim embedding)
- Memory embeddings (PostgreSQL `memory_embeddings` table)
- Task and reminder data

None of this data leaves your server unless you explicitly configure Replicate, Apify, or Telegram integrations.

## Owner-Gated Domains

The biometric gate blocks non-owner access to:
- Security logs, system credentials, .env contents
- Personal calendar, email, contact information
- Financial data
- Database schema and internal architecture
- Admin actions (user management, backup restore)

## Known Limitations

- Biometric voice gate requires enrolment — new installations have no voice owner profile
- Replicate, Apify, and Telegram integrations send data to third-party services when configured
- Computer use (Playwright) runs in a container with `--no-sandbox` — isolate from sensitive data

## Supported Versions

Only the current `main` branch (v1.0.x) receives fixes. Earlier tags are not
maintained.

## Secrets Policy

- All secrets live in `.env` (never committed — CI fails the build if a `.env` is tracked in git).
- **Every credential field in `supracloud-jarvis/.env.example` is empty.** Fill them only in
  your real `.env`; generate values with `openssl rand -hex 32`. An example file is published
  to everyone who can read the repository, so a value in it is a published value.
- Optional encrypted-at-rest handling via sops + age (`make secrets-encrypt` /
  `secrets-decrypt` in `supracloud-jarvis/Makefile`).
- **Secret scanning is enforced in CI.** The `secrets` job runs
  [Gitleaks](https://github.com/gitleaks/gitleaks) (`scripts/ci/gitleaks.sh`, pinned by
  version and checksum) over the **whole git history** on every pull request and push to
  `main`; a detected credential fails the build. The rules are `.gitleaks.toml`; the accepted
  historical findings, each a placeholder that protected nothing, are listed with their
  reasons in `.gitleaksignore`. History is never rewritten; a value that was ever committed
  is treated as published and replaced.
- Demo and simulation scripts generate every password and key on the machine that runs
  them (`scripts/demo/_local_state.py`); nothing in the repository can sign in to a demo.

## Reporting Security Issues

This is a personal project maintained by Praveen Kamineti. If you find a security issue,
please use **GitHub private vulnerability reporting** on this repository (Security →
"Report a vulnerability") rather than a public issue. If that's unavailable, contact the
maintainer directly via the GitHub profile. Please include reproduction steps; you can
expect an acknowledgement, but there is no bug-bounty program and no guaranteed SLA — this
is best-effort, honestly stated.

## Security Checklist (Before Production Deployment)

- [ ] `DEV_MODE=false` in `.env`
- [ ] Strong `IRA_SECRET_KEY` (run: `openssl rand -hex 32`)
- [ ] Strong `IRA_ADMIN_PASSWORD` (16+ chars, mixed case + symbols)
- [ ] `OWNER_NAME` set to your real name
- [ ] TLS certificate configured in nginx
- [ ] `IRA_DOMAIN` set to your actual domain
- [ ] Telegram or email notifications configured for alerts
- [ ] Voice profile enrolled at `POST /api/v1/voice/enroll`
- [ ] TOTP enrolled for admin login (`POST /auth/totp/enroll` with the account password, then verify with `POST /auth/totp/verify`)
- [ ] `.env` file not committed to git (verify: `git ls-files .env`)
