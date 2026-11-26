# IRA CARE carer dashboard

Next.js 15 (TypeScript) for carers and care organisations. Signs in with the backend's
login, chooses the care organisation (a workspace), and shows many people at once: an
overview with each person's worst open alert, the alerts list with one-click acknowledge and
resolve, and a person page with a timeline of check-ins and alerts, reading charts, routines,
medication and doses, the care circle, the safety settings (editable by administrators),
consents, the conversation (only when the person's consent covers the viewer's role; a
refusal is itself logged) and the access log.

```bash
cd dashboard
npm ci
npm run typecheck && npm test && npm run build
npm run dev   # http://localhost:3000, backend at NEXT_PUBLIC_CARE_API_BASE_URL (default http://localhost:8000)
```

The token lives in the browser's session storage for the tab's life. Every care call
carries the bearer token and the `X-Workspace-ID` header, which the backend verifies.

## Deployment administration (`/admin/devices`)

A separate screen for **deployment administrators** (D-085, D-089): accounts the operator has
granted with `scripts/deployment_admin.py`. It is never a care role: a care manager, carer or
viewer without the grant sees only "This area is for deployment administrators." and a link
back, and the "Deployment" link in the navigation appears only for an account the server says
holds the grant (asked once per sign-in, remembered in memory, never in storage).

An administrator chooses one paired watch (tenant, workspace and watch ids, or a person from the
people list and one of their paired watches), sees every instruction sent to it with the
server's own status words and the audit trail per instruction (refreshed every 30 seconds, with
"last updated" and a stale warning), and may send one of three actions with a reason: open a
maintenance window (1 to 15 minutes), restore the restrictions, or request a factory reset,
which a *different* administrator must approve within 30 minutes. Approve appears only on a
request still waiting; revoke, with a reason, on live rows. When the server asks the
administrator to confirm who they are (a refusal naming `step_up_required`), an inline
"Confirm it is you" panel takes the password (and the authenticator code when one is enrolled),
calls `/auth/step-up`, keeps the new sign-in through the same path a renewal takes, and retries
the one refused action exactly once. The password is sent once and kept nowhere.
`docs/ops/DEPLOYMENT_ADMIN_SCREEN.md` has the detail; `src/__tests__/device_admin.test.tsx`
checks the behaviour against a stand-in server.
