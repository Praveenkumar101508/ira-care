# IRA CARE: product specification

IRA CARE is a proactive companion that runs on a smartwatch worn all day and all night by
an older person who lives independently. It starts conversations itself, notices when the
person does not answer, reads the watch's health sensors, and raises an alarm that reaches
a human who can go and check in person. Family use a phone app. Carers and care
organisations use a web dashboard.

This document is the specification the code is built to. `BUILD_RULES.md` says how the
code is built, `DECISIONS.md` records every choice made where this document left room, and
`progress/STATUS.md` says what exists today.

## 1. The promise

1. **Company.** Every 15 to 20 minutes while the person is awake, IRA starts a short, warm
   conversation by voice: meals, medication, mood, pain, plans, how they feel.
2. **Attention.** If there is no answer, IRA repeats louder, then vibrates, then checks the
   watch's readings: heart rate, movement, whether the watch is on the wrist, battery.
3. **Help.** If the readings are outside the range the carer set, or there is no answer and
   no movement, IRA raises an SOS. The SOS escalates in tiers (family and carer, then the
   care organisation, then emergency help) until a human acknowledges and goes to check.
4. **Visibility.** Family see live status, alerts and summaries on their phone. Carers see
   many people at once: vitals, check-ins, routines, medication, alerts and (with consent)
   conversation history.
5. **Tone.** IRA speaks like a kind, patient companion: short sentences, one question at a
   time, remembers names and routines. It never gives a medical diagnosis or medical advice.

## 2. Who uses it

| Role | Device | Sees | Does |
| --- | --- | --- | --- |
| The person | Watch (and optionally the phone app) | Check-ins, "I'm OK" and "Help" | Answers by voice or by button; asks for help |
| Family member | Phone app | Alerts, live status, summaries; transcripts only with consent | Acknowledges alerts ("I'm on my way"), calls the person, manages contacts |
| Carer | Dashboard (and phone app) | Everything above plus timelines, routines, medication | Sets safe ranges, quiet hours, routines; acknowledges alerts |
| Care organisation | Dashboard | Many people at once | Receives tier-2 escalations; staff roles |
| Clinician | Dashboard | A person's vitals and ranges | Sets the safe ranges |

Roles map onto the existing login model: `viewer`, `trusted_user`, `family_admin`, and the
owner. A **care circle** is the set of people around one person, each with a role and
contact details, in priority order for escalation.

## 3. The check-in cycle

- Check-ins happen every 15 to 20 minutes, randomised, during waking hours.
- **Quiet hours** (default 22:00 to 07:00, per person) have silent monitoring only: no
  voice, but sensors, wrist-on, battery and sync are still watched.
- Each check-in is one short question drawn from the person's routine and recent answers
  (meal, medication, mood, pain, plans, how they feel). Answers are stored, summarised and
  remembered, so the next question can refer back ("Did the paracetamol help?").
- No answer: repeat after 1 minute, louder and with vibration. After 2 more minutes with
  no answer, check the sensors (section 4).
- Three missed check-ins in a row with normal readings is itself an alert.
- The **Help** button on the watch, or the spoken word "help", escalates immediately.

## 4. "Is this normal?" rules

The product makes **safety** judgements ("no answer and no movement"), never medical ones.
There are no built-in medical thresholds and no diagnosis words anywhere in the product.

| Signal | Rule (per person, carer-configurable) | Outcome |
| --- | --- | --- |
| No answer to a check-in | After the repeat and the sensor check | Missed check-in; escalate after 3 in a row |
| No answer and no movement | Movement below the person's floor during the check | Escalate now |
| Heart rate outside the carer-set range | Range set by a carer or clinician; absent range means no heart-rate rule | Escalate now |
| Watch off the wrist during the day | 30 minutes by default | Alert the carer |
| Battery low | Under 15 % by default | Alert the carer |
| Background sensor permission turned off | Reported by the watch | Alert the carer |
| No sync from the watch | 30 minutes by default | Alert the carer |

## 5. Escalation and SOS

| Tier | Who | How | Waits for |
| --- | --- | --- | --- |
| 1 | Family and carer, in care-circle priority order | Push, SMS, phone call | An acknowledgement within 5 minutes |
| 2 | The care organisation | Push, SMS, phone call, dashboard banner | An acknowledgement within 5 minutes |
| 3 | Emergency | A monitoring-centre adapter if one is configured; otherwise tiers 1 and 2 repeat and family see a clear "call 999 now" screen | A human |

- Every step is retried and every attempt, delivery and acknowledgement is logged.
- An acknowledgement ("I'm on my way") stops the escalation and is shown to everyone.
- **The system never dials 999 itself.** In the UK, automated alarms go to a monitoring
  centre that decides whether to call the emergency services; that connection is an
  adapter, off until a contract exists.
- SMS and phone calls go through a provider interface. The default provider only logs.
  A paid adapter stays off unless its keys are configured.

## 6. The watch app (Wear OS)

- Galaxy Watch 4 and later (Wear OS 3+); Kotlin, Jetpack Compose for Wear OS.
- Runs all day as a foreground service. Speaks with the device's own text-to-speech and
  listens with the device's speech recogniser.
- Big "I'm OK" and "Help" buttons on every check-in screen.
- Sensors through Wear OS Health Services (heart rate, steps, movement, wrist-on), behind an
  interface so a vendor sensor SDK can be added after partner approval.
- Works offline: readings and answers queue locally and sync when back online.
- Pairs with a care circle through a short code shown in the family app or dashboard.
- Setup walks the family through the background body-sensor permission; the app reports
  when the permission is turned off.

## 7. The family and patient phone app

Live status, alerts with one-tap acknowledgement, call the person, history, contacts and
care-circle management, settings, consent screens. Push notifications for alerts.

## 8. The carer dashboard (web)

Many people at once. For each person: timelines of vitals, check-ins, routines, medication
and alerts; conversation history (respecting consent); the safe ranges and quiet hours;
the care circle. Roles for family, carers and care organisations. Daily carer report and
weekly family report (section 10).

## 9. Data model

Each family and each care organisation is kept apart by the existing tenant and workspace
isolation (row-level security). The care tables are:

people (the person cared for), care circle members, devices, vitals (time series),
check-ins and answers, alerts, acknowledgements, escalation attempts, routines,
medications and medication events, transcripts, consents, safety settings (ranges, quiet
hours, thresholds).

## 10. Reports and history

- **Daily carer report:** vitals trends, check-ins answered and missed, routine adherence,
  medication confirmations, alerts, notable conversation points.
- **Weekly family report:** the same in family language, without transcripts unless
  consented.
- **Searchable history** for carers, every view audited.

## 11. Privacy and consent

- Family see alerts and summaries by default. Full transcripts only when the person (or
  their legal representative) has consented, and the consent is recorded in the system.
- Every view of health data or a transcript is written to the audit log.
- Default retention 12 months, configurable per family or organisation.
- Audio is never stored. Speech is transcribed on the device or the server and the audio
  is discarded.

## 12. Limits the design builds around

| Limit | What the build does |
| --- | --- |
| Hospitals and emergency services do not take automated alerts | Tiers reach humans; a monitoring-centre adapter is a plug-in; the app never dials 999 |
| Remote monitoring software may be a medical device | No diagnosis words, no built-in medical thresholds, carer-set ranges, safety alerts only; regulatory advice before sale |
| The vendor sensor SDK needs partner approval and a real watch | Health Services first, vendor SDK behind an interface later |
| Background sensing needs the user's permission | Guided setup, alert when the permission is off |
| Apple Watch cannot monitor continuously in the background | Later, on a Mac, using the data the watch records itself (`APPLE_WATCH_PLAN.md`) |

## 13. Out of scope for now

Apple Watch (plan only), the vendor sensor SDK, a monitoring-centre contract, SMS and call
provider keys, regulatory approval, payment and billing for care.
