# Deployment administration in a real browser (requirements D3.l, E2.p; 27 November 2026)

`e2e/deployment_admin.spec.ts`, run in headless Chromium (Playwright 1.63.0) against a running
care API and the built dashboard, on the build machine (not the owner's computer).

## The stack

| Part | How it ran |
|---|---|
| Care API | `scripts/demo/api.py` on `localhost:8000` (the address the dashboard's security policy allows), PostgreSQL 16 with pgvector and Redis 7, `IRA_STEP_UP_MAX_AGE_SECONDS=60` so a sign-in goes stale within the test, and a throwaway `IRA_DEVICE_POLICY_KEY` made by `python -m scripts.deployment_admin keygen` and never stored |
| Dashboard | `next build`, then `next start` on `localhost:3000` |
| People | the fictional demo seed (`scripts/demo/seed_demo.py`); two of its accounts granted deployment administrator with `python -m scripts.deployment_admin grant` (migrator credentials); the coordinator, a care manager, left without a grant |

## Result: 4 of 4 passed (1.4 minutes)

| Test | What it proves | Result |
|---|---|---|
| A carer who is not a deployment administrator sees the refusal and no controls | the grant is never a care role: no link, no field, no button | passed (1.9 s) |
| A deployment administrator sends a maintenance window to a paired watch, sees the row, and revokes it | issue, the signed row, revocation | passed (2.0 s) |
| A factory reset needs a second, different administrator, and the audit trail shows both | the requester's own approval is refused (the row stays waiting), a second administrator's is accepted, the audit trail lists "Requested", "Approval refused" and "Approved"; then revoked | passed (3.9 s) |
| Step-up after a sign-in that is no longer recent | after 70 s against a 60 s limit, sending asks the administrator to confirm who they are; a wrong password is refused and nothing is signed; the right one signs it; then revoked | passed (1.2 min) |

Run with: `E2E_DEPLOYMENT_ADMIN=1 E2E_DEPLOYMENT_ADMIN_KEY=<first> E2E_DEPLOYMENT_ADMIN_SECOND_KEY=<second> E2E_STEP_UP_WAIT_SECONDS=70` and the
stack variables `IRA_DEMO_INFO`, `IRA_CARE_API`, `DASHBOARD_URL`.

## Two defects the browser found, both fixed

1. **A step-up refusal reached the browser as a 500.** The care API (`scripts/demo/api.py`, the app
   the IRA CARE journeys run on) did not install the shared error handlers the full server installs
   (`core.runtime.errors.install_error_handlers`), so the server's correct "step-up required"
   (logged: `reason=authentication_too_old age=77 max=60`) escaped as 500 Internal Server Error and
   the dashboard said "No connection to the server" instead of asking for the password. The handlers
   are now installed; `tests/readiness/test_care_api_error_handlers.py` fails without the fix (shown).
2. **The screen said "You confirmed who you are 6 s ago" a minute later.** The line was computed once
   when the page loaded. It now counts on while the page is open and, once past the server's limit,
   says the next action will ask (`stepUpWords`, with a unit test in
   `dashboard/src/__tests__/device_admin.test.tsx`; the dashboard suite is 161 tests).

The first run also met the server's sign-in rate limit (5 a minute; HTTP 429) after several runs in
quick succession: the limit working as designed. The runs were spaced a minute apart.

## What this does not show

A watch collecting and applying the instruction: that is the watch's own journey
(`docs/evidence/emulator/WEAR_EMULATOR_JOURNEY.md`) and, for the real thing, a device. A run on
GitHub's runners: this stack ran on the build machine; nothing here needs anything paid.
