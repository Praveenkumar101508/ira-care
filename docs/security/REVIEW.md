# Security review of the integrated stack, 2026-11-25

Scope: the care backend (`supracloud-jarvis/ira`: `care`, `core`, `api`, `auth`, `security`,
`worker`, `app`), the carer dashboard (`dashboard`), and the family app (`mobile`). This is
an engineering review, not a penetration test or an external audit: those still need a
qualified third party (register C-22).

The threat model that completes this review's scope, STRIDE per component with every control
tied to its code and test, is `THREAT_MODEL.md` (25 November 2026).

## What was run

| Check | Tool | Result |
|---|---|---|
| Static analysis, backend | bandit 1.x over the modules above, tests excluded | 0 high. 26 medium "SQL built from strings" (B608), all reviewed by hand below; 1 medium `urlopen` in billing (fixed URL, not user input); the rest low |
| Python dependencies | pip-audit over `requirements.lock` | no known vulnerabilities |
| Dashboard dependencies | `npm audit --omit=dev` | before: 1 high (PostCSS 8.4.31, bundled with Next.js), 1 moderate; **after: 0** |
| Family app dependencies | `npm audit --omit=dev` | before: 23 (1 critical, 5 high); after: 20 (1 critical, 3 high). See residual risk |
| Secrets in history | gitleaks (in CI on every PR) | passing |
| Unauthenticated routes | AST walk over `care/routes.py` | three at the review; four since G3.d added the signed monitoring-centre callback (listed below) |

## Findings and what was done

| ID | Severity | Finding | Done |
|---|---|---|---|
| S1 | Medium | `POST /care/devices/pair` and `POST /care/signup` take no login and had no rate limit. A pairing code has 40 bits and lives 15 minutes, so guessing is infeasible, but every guess was a free database lookup, and sign-up creates rows. | Rationed per client address (the same `rate_limit_key` the auth routes use): pairing 10 a minute, sign-up 3 a minute, the same budget as `/auth/register`. A refused request never reaches the database. `tests/care/test_care_public_rate_limits.py` (fails against main). |
| S2 | Medium | The dashboard sent no security headers, and its session token is in `sessionStorage`. Any site could frame it. | Every response now carries `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, a permissions policy (camera, location, payment, USB off; microphone for this origin only, for the browser watch's voice), HSTS, and no `X-Powered-By`. Checked on a built server with `curl -I`. `security_headers.test.ts` (fails against main). |
| S3 | High (build-time) | PostCSS below 8.5.10 (XSS in stringified CSS; file read via `sourceMappingURL`), bundled by Next.js 15.5 in the dashboard and by Expo in the family app. Only our own CSS goes through it, so not reachable by a user, but it ships in the build chain. | npm `overrides` pin PostCSS 8.5.33 in both. Dashboard builds; 73 tests pass. |
| S4 | High (build-time) | `@xmldom/xmldom` 0.7.13 (XML injection, recursion DoS) under Expo's plist handling. | Override to 0.8.15 (same API). Family app: 75 tests pass, `tsc` clean, `expo config` resolves. |
| S5 | — | Dynamic SQL (bandit B608) in the care backend. | Reviewed by hand: every column name comes from a fixed allow-list in the module (`_MEMBER_FIELDS`, `_ROUTINE_FIELDS`, `_MED_FIELDS`, `_SETTINGS_FIELDS`, the visit `allowed` set, retention's `PURGED`, the access-note `COLUMNS`) and every value is a bound parameter. No change needed. |

## Reviewed and sound

- The unauthenticated care routes are sign-up, pairing, and two provider callbacks: Twilio's status callback and, since G3.d, the monitoring centre's acknowledgement (`/care/providers/arc/status/...`), refused unless our token in its address and the centre's timestamped HMAC signature over the body both verify.
  The callback is refused unless both the per-deployment token in its URL and Twilio's
  signature over the full URL check out (REMEDIATION A2).
- Workspace isolation is PostgreSQL row-level security with FORCE on every care table, plus
  person-level checks in `care/authz.py` that answer "not found" (REMEDIATION E1, D-076).
- Pairing codes and device tokens come from `secrets` (not `random`); only SHA-256 digests
  are stored. The `random` uses bandit flagged are greeting variety and retry jitter.
- The server refuses to start with a published default secret
  (`core/runtime/published_credentials.py`).
- The care server exposes no OpenAPI or docs pages; CORS is an explicit list from
  `IRA_CARE_ALLOWED_ORIGINS`, empty by default.
- Demo, presenter and simulator pages are off in production builds (REMEDIATION B6, PR #67).

## Residual risk, not fixed here

- **Family app build tooling.** The remaining critical (`tar` 6.2.1, path traversal on
  extraction) and highs (`image-size`, `glob`/`minimatch`, `uuid`) are all inside Expo's CLI
  and the Metro bundler: the developer's machine and CI, not the app on a phone. The fixes
  are in major versions (`tar` 7, `image-size` 2) that Expo SDK 52 does not support; forcing
  them would break the build. The fix is an Expo SDK upgrade, which needs a device test pass.
- **No script-src policy on the dashboard.** Next.js injects inline scripts, so a strict
  policy needs per-request nonces in middleware, and `connect-src` needs the deployment's
  API origin. Until then the token in `sessionStorage` is exposed to any XSS; React's
  escaping is the main defence.
- **Not done:** a third-party penetration test; a threat model reviewed by someone outside
  the team; mobile certificate pinning; secret rotation drills.
- One run of the family app's test suite failed once while three other builds ran on the
  same machine, then passed four times in a row. Recorded, not explained.
