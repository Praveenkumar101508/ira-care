# Browser journey re-run, 25 November 2026 (requirements C-10.d, K.f)

`scripts/staging/clean_clone_proof.sh` on revision `a348b8c` (this branch: integrated `main`
at `eec19a0` plus workspace search and the care coordinator): a fresh clone, PostgreSQL and
Redis in containers, the migration chain applied as `ira_migrator`, the care server and worker,
the dashboard built and served standalone, the phone app exported for the web, the demo
organisation seeded with made-up people, then the Playwright suite in `e2e/` in headless
Chromium. Log-only message providers; no real person, message or emergency number.

Result: **passed**, 300 s end to end; **9 of 9 browser tests passed** (`e2e_results.json`,
`clean_clone_proof.json`, `outages.json` in this folder).

| Spec | Test | Result |
| --- | --- | --- |
| journey | the coordinator adds a person and makes a pairing code | passed |
| journey | the watch pairs with the code typed on its face and shows all well | passed |
| journey | the server asks a check-in and the person answers from the watch | passed |
| journey | a heart rate outside the range opens an alert the family answers on the phone | passed |
| journey | the coordinator plans a visit and the family member records its outcome | passed |
| live_refresh | the open record picks up a new visit, warns when cut off, and recovers on its own | passed |
| live_refresh | the care team search finds the person and leads to their record | passed |
| outages | Redis stops: the server says so, help from the watch is kept and delivered when it returns | passed |
| outages | the database stops: the server reports it and comes back on its own | passed |

**Found on the first run, and fixed:** the journey's fourth step failed. The spec added the family
member to the new person's circle with no account link (`user_id: null`); since #51 (D-076) a
family member sees only the people whose circle their own account is in, so the family's phone
correctly showed no alert. The suite had last passed on 24 November, before #51. The spec now links
the family account, taken from a seeded circle, as a care manager would. The product was right; the
test was stale.

**C-10.d (live refresh in a real browser):** with the record open and nobody touching it, a visit
planned through the API appeared within the record's own 30-second refresh; cutting the browser off
from the care server turned the status line to "Connection lost" while the record kept what it
had; restoring the connection brought "Live: updated" back with no reload.

Not covered here: a physical phone or watch, a real language model, and live SMS or call delivery.
