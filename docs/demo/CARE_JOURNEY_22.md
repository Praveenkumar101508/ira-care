# The care journey in 22 steps

The owner's directive (section 16) asks for the whole care journey, in order, on the real
platform. It runs as one automated test:

```
supracloud-jarvis/ira/tests/tenancy/test_care_journey_db.py::test_the_complete_care_journey_in_twenty_two_steps
```

How it runs:

- Against the real care API over HTTP (ASGI, in process) and a real PostgreSQL connected as
  `ira_app`, so row-level security applies.
- The background jobs (check-in cycle, escalation, reports) are called as the worker calls
  them, with the clock moved forward where a step needs time to pass.
- The watch is driven over its own routes with its own device token.

Every person is fictional: Edith Example, Daughter Dee and Carer Cal. Nothing in the test
reaches the outside world:

- **Watch readings** are simulated and labelled so in the test.
- **Text messages and calls** go to a recording provider.
- **Push messages** go to a fake push service.
- **Phone numbers** are test numbers; no emergency number is involved.
- **Language model:** IRA's deterministic test model, as in CI.

Run it:

```
. <a file exporting IRA_TEST_ADMIN_DSN and IRA_TEST_DATABASE_DSN>
cd supracloud-jarvis/ira
python3 -m pytest -q tests/tenancy/test_care_journey_db.py
```

It takes about 4 seconds. On 2026-11-25 it passed three runs in a row on the same database.

## The steps, and the assertion that proves each one

| # | Step | What happens | Proved by (assertion message in the test) |
|---|---|---|---|
| 1 | Create a care organisation and wearer | The verified founder calls `POST /care/organisations`. This route has no other database-level test. It creates the tenant, the "Care team" workspace and the trial. The founder then adds Edith. | `201` from the route; "step 1: the wearer is on the organisation's list" |
| 2 | Consent and care-circle access | Dee (family, tier 1, a test phone), Cal (carer, tier 2) and Edith herself join the circle. Edith consents to health data for the family and to topics. Dee registers her phone for push. | "step 2: consents recorded"; "the daughter in the circle sees Edith"; "an account outside the circle does not" (a 404, so the person's existence is not revealed) |
| 3 | Pair the watch | The founder makes a pairing code; the watch exchanges it for a device token over `POST /devices/pair`. | "the watch knows whose it is"; "the paired watch is told all is quiet"; "the dashboard shows it paired" |
| 4 | Routines and care schedules | A routine, a medicine at 08:00 and a lunch visit with tasks. | "the routine is saved"; the medicine listed; "the visit is scheduled" |
| 5 | IRA initiates a conversation | The check-in cycle schedules the first check-in. The watch collects a question, by name, with one question mark, composed by the model. Edith answers. | "IRA asks Edith by name"; "composed by IRA's model"; "the answer is on the record" |
| 6 | IRA remembers an authorised preference | Dee sets "her garden roses" as a familiar topic. It is kept only because Edith consented in step 2. Edith talks to IRA. | "kept because Edith consented"; "IRA's next conversation uses the preference" (the model's prompt says "Enjoys: her garden roses"); "the day is remembered, for the family to see and correct" |
| 7 | Medication reminder | A reminder for the Blue tablet falls due; the watch is asked "Have you taken it?". | "the watch asks about the tablet" |
| 8 | An ambiguous answer is handled safely | Edith says "maybe, I can't remember". | "'maybe' is read as unclear"; "the tablet is asked about again later, not ticked off"; "no dose is written from an unclear answer" |
| 9 | A care visit is scheduled and completed | Cal arrives (the watch says he is here) and leaves with an outcome, notes, a handover and the task ticked. | "the watch says the carer is here"; "the visit is the record" |
| 10 | A missed visit is identified | A visit whose window has passed with nobody arriving. | "the job finds the visit that never happened"; the visit's status is `missed`; "an urgent alert for the coordinator" |
| 11 | A simulated fall | The watch reports a hard impact (3.6 g) and stillness. This is simulated: no one fell. | "a suspected fall, asked about first" (status `asking`) |
| 12 | IRA checks wellbeing | The alert waits for Edith's answer, with a deadline. The wrist is asking; nobody is sent; 20 seconds in, nothing escalates. | "the alert waits for Edith's answer"; "the wrist is asking Edith whether she is OK; nobody is sent yet"; "inside the countdown nothing escalates" |
| 13 | The wearer does not respond | 45 seconds on, with no answer, the escalation job promotes the fall. | "silence past the deadline promotes the fall" |
| 14 | An SOS is created | The alert is `escalating` at SOS severity; the wrist says help is being called. | "the SOS is live"; "the wrist says help is called" |
| 15 | A test notification reaches the family | A text to Dee through the recording provider and a push to her registered phone through the fake push service. | "a text to the daughter, by the test provider"; "a push to the daughter's registered phone"; "no emergency number is involved" |
| 16 | The dashboard receives the incident | The alert heads the open alerts, Edith's record shows the live SOS, and the organisation's overview lists it and puts Edith under "needs attention". | "the fall heads the open alerts"; "Edith's record shows the live SOS"; "the organisation's overview shows it" |
| 17 | A caregiver acknowledges it | Dee taps "on my way"; the wrist names her. 20 minutes on, past the 5-minute wait for the next tier, nobody else is called. 31 minutes on with no arrival, IRA asks Dee, and only Dee, whether she has reached Edith. | "the wrist names who is coming"; "once someone is on the way, nobody else is called for this fall"; "'have you reached Edith?' to Dee, and to nobody else" |
| 18 | Arrival and outcome are recorded | Dee arrives and resolves the alert with an outcome; the wrist hears it is over. | "step 18" (status `resolved`, outcome `all_well`, the resolution text); "the wrist hears it is over" |
| 19 | Daily and weekly reports | The daily carer report for today and the weekly family report are generated. | "summaries, never her words" (neither her answer nor her preference appears); "the weekly report covers seven days" |
| 20 | Network failure is simulated | The watch's network is down. Help (with an idempotency key) and four simulated heart-rate readings fail to send. | "nothing reached the server" |
| 21 | Offline functionality | The connection returns for one request. The server acts on the Help, but the answer is lost on the way back, so the watch cannot know it was sent and keeps it. | "the server acted on it although the watch never heard" |
| 22 | Connectivity recovers without duplicate actions | The watch's outbox sends the same Help, with the same key, twice more. It sends the held readings twice. | "the same key is a replay" (`X-Idempotent-Replay: true`, same alert); "one Help, one alert"; "the readings held offline arrive once each, with their own times" |

## What this test does not cover, and where it is covered instead

- **The watch's own outbox and offline queue (steps 20 to 22, the watch side).**
  - This test sends what the watch's outbox would send.
  - The outbox itself, which keeps events on disk across a restart and retries with backoff,
    is Kotlin. Its tests are `watch/app/src/test/.../outbox/OutboxTest.kt` and
    `queue/OfflineQueueTest.kt`. They run in the watch CI job, not here.
- **Sign-up and e-mail confirmation (before step 1).** The founder's account is created
  already verified here. Sign-up and the confirmation mail are tested in
  `test_care_onboarding_db.py`.
- **A real language model.** Steps 5 and 6 use the deterministic test model. A run with a
  real model is a separate requirement.
- **A real SMS, call or push provider.** Steps 15 and 17 use the recording and fake
  providers. Live delivery needs provider accounts.
- **A physical watch and phone.** Not available.

## Negative controls (2026-11-25)

Each protection was removed from the product code for one run, then restored with
`git checkout`. In each case the journey failed at the step that depends on it:

| Protection removed | Where | Failed at |
|---|---|---|
| Idempotent replay of the watch's urgent events | `care/sos.py`: `replay` always answers "new key" | step 22: "the same key is a replay" |
| An unsure medication answer is never a yes | `care/checkins.py`: `read_dose_answer` returns "yes" for an unsure answer | step 8: "'maybe' is read as unclear" (the reminder went to `confirming`) |
| Person-level visibility (D-076) | `care/authz.py`: `may_see_person` lets everyone see everyone | step 2: "an account outside the circle does not" |

## Findings while building it

- No product defect was found.
- One assertion was wrong at first, and the product was right. Half an hour after "on my
  way", IRA pushes the responder "Have you reached Edith?" (`care/followup.py`). That is a
  designed follow-up, not a repeated escalation. Step 17 now asserts both: no other responder
  is called, and the follow-up goes to Dee alone.
- Moving the clock 30 minutes on also opens the ordinary "the watch has not synced" notice.
  The step 17 assertions look only at the fall alert, so that notice does not affect them.
- The organisation's overview still lists Edith under "a visit is late or did not happen"
  after the missed-visit alert is resolved, because the visit itself stays `missed`. That is
  the record of what happened, not a fault, but a coordinator may expect a resolved one to
  drop off the list.

## With the real model

`IRA_JOURNEY_MODEL=real` runs the same 22 steps against the product's real model chain (Ollama,
`gemma4:e4b` then `gemma4:e2b`). `scripts/demo/real_model_journey.py` repeats it and writes the
evidence; the result is in `docs/evidence/demo/REAL_MODEL_RUN.md` (8 of 8 runs passed).
