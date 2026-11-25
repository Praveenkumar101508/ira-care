# Demo re-run on the remediated stack (register J)

Run on 25 November 2026 on `main` at `ac71b8c` (PRs #50 to #62 merged), plus the three fixes
this re-run found, which are in the same pull request.

## How it was run

A fresh PostgreSQL 16 database, set up the way CI does:
- roles from `postgres/provisioning/001_database_roles.sql`;
- the migration chain, 001 to 114, applied as `ira_migrator`.

Then:
- `scripts/demo/seed_demo.py`;
- `scripts/demo/scenarios.py all`;
- the demo API (`scripts/demo/api.py`), the carer dashboard (`next build` and `next start`), and the family app's web export, served locally;
- `scripts/demo/record_demo.mjs`, which signs in, drives the browser watch from the presenter page, and records the screenshots in this folder and `demo.mp4`.

Everything is invented: the people, the organisation ("Lowbridge Care"), the telephone numbers (the Ofcom drama range) and the addresses. Message providers are log-only, so nothing dials and nothing leaves the machine.

The conversation model is the demo's **fake** model (`model="fake"`): no language model ran, so "IRA asked (model)" below marks the code path, not a real model's words.

## The scripted scenarios (log, verbatim)

```
Wed 07:31:42  IRA asked (model): Hello Ada. Have you had the morning tablets yet?
Wed 07:31:42  summary for the family: Said they had taken the morning tablets.
Wed 07:31:42  IRA asked (template): Ada, it's me. Did you have your breakfast yet?
Wed 07:31:42  summary for the family: Said they had eaten.
Wed 07:31:42  IRA asked (model): Hello Ada. Have you had the morning tablets yet?
Wed 07:31:42  summary for the family: Answered about their plans.
Wed 07:31:42  Talk: 'what's on today?' -> Today, Ada: breakfast at 08:00; lunch at 12:30; afternoon walk at 15:00; tea at 18:00; the morning tablets is due.
Wed 07:31:42  Talk: 'did I take my tablets?' -> Today's tablets: the morning tablets at 08:00: taken.
Wed 07:31:42  Talk: 'call my daughter' -> Here is Sravani Perni. Tap to call. [action: {'kind': 'call', 'name': 'Sravani Perni', 'phone': '+447700900001'}]
Wed 07:31:42  heart rate 158, 161, 164 over two minutes: heart_rate_outside_range -> asking first
Wed 07:31:42  watch vibrates: Ada, are you OK? Tap I'm OK, or say help.
Wed 07:32:57  no answer within the wait: 1 question became an SOS; tier 1 told (6 attempts, log-only)
Wed 07:37:59  tier 2: 4 attempts
Wed 07:42:59  tier 3: 10 attempts ('call 999 now' shown to the family; nothing dials)
Wed 07:32:57  text sent (log-only): IRA CARE alert for Ada Hartley. Heart rate readings 158, 161, 164 over 120 seconds are outside the range set for this person (45 to 130). Location unavailable: the watch did not send one. Home address: 14 Orchard Close, 
Wed 07:31:42  watch: hard impact 3.6 g then 20 s still -> asking first (alert asking), countdown on the wrist
Wed 07:31:42  countdown ended: SOS suspected_fall (open), at home: False
Wed 07:31:42  tier 1 told (6 attempts). IRA CARE alert for Bert Lowe. The watch detected a hard impact (3.6 g) followed by 20 seconds without movement. The person is being asked if they are OK. The person appears to be AWAY FROM HOME, about 2.0 km from Flat 3, Mill House, Lowbridge, LB1 4QT. Locatio
Thu 07:00:00  carer report: Daily report for Ada Hartley: Wednesday 25 November 2026
Thu 07:00:00    Daily report for Ada Hartley: Wednesday 25 November 2026
Thu 07:00:00    Check-ins: 3 answered, 1 missed, of 4 asked; 0 skipped at night.
Thu 07:00:00    Meals: asked about 1 time(s); said they had eaten 1 time(s).
Thu 07:00:00    Medication: 1 dose(s) confirmed taken, 0 said not taken, 0 not confirmed, of 1 recorded.
Thu 07:00:00    Heart rate: 6 readings, 74 to 164 (average 118); range set for this person: 45 to 130; 3 reading(s) outside it.
Thu 07:00:00    Movement: 120 steps or movement counts recorded.
Thu 07:00:00    Watch now: battery 60 %, on the wrist, sensor permission on.
Thu 07:00:00    Alerts (1):
Thu 07:00:00    - 07:31 heart rate outside the set range (sos, reached tier 3, now escalating)
```

What this shows about the remediation:
- **Night escalation:** IRA asks first, then escalates through tiers 1 to 3. The messages are queued in the durable outbox (PR #46), with "call 999 now" for the family at tier 3. Nothing is presented as delivered.
- **Fall with location:** the SOS carries the distance from home.
- **Medication:** a dose is recorded as taken only when the answer says so (PR #44).
- **Morning report:** counts check-ins, meals, doses, heart-rate readings outside the carer's range, and the alert that reached tier 3.

## The browser demo (screenshots `01` to `12`, `demo.mp4`)

All twelve steps ran. Afterwards, the demo database held:
- one SOS for Ada's fall, raised from the browser watch;
- Bert's fall, acknowledged by Sravani from the family app ("I'm on my way");
- the heart-rate alert from the scripted night, still escalating.

The dashboard shows a red "Monitoring paused" banner (`02`). That is correct: this setup runs no care worker, so the monitoring heartbeat is stale, and the banner says so.

## What the re-run found, and fixed here

| Found | Cause | Fix |
|---|---|---|
| Pressing **Help during a fall countdown** on the browser watch kept the countdown running ("Help in 24 s" after Help was sent). At zero it would have sent a **second SOS**. | The browser watch's `help()` did not stop the countdown. The watch app's own `FallCountdown.helpPressed()` already does. | `help()` ends a running countdown. `browser_watch_fall.test.ts` (2 tests); the first fails on the old code with the countdown still 30 after Help. |
| A person's record opened with an **old tab name** (`?tab=home`, from bookmarks or the old recorder) showed a **blank page**. | The page trusted the `tab` parameter. | Unknown tab names show the overview. |
| The recorder captured stale tabs, and acknowledged whichever alert came first rather than the fall. | The script had drifted from the app. | `record_demo.mjs` updated; the screenshots in this folder are new. The old `0x-simulator-*` files are removed, and `READINESS.md` points at `06-watch-talk.png`. |

## Not re-run here

- The narrated film (`docs/film/`) and its voice-over.
- Anything on a physical watch or phone.
