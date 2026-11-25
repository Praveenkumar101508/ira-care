# IRA CARE demo script (10 minutes, no hardware)

The four-to-six-minute film made from this stack is `docs/film/ira_care.mp4` (1920 × 1080),
with its words in `docs/film/NARRATION.md`; `scripts/film/record_scenes.mjs` and
`scripts/film/make_film.sh` remake it from a running staging stack
(`scripts/staging/clean_clone_proof.sh --keep`). The film follows the same journey as this
script, in this order: the title, the 20-second turntable of the watch, a check-in composed by
the model and a short talk with IRA, the ask-first question after a heart rate outside the
range, Help from the wrist reaching Sravani's phone with "I'm on my way" back on the wrist, the
coordinator's dashboard, the carer arriving on the watch, the morning report, privacy, and the
hardware. It zooms in on each of those moments; what IRA says in it is IRA's own words from the
recording.

Everything in this demo is made up: the people being looked after, the telephone numbers, the
readings and the addresses are invented (docs/BUILD_RULES.md, "Demonstration data"), and the
watch is a web page that speaks with the real server. Nothing here dials emergency services
and no real message leaves the machine (the providers are log-only).

## Before the demo (5 minutes, once)

This is the presenter guide (requirement J.c) for every audience: the internal engineering
demonstration, the investor demonstration and the Innovator Founder endorsement demonstration.
Which of those uses is allowed, and on what conditions, is decided in
`docs/progress/GO_NO_GO.md` ("Readiness decisions by use"). The technical case behind it is
`docs/evidence/TECHNICAL_ENDORSEMENT_REPORT.md`.

### What the machine needs

Checked against the repository on 25 November 2026; the stack itself was not started for this
check.

- **PostgreSQL 16** on port 5432 with a database `ira_test`, the roles from
  `supracloud-jarvis/postgres/provisioning/001_database_roles.sql`, and the passwords the
  scripts default to (`ira_app`, `ira_migrator`, and `postgres` for the admin role). Other
  values go in `IRA_TEST_DATABASE_DSN`, `IRA_TEST_ADMIN_DSN` and `IRA_MIGRATOR_DSN`.
- **Redis**: `up.sh` starts `redis-server` if `redis-cli ping` does not answer.
- **The backend's virtual environment** at `supracloud-jarvis/.venv`. `up.sh` falls back to the
  system `python3` for the seed and the API, but always runs the migrations with
  `supracloud-jarvis/.venv/bin/python`, so the virtual environment must exist.
- **Node 22** with `npm install` already run in `dashboard/` and `mobile/` (`apps.sh` builds the
  dashboard and exports the family app; it does not install packages).
- A browser. No watch, phone, model server or network access is needed.

### Start (two terminals)

```bash
# Terminal 1: migrations, the demo organisation (fresh passwords and keys), the demo API on port 8000
./scripts/demo/up.sh
# Terminal 2: the carer dashboard (port 3000, with /watch and /presenter) and the family app (port 8081)
./scripts/demo/apps.sh
# Optional, before the audience arrives: fill the record with the scripted day, night, fall and report
python3 scripts/demo/scenarios.py all
```

`apps.sh` builds the dashboard with `NEXT_PUBLIC_IRA_DEMO_PAGES=1`. Without it the browser
watch and the presenter page do not exist: production builds leave them out
(`dashboard/src/lib/demoPages.ts`).

### Check before the audience arrives

1. The terminal running `up.sh` printed the seeded accounts and device keys, and "Demo API on"
   the port.
2. The dashboard signs in as Praveen Kamineti and shows Ada Hartley and Bert Lowe under
   **People**.
3. The presenter page pairs (it shows "watch paired, online") and **Send 3 readings over 2
   minutes** makes the watch tab flash and ask "Are you OK?".
4. The family app signs in as Sravani Perni.

A red "Monitoring paused" banner on the dashboard is expected: the demo runs no separate care
worker, so the monitoring heartbeat is stale and the banner says so (`docs/demo/RERUN.md`).
Explain it if it is on screen; do not hide it.

### Reset between runs

1. Stop both terminals with Ctrl-C.
2. Run `./scripts/demo/up.sh` again. The migrations are already applied and are skipped; the
   seed removes the previous demo organisation and creates it afresh (`seed_demo.py --reset` is
   the same behaviour), with new passwords, new device keys and a new sealing key for the
   access notes. The API must be restarted after every seed, because it reads that key when it
   starts; `up.sh` does both.
3. Run `./scripts/demo/apps.sh` again, or leave it running: the web apps hold no demo data.
4. In the browser, sign out of the dashboard and the family app, and enter the new device key
   on the presenter page (it remembers the old key in the browser's local storage).
5. Optionally run `python3 scripts/demo/scenarios.py all` again.

### Say this first (investor and endorsement audiences)

- The people, addresses, telephone numbers and readings are invented.
- The watch on screen is a web page that talks to the real server; the IRA watch hardware is a
  design, not yet built, and nothing has run on a physical watch or phone.
- Messages are log-only: nothing is sent, and the system never dials the emergency services.
- IRA CARE is not a medical device and makes no diagnosis. The fall and heart models are
  measured on public research data only, not on older adults or on the IRA watch.
- There are no customers, pilots or revenue yet (`docs/evidence/ASSUMPTIONS_REGISTER.md`).

### The real language model

The demo API runs with the deterministic stand-in model (`IRA_CARE_MODEL=fake`), so the
wording is predictable on any laptop. To show Gemma 4's own words, use either the film
(`docs/film/ira_care.mp4`, recorded with `gemma4:e4b`) or the staging stack, which runs the
model chain through Ollama: `scripts/staging/clean_clone_proof.sh --keep --skip-e2e`, as in
`docs/film/RECORDING.md`. The safety rules are the same either way
(`docs/evidence/demo/REAL_MODEL_RUN.md`).

### Sign-ins

`scripts/demo/seed_demo.py` generates fresh passwords and device tokens on every run, prints
them to the terminal and writes them for the local scripts to `.demo/demo.local.json` (mode
0600, ignored by git, never committed). Open four browser tabs:

| Tab | Address | Sign in as |
| --- | --- | --- |
| Carer dashboard | http://localhost:3000 | Praveen Kamineti (care coordinator) |
| The watch in the browser | http://localhost:3000/watch | the pairing code or the device key, entered on the presenter page |
| Presenter controls (never on screen in a recording) | http://localhost:3000/presenter | the device key from the seed output |
| Family app (web) | http://localhost:8081 | Sravani Perni (family), or Ada Hartley for the wearer's own phone |

## Minute 0 to 1: who is being looked after

Dashboard, **People**. Two made-up people: Ada Hartley (lives alone, morning tablets,
lunch at 12:30) and Bert Lowe. Open Ada. The Watch line shows the simulated
watch: battery, on the wrist, last sync. The **Profile** tab has the made-up address, the home
coordinates and the emergency information. Say: *this is free text the carer controls;
there are no condition fields of our own.*

## Minute 1 to 3: a normal check-in, as a conversation

Watch tab, with the presenter tab beside it. In the family app, signed in as Praveen, open Ada
and press **Ask IRA to check in now** (or wait: the schedule asks every 15 to 20 minutes).
The watch shows and speaks the question. It was composed by the model from the person's
context (name, routines, what was said earlier) with the template as the fallback; the
dashboard timeline marks composed questions. Answer by voice or type *"I'm fine, I had my
tablet with breakfast"*. On the dashboard timeline the answer appears as a summary
("Said they had taken the morning tablets"), **never the person's words**: the words sit in
the transcript, readable only with recorded consent (the IRA tab shows "Transcripts are private" until consent
is recorded).

Press **Talk to IRA** and type *"what's on today?"* then *"call my daughter"*: the plan comes
from the record, and the contact is shown; the phone would dial only when the person taps.
Ask *"should I take another tablet?"*: *"I can't advise on that, shall I let Carer know?"*

## Minute 3 to 6: the night, an abnormal heart rate, no answer

Presenter tab, **Send 3 readings over 2 minutes** at 160 bpm. Within a moment the
watch vibrates (the page flashes) and asks *"Are you OK? Tap I'm OK, or say help."*. Say:
*one reading is noise; two in a row over two minutes is a question, not an alarm.* Do not
answer. After the wait (60 s by default) the server opens the SOS itself: the dashboard
**Alerts** page shows "Heart rate outside the set range", family and carers told (the attempts
list shows push, text and call, all log-only), then the care team five minutes later, then
everyone, with "call 999 now" shown in the family app. Nothing dials anything.

Family app tab: the alert card shows where the person is (at home, with the address, or
unavailable), the emergency information, and **Show access notes** (key safe, dog), logged
on every view. Tap **I'm on my way**: the chain stops; the dashboard shows who is going.

## Minute 6 to 8: a suspected fall, with location

Presenter tab, **Fall**. The watch counts down 30 seconds, vibrating and asking *"It looks like
you may have fallen. Are you OK?"*. Let it run out. The SOS carries the watch's location
(the presenter page sends a fake fix 2 km from home): "AWAY FROM HOME, about 2.0 km from 14 Orchard
Close" with a plain map link. Then press **Fall** again and tap **I'm OK** within the
countdown: the alert resolves with "The person answered the fall check: OK."

Dashboard, person, **Health and wellbeing**: fall detection can be turned off per person; the
thresholds are settings, not medical claims, and real-world accuracy is unmeasured.

## Minute 8 to 9: the watch comes off, the night rules

Presenter tab, **Remove watch**. In the day: a notice after the set minutes. At night: nothing
until an hour has passed, then a notice for the morning, never an SOS. **No movement** at
night raises nothing; the morning check closes the gap if nothing has moved by wake time.

## Minute 9 to 10: the next morning's carer report

Dashboard, person, **Reports**, **Yesterday's daily report** (or the report the morning-report
scenario already made): what happened, in summaries
(check-ins answered and missed, alerts and how they ended, readings against the carer's
range), with no diagnosis words anywhere. The weekly family report is shorter and softer.

## If something does not go to plan

- The presenter page says "offline": the API is not running or CORS refused the origin; check
  `./scripts/demo/up.sh` is still running.
- A question does not arrive: it is quiet hours for the person (22:00 to 07:00 by default);
  use **Ask IRA to check in now**, or change the quiet hours on the **Visits and medication** tab.
- The model is slow or absent: the templates are asked instead and the timeline shows no
  "composed" mark. The demo API runs with `IRA_CARE_MODEL=fake` unless told otherwise.
