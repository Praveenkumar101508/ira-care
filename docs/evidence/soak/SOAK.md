# Soak: a simulated day for 200 people (register G2.e)

Run on 25 November 2026 by `scripts/soak_24h.py` against a fresh PostgreSQL 16 database
(`iratest_soak`, the full migration chain applied as `ira_migrator`, dropped afterwards).
The numbers are in [`soak.json`](soak.json).

**This is a simulated day, not 24 hours of wall-clock time.** The script advances a clock
in 289 steps of five simulated minutes and runs the real check-in and monitoring passes at
each step, as fast as the machine allows, on the shared 4-CPU build container. It shows the care jobs keep up with 200 people through a day and miss no
escalation. It does not show what a real day of wall-clock time shows: slow leaks, a
database that grows for 24 hours, provider and network weather. That run, on the machine
that will serve people, is still to do, so requirement G2.e stays **not started**.

| Measure | Result |
|---|---|
| People, each with a paired watch and a circle | 200 |
| Steps (5 simulated minutes each) | 289 |
| Check-ins answered / missed | 6 815 / 385 |
| People who stopped answering and moving | 20 |
| Escalated | 20 of 20 |
| Missing escalations | 0 |
| Late escalations | 0 |
| Lowest tier reached by the escalated | 3 |
| Check-in pass, mean / p95 / max | 0.176 s / 0.337 s / 0.754 s |
| Monitoring pass, mean / p95 / max | 0.264 s / 0.502 s / 0.919 s |

Both passes stay far below the five-minute step: the jobs are not the limit at this size.
The missed check-ins include every question to the 20 silent people; the other simulated
people answer most questions, not all, as the script describes.
