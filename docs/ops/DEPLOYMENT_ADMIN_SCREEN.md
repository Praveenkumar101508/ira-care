# The deployment administration screen

`dashboard/src/app/admin/devices` ("Deployment administration"), with its components in
`dashboard/src/components/DeviceAdmin.tsx` and its pure helpers in
`dashboard/src/lib/deviceAdmin.ts`. It is the dashboard end of the signed watch instructions
decided in D-085 and D-089; the routes it calls are in `supracloud-jarvis/ira/care/routes.py`
(`/care/deployment-admin/...`).

## Who may use it

Only an account with a live row in `deployment_administrators`, written by the operator with
`scripts/deployment_admin.py`. No care role, workspace role, tenant role or owner flag confers
it (D-085). The screen asks the server once, `GET /care/deployment-admin/me`, and:

- `deployment_admin: false` (or a 403 or 404 for the route): the page shows only
  "This area is for deployment administrators." and a link back. No field, button, watch, action
  or history is rendered, and nothing else is asked of the server.
- `deployment_admin: true`: the screen below.
- any other failure: the failure is named ("Could not check whether you are a deployment
  administrator: ...") and nothing else is shown.

The "Deployment" link in the navigation appears only after the same answer says `true`. The
answer is remembered in memory for the sign-in (keyed by server and access token), never in
session or local storage, so a new tab asks again and a revoked grant disappears on the next
request, as D-085 promises.

## What the screen does

1. **Which watch.** Tenant id (filled from the sign-in's workspace list), workspace id (filled
   from the chosen care organisation) and watch id, each checked for the identifier shape the
   server accepts before anything is asked. As a helper, a person from the people list can be
   chosen; their watches are fetched through the ordinary care route
   (`GET /care/people/{id}/devices`) and the paired ones offered. A revoked or unpaired watch is
   not offered.
2. **Instructions sent to this watch.** `GET /care/deployment-admin/device-instructions`, every
   30 seconds and when the tab comes back into view, with the same gate as a person's record
   (one load in flight; an answer for a watch no longer chosen is dropped). The line above the
   table says "Live: updated N s ago", or "Connection lost: reconnecting ... may be out of date"
   after a failure or 90 seconds without one, and what was last seen stays on screen. Each row
   shows the action, where it stands in the server's own status words (signed, delivered,
   applied, restored, refused, revoked, awaiting approval, lapsed, withdrawn), who issued or
   requested it and when, who approved and who revoked it, the reason, and an expandable audit
   trail of the insert-only events (requested, approved, approval refused, issued, delivered,
   revoked, applied, refused, restored) with actor and time.
3. **Send an instruction.** One of the three actions (there is no other field), minutes 1 to 15,
   and a reason of at least ten characters. A maintenance window or a restore is signed at once
   (201) and the row appears. A factory reset is explained on the form as a two-person action:
   the request (202) is shown as "Waiting for a second administrator to approve it within 30
   minutes", and the row stands at "awaiting approval" until a *different* administrator
   approves it or it lapses.
4. **Approve** appears only on a row the server says is awaiting approval and calls
   `POST .../{id}/approve`. The server's refusal for approving one's own request is shown in its
   own words. **Revoke** appears on live rows (signed, delivered, applied, awaiting approval),
   asks for a reason, and calls `POST .../{id}/revoke`.

Every failure is named where it happened (the people list, a person's watches, the history, an
action, the confirmation) and the rest of the screen stays usable.

## Confirming who you are (step-up)

Issuing, approving and revoking may need a recent proof of who the administrator is. The screen
is built against this contract and does not depend on the server already enforcing it:

- `/me` also answers `auth_age_seconds`, `step_up_required`, `max_age_seconds` and
  `mfa_enrolled`; the screen states up front whether the next action will ask for confirmation.
- An action refused with 403 whose body names `step_up_required` (as `detail.code`, or in an
  `error` or `code` field, or anywhere in the body) opens the inline "Confirm it is you" panel.
  Any other 403 is shown in the server's words and opens no panel.
- The panel takes the password and, when `mfa_enrolled`, the authenticator code, and calls
  `POST /auth/step-up` with the current bearer. On 200 the returned access token replaces the
  stored one through the same path a renewal takes (`Api.onRenewed`, which rewrites the stored
  session), and the one refused action is sent again exactly once. A second refusal is reported
  and not retried. On 401 the panel says "Wrong password or code." and nothing is retried; on
  503 it says the sign-in service is unavailable. Cancelling sends nothing.
- The password field is cleared as soon as it is submitted; the password is never kept, logged
  or written to storage.

## What it does not do

- It does not grant or revoke deployment administrators, list who holds the grant, or show
  anything to an account without it. The grant is the operator's (`scripts/deployment_admin.py`).
- It does not offer any instruction outside the three actions, a window longer than 15 minutes,
  or a reset without a second administrator; the server and the table refuse those anyway.
- It does not approve a request on the requester's own behalf, and it cannot: the server
  refuses and the screen shows that refusal verbatim.
- It does not enrol or manage authenticators, change passwords or sign anyone in; it only asks
  for a confirmation when the server does.
- It does not talk to a watch. The watch collects signed instructions itself; the screen shows
  what the watch reported.

## Checks

- `dashboard/src/__tests__/device_admin.test.tsx` (vitest, a stand-in server behind `fetch`):
  the refusal and absence of controls for a non-administrator; the history with status words
  and audit trail; identifier validation; the person-and-watch helper; issuing a maintenance
  window; the factory-reset explanation and the 202 message; approve and the verbatim
  self-approval refusal; revoke with a reason; the step-up panel, password and code sent once,
  the new sign-in kept in the stored session, the action retried exactly once; wrong password
  not retried; a still-refused confirmation not retried; the 30-second refresh and stale state;
  the navigation link for administrators only, asked once and kept out of storage; axe for
  both states. `no_developer_words.test.ts` and `theme_contrast.test.ts` cover the new screen.
- `e2e/deployment_admin.spec.ts` (Playwright, staging stack): the refusal for a care account,
  and, with `E2E_DEPLOYMENT_ADMIN=1` and a granted account named by
  `E2E_DEPLOYMENT_ADMIN_EMAIL`/`E2E_DEPLOYMENT_ADMIN_PASSWORD` (or `E2E_DEPLOYMENT_ADMIN_KEY`),
  a maintenance window sent to a seeded person's paired watch and revoked, confirming who they
  are if asked (`E2E_DEPLOYMENT_ADMIN_TOTP` when an authenticator is enrolled). Type-checked
  only in this round; not executed (no browser stack here).
