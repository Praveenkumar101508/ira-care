# IRA CARE: intended purpose statement

**DRAFT FOR REVIEW BY A QUALIFIED PERSON.** Written by the build from what the software does.
It claims no regulatory status and no compliance; it exists so that a regulatory adviser and
a clinical safety officer have an accurate description to review.

## What IRA CARE is intended to do

IRA CARE is a companion and alerting service for an adult who lives alone and the family
and carers who look after them. Through a watch the person wears, it:

1. asks short, friendly questions a few times a day and listens for the answer;
2. notices three kinds of event: no answer together with no movement; a heart-rate
   reading that stays outside a range a carer set; and a hard impact followed by stillness
   (a suspected fall); and the person asking for help by button or by voice;
3. asks the person "are you OK?" first, and, with no answer, tells the people in the
   person's care circle in a set order (family and carers, then the care organisation,
   then a message that says to call emergency services), with the home address and the
   watch's location at that moment;
4. lets the person talk to the companion at any time (company, reminders, "what's on
   today", "did I take my tablets", "call my daughter"), with replies that never give
   medical advice;
5. gives family and carers a view of how the person is (summaries, and the person's own
   words only with recorded consent), daily and weekly reports, and a record of who looked.

## Intended users

- The person looked after: an adult who lives alone, able to wear a watch and answer a
  spoken question, with or without a family member's help setting it up.
- Family members and informal carers: through the phone app.
- Care organisations and their staff: through the dashboard.

## Intended use environment

At home and about, on a consumer smartwatch (Wear OS 3 and later) with a data connection,
and on ordinary phones and web browsers. The server runs on infrastructure the operator
chooses (the deployment guide places it on one machine in a UK region).

## What IRA CARE is not intended to do, and says so

- It does not diagnose, treat, monitor a disease or a physiological process for a medical
  purpose, or give medical advice. The heart-rate range is the carer's own setting with no
  built-in threshold; alerts describe what was observed in plain words; a filter removes
  diagnosis words from every message and every model reply.
- It does not contact emergency services. It tells the people in the care circle, and at
  the last tier shows them "call 999 now".
- It is not a guarantee. A watch can be off the wrist, flat or out of signal; the server can
  be down (and says so with a "monitoring paused" banner); a fall can go undetected or be
  detected when there was none (the detector's real-world accuracy is unmeasured).

## Claims made in the product

"A gentle companion on the wrist that checks in and gets help when needed." "IRA CARE never
dials emergency services itself." "It is not a medical device and makes no diagnosis."
These are the claims in the store listings and the terms; the adviser should say whether the
heart-rate rule and the fall detector, as built, keep the product outside the medical
device definition or bring it within it (see `MHRA_ADVISER_QUESTIONS.md`).

## Where the behaviour is specified and tested

`docs/PRODUCT_SPEC.md` (the specification), `docs/DECISIONS.md` (every decision, D-001 to
D-047), `docs/progress/STATUS.md` (what was measured), and the test suites named in the
hazard log (`CLINICAL_SAFETY_CASE.md`).
