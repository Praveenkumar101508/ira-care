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
