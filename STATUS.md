# IRA CARE: Status

**What is built, how it is proved, and what is not.** Nothing below is rounded up. "Tested"
means an automated test in CI exercises it; "not run" means nobody has yet run it outside
tests. The per-phase detail is in [docs/progress/STATUS.md](docs/progress/STATUS.md).

## Care features

| Feature | State | How it is proved |
| --- | --- | --- |
| Care data model: people, circle, devices, readings, settings, check-ins, alerts, routines, medication, transcripts, consents, access log, reports | Built | 16 tables with forced RLS; adversarial isolation suites (other family, other workspace, unbound connection) against a real PostgreSQL |
| Check-in cycle: 15 to 20 minutes, quiet hours, warm wording, answers summarised | Built | Unit tests on the rules; database tests driving a full day with an explicit clock |
| "Is this normal?" rules and tiered SOS with acknowledgements and retries | Built | Unit tests on the rules; the end-to-end simulation to tier 3 and back; a notice told once; one open alert per kind |
| Text and call providers | Interface built; log-only by default; Twilio adapter off without keys | Tests prove nothing is sent and emergency numbers are refused |
| Wear OS watch app | Built, unit tested | Gradle build and 22 JVM tests in CI. **Not run on a watch or emulator** |
| Family and patient phone app | Built, unit tested | Type check and 14 tests in CI. **Not run on a phone** |
| Carer dashboard | Built, unit tested | Type check, 12 tests and a production build in CI. **Not run against a live backend by a person** |
| Daily carer report and weekly family report | Built | Unit tests on wording; database tests on idempotent generation and the hourly job |
| Searchable history, audited | Built | Database tests with and without consent |
| Consent: family see summaries, words only with recorded consent | Built | Database tests on the transcript, check-in and search routes |
| Retention: purge past each person's period | Built | Database tests; open alerts and the access log never purged |
| Audit: every view of health data logged | Built | Database tests and a static check over every route |
| Load: 1,000 people in one check-in pass | Measured | About one to two seconds per pass locally, in a test |

## Round 2 features

| Feature | State | How it is proved |
| --- | --- | --- |
| Readings checked as they arrive; ask first, then escalate; night rules; the morning check | Built | Unit and database tests; the simulator scenarios |
| The system watches itself: heartbeats, `/health/jobs`, the "monitoring paused" banner | Built | A database test that stops the jobs; a dashboard test |
| Home record, sealed access notes, location once at an SOS, whereabouts in every message | Built | Unit and database tests; the recording |
| Fall detection on the watch with a countdown; the server's deadline; per-person off switch | Built, unit tested | Synthetic signals only; **real-world accuracy unmeasured** |
| IRA talks: composed check-ins, structured answers, red flags, Talk to IRA, memory | Built, measured | Unit and database tests with the fake model; the 162-turn evaluation on the real open models and the staging stack (round 4: first words at once, the model's first sentence 3.5 to 5.5 s on 4 CPU cores; `docs/MODEL_CHOICE.md`) |
| The hardware-free demo: seed, simulator, scenarios, recording | Built, recorded | `docs/demo/`; the clean-clone run |
| Sign-up, invitations by link with consent, plans and limits | Built | Database tests on every new route; sign-up off by default |
| Deployment: containers, HTTPS, health checks, encrypted backups with a proved restore, soak | Built, checked locally | The CI restore round trip; the soak (20 of 20 escalated); **not run on a rented machine** |
| Store readiness, accessibility, legal drafts, compliance drafts | Built or written | Automated accessibility checks; every document marked as a draft for review |

The full table with proof or blocker per feature is [docs/READINESS.md](docs/READINESS.md).

## Round 4 (the polish, 24 November 2026)

| Change | State | How it is proved |
| --- | --- | --- |
| A conversation that feels smooth on four CPU cores: the model kept loaded, a shorter prompt, an instant acknowledgement, prepared check-in questions, and a turn that asks only a loaded model | Built, measured | The 162-turn evaluation (`docs/evidence/eval/round4/`) and the staging stack; D-069 to D-071, D-073 |
| The watch's one look and the exploded view in stack order, across the Blender model, eight renders, the turntable, the viewer and the browser watch | Built | Every render reviewed at full size; the viewer exploded in a headless browser; D-072 |
| The film at 1080p with IRA's own words | Built | `docs/film/ira_care.mp4` (5 min 6 s, 15.2 MB), remade from the staging stack |

## Foundation (from IRA)

Unchanged from the split: conversation, memory, scheduler, notifier (push and e-mail; push
stays off until its key is configured), logins and roles, approval gating, privacy centre,
multi-tenant isolation. Spoken replies are not active on the server; the watch uses its own
text-to-speech.

## Not done, and why

- **No real device.** Needs a Galaxy Watch with LTE. The Health Services sensor source,
  text-to-speech and speech recognition are implemented against the Android APIs but unexercised.
- **No vendor sensor SDK.** Needs partner approval; the sensor source is an interface for it.
- **No text messages or calls are sent.** Needs provider keys; without them the provider logs.
- **No monitoring centre.** Needs a contract; tier 3 shows the family "call 999 now".
- **No GPU.** The open models run on the server's CPU (`gemma4:e4b`, then `gemma4:e2b`, then
  the set lines; no hosted model API). A whole talk turn's first sentence takes 3.5 to 5.5 s on
  four cores; a small GPU server would bring it well under a second (`docs/MODEL_CHOICE.md`).
- **One inherited test** fails on a full local clone and skips in CI (DECISIONS D-004).

## Verification

Round 1 (21 November 2026): backend unit 4,783 passed, 531 skipped, 1 inherited failure that
skips in CI; database suites 3,448 passed; watch 22; family app 14; dashboard 12 and a build.
Round 2, from a clean clone of the integrated branch: see `docs/progress/FINAL_REPORT.md`
("Round 2: test counts"). CI matched the suites for pull requests 10 to 13. From 14 on, GitHub refused to start
any run for two days (declined card payments on the account, not the workflow or the
minutes); once the payment cleared on 23 November, 14 to 18 ran green and merged in order,
after one real fix (D-050).
