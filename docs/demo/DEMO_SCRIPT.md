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

```bash
# 1. A database with the migration chain applied, Redis, and the demo organisation
./scripts/demo/up.sh            # starts Redis, applies migrations, seeds the demo, starts the API (port 8000)
# 2. The carer dashboard and the family app (web)
./scripts/demo/apps.sh          # dashboard on http://localhost:3000, family app on http://localhost:8081
```

`scripts/demo/seed_demo.py` generates fresh passwords and device tokens on every run, prints
them to the terminal and writes them for the local scripts to `.demo/demo.local.json` (mode
0600, ignored by git, never committed). Open three browser tabs:

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

Watch tab, with the presenter tab beside it. On the dashboard's record press **Ask IRA to check in
now** (or wait: the schedule asks every 15 to 20 minutes).
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

Dashboard, person, **Reports**, **Generate daily report**: what happened, in summaries
(check-ins answered and missed, alerts and how they ended, readings against the carer's
range), with no diagnosis words anywhere. The weekly family report is shorter and softer.

## If something does not go to plan

- The presenter page says "offline": the API is not running or CORS refused the origin; check
  `./scripts/demo/up.sh` is still running.
- A question does not arrive: it is quiet hours for the person (22:00 to 07:00 by default);
  use **Ask IRA to check in now**, or change the quiet hours on the Visits and care plan tab.
- The model is slow or absent: the templates are asked instead and the timeline shows no
  "composed" mark. The demo API runs with `IRA_CARE_MODEL=fake` unless told otherwise.
