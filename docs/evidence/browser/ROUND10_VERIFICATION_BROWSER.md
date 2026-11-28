# Round 10 verification in a real browser and on the database (28 November 2026)

Behaviour the register held only on unit tests, now exercised against the running system on the
build machine (not the owner's computer). Fictional people only (`scripts/demo/seed_demo.py`).
Results: `docs/evidence/browser/round10_verification_results.json`.

## The stack

| Part | How it ran |
|---|---|
| Care API | `scripts/demo/api.py` on `localhost:8000`, PostgreSQL 16 with pgvector and Redis 7, the database prepared as the CI jobs prepare it (`postgres/provisioning/001_database_roles.sql`, then `scripts.migrate apply`) |
| Dashboard | the production build: `next build`, then `next start` on `localhost:3000`, `NEXT_PUBLIC_IRA_DEMO_PAGES` unset |
| Browser | headless Chromium, Playwright 1.63.0 |
| Accessibility checker | axe-core 4 (the dashboard's own development dependency), the WCAG 2.0 and 2.1 A and AA rules, run inside each page |

## Browser: `e2e/verification.spec.ts`, 5 of 5 passed

| Requirement | Test | What it proves | Result |
|---|---|---|---|
| J.h | the demonstration pages answer 404 in a production build | `/watch`, `/presenter`, `/simulator` and a path below one answer 404 and never show the presenter controls; `/login` and `/` answer 200 | passed |
| G2.c | the dashboard says when monitoring has paused, and only then | against the server with its care jobs running, no "Monitoring paused" banner; against the same server restarted with no care jobs (`IRA_WORKER_JOBS=none`), once every job's last run was more than five minutes old, the banner shows on the home page and on the person's record, saying check-ins and alerts are not being processed | passed, both ways (the "paused" run at 01:15 UTC) |
| C-11.a | a silent watch is shown as last known, a watch that never said as unknown | the person's watch row set as a silent watch leaves it (worn, last sync three hours ago): the record says "last known on the wrist" and not "on the wrist"; never said: "wrist unknown"; negative control, worn and just synced: "on the wrist"; the row is put back afterwards | passed |
| D4.b | a measurement IRA cannot make is refused from the watch and shown as not available | the person's own watch (its device token) sends blood oxygen (97 %) and blood pressure (120 mmHg): each refused with 422, and no row added to the person's readings; negative control, a heart rate from the same watch is accepted; the health tab lists both as not available, with no number and unit, and does not list heart rate | passed |
| C-23.a | axe finds no serious or critical violation | the sign-in page, the people list, a person's record (care and health tabs), alerts and visits | passed after the fix below |

The first run of the monitoring test was started before the last job runs were five minutes old,
and failed (correctly: monitoring had not yet paused). It was run again once they were.

## A defect the browser found, fixed

**Unnamed form controls on a person's record (critical, WCAG 1.3.1 and 4.1.2).** axe found three
time fields (the routine's time, the quiet hours' start and end) and a drop-down (the routine's
kind) with no accessible name, so a screen reader has nothing to say what they set. The unit accessibility test (`dashboard/src/__tests__/accessibility.test.tsx`)
never rendered that tab, so it could not see them. Every unnamed control on the record now has a
name: the routine form, both "add to the circle" forms, the quiet hours, the usual wake time, the
safety settings' number fields and the communication profile's three drop-downs
(`dashboard/src/app/people/[id]/page.tsx`). The run after the fix found nothing on any page; the
dashboard's 161 unit tests still pass.

## Database: `supracloud-jarvis/ira/tests/tenancy/test_care_logs_no_personal_data_db.py`, 2 of 2 passed

| Requirement | Test | What it proves | Result |
|---|---|---|---|
| C-6.a | no log line of the whole care journey carries personal data | the complete 22-step care journey (`test_care_journey_db.py`: sign-up, circle, consent, check-ins, IRA's conversation, readings, a fall, SOS escalation, the watch's network failing and losing answers, offline readings sent later, resolution) on a real PostgreSQL as `ira_app`, with every logger recorded at DEBUG and every text message and call also handed to the log-only provider (the one a deployment without provider keys uses). 217 lines read, 7 from the care modules (5 of them the provider's); none holds the person's full or preferred name, her address, the family member's or carer's name, the family phone number, the private topic she consented to, or a reading ("71 bpm" to "80 bpm"). The messages themselves did name her, so a leak had something to leak | passed |
| C-6.a | the recorder catches a personal line | negative control: a name with a phone number, a reading and an exception carrying an address are each caught | passed |

## What this does not show

The phone app and the watch (their own journeys on emulators, and devices). A run on GitHub's
runners: these ran on the build machine; nothing here needs anything paid.
