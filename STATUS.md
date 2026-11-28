# IRA CARE: Status

**What is built, how it is proved, and what is not** (updated 28 November 2026, round 10). Nothing
here is rounded up. The measures come from `scripts/completion_summary.py` over the requirements
register (`docs/progress/requirements.json`); the round-by-round detail is in
[docs/progress/STATUS.md](docs/progress/STATUS.md), the last full report in
[docs/progress/ROUND10_REPORT.md](docs/progress/ROUND10_REPORT.md), and every decision in
[docs/DECISIONS.md](docs/DECISIONS.md).

> **Mandatory fix outstanding:** pull request 132 was merged with the iOS journey failing; see
> `docs/progress/NEXT.md`, "Mandatory: must be fixed".

## The measures

| Measure | Now |
| --- | --- |
| Hardware-independent software implementation | 179 / 195, 91.8 % |
| Hardware-independent software acceptance | 174 / 195, 89.2 % |
| Software verification (integration evidence: database, browser, emulator, model evaluation, device) | 110 / 210, 52.4 % |
| AI development (accepted) | 41 / 48, 85.4 % |
| OS development (accepted, hardware-independent) | 17 / 29, 58.6 % |
| Hardware integration | 0 / 14 |
| External and commercial readiness | 2 / 34 |
| Completed requirements (accepted or better) | 176 / 258 |

## What runs, and where it was proved

| Part | State | Proof |
| --- | --- | --- |
| Care backend (people, circle, check-ins, alerts and SOS tiers, medication, reports, consent, retention, audit) | Built, tested | about 5,800 unit and 4,500 database tests against a real PostgreSQL with forced row-level security |
| Wear OS watch app | Runs on the emulator | the 49-step care journey passed on GitHub's runner (run 45999809729): medication, check-in, fall countdown, Help, offline queue, reboot, permissions, unpair and pair again, accessibility |
| Family phone app, Android | Runs on the emulator | the authenticated journey, 18 of 18 (run 45990580023; again on run 46100618011 with the Talk-only-for-the-wearer checks) |
| Family phone app, in a browser | Runs (its web build) | the stale-wrist rule (after a fix), reminders, visits, appearance across devices and an expired sign-in renewed, 5 of 5 (`docs/evidence/browser/ROUND10_PHONE_WEB.md`) |
| Family phone app, iOS | Launches in the simulator; the authenticated journey not yet passed | 6 of 6 launch checks (run 45994124214). The authenticated journey reached 12 of 16 steps (run 46083831505; the rest followed a lost sign-in at restart). Signed to run locally, the next run stopped at sign-in on iOS autocorrect; the fix is committed and not yet run |
| Carer dashboard | Runs in a browser | the deployment-administration journey, 4 of 4 in Chromium against a running server (`docs/evidence/browser/DEPLOYMENT_ADMIN_BROWSER.md`); demo pages refused in production, the monitoring-paused warning, wrist status never guessed, unavailable measurements refused, an expired sign-in renewed, an axe accessibility check of six pages and their contrast in light, dark and high-contrast appearances, the activity trend only under consent, 8 of 8, after fixing unnamed form controls and colour-only links the check found (`docs/evidence/browser/ROUND10_VERIFICATION_BROWSER.md`) |
| Step-up authentication for deployment administrators | Built, verified | database and browser tests, including the two-person factory reset |
| Server memory over time | 1-hour soak passes (1.996 % an hour against 5 %) | `docs/evidence/soak/WALLCLOCK_1H_ROUND9.md`; a 5.5-hour run is available on a free runner (`.github/workflows/soak.yml`) |
| IRA conversation and safety rules | Built, measured | 162-turn set unchanged; round 10 hold-out 31 of 32 (`docs/ai/conversation/ROUND10_CONVERSATION.md`) |
| Fall detection on the watch | Shipped model v3 (trained on BITS-2 and UCI HAR) | the optional confirmation stage **failed** its gate on recorded falls and stays off (D-110) |
| Licence registers | Re-read at source | 45 sources agree, none differ (`docs/legal/LICENCE_SOURCE_CHECK.md`); non-commercial weights refused in the commercial edition (D-109) |
| Operations runbooks | Drilled | the worker-stopped and key-rotation sections run against the production server and worker, 10 of 10, after making the deployment's backups private (`docs/evidence/ops/DRILLS_ROUND10.md`) |
| IRA CARE OS (AOSP product layer) | Host-checked | 76 host tests; **the image has not been built or booted** |

## Not done, and why

What to ask for, from whom, for each item below: `docs/progress/RESOURCE_REQUESTS.md`.


- **The 24-hour soak (G2.e):** a free runner stops a job at 6 hours and the build container stops
  its daemons when idle; it needs a Linux server that stays up for a day. The owner's personal Mac
  is ruled out for all testing (storage).
- **The OS image (E2.h onwards):** needs a Linux host with about 64 GB of memory, 400 GB of disk
  and KVM.
- **Physical devices:** a watch and a phone for about ten items (speech, Keystore, screen readers,
  battery).
- **Datasets:** permission replies (KFall, SisFall, FallAllD, WEDA-FALL); a Mozilla Data Collective
  account for Common Voice (the older-speaker speech test).
- **Accounts and keys:** signing keys, Google Play and Apple developer accounts.
- **Reviewers:** clinical safety, data protection and legal sign-off of the written documents.
- **Money:** hosting, providers, a monitoring centre and hardware; nothing has been bought.
- **No text messages or calls are sent:** the providers log until keys are configured.
