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

`docs/PRODUCT_SPEC.md` (the specification), `docs/DECISIONS.md` (every decision, D-001
onwards), `docs/progress/STATUS.md` (what was measured), and the test suites named in the
hazard log (`CLINICAL_SAFETY_CASE.md`).

## Checked against current MHRA guidance (read 25 November 2026)

Read at their source on gov.uk on 25 November 2026, through the GOV.UK content API:

- "Crafting an intended purpose in the context of Software as a Medical Device (SaMD)",
  MHRA, published 22 March 2023 (no later update);
- "Medical devices: software and artificial intelligence (AI)", MHRA, updated 3 February 2025;
- "Regulating medical devices in the UK", MHRA, updated 20 February 2026;
- "Medical devices: software applications (apps)", MHRA, updated 1 July 2023 (the PDF guidance
  itself was listed, not read in full).

What they change for this statement:

1. **The four elements.** The MHRA guidance breaks an intended purpose into the structure and
   function of the device (inputs, outputs, the user's actions, and its place in the care
   workflow), the intended population, the intended user and the use environment. The statement
   above covers function, users and environment. It did not name the population precisely or
   say who it is **not** for; the guidance says that without contraindicated populations "the
   widest application will be assumed". Added below.
2. **Software in a device, not only software.** The guidance is written for standalone software
   (SaMD) and says it "has not been specifically drafted for products with hardware components";
   its principles may still help. IRA CARE today runs on consumer watches (software), but the
   IRA watch in `docs/hardware/` would make it software in a device. The adviser question on this
   is added to `MHRA_ADVISER_QUESTIONS.md`.
3. **Machine learning in the alerting path.** Since D-080 (fall model v3) a trained model decides
   which impacts become a "did you fall?" question on release builds. The MHRA page on software and
   AI points to the joint MHRA, FDA and Health Canada guiding principles for good machine learning
   practice, for predetermined change control plans and for transparency. If the product is a
   device, these apply to the fall model; `docs/ai/fall/MODEL_CARD_V3.md` is the starting record.
4. **Post-market surveillance.** "Regulating medical devices in the UK" states that strengthened
   post-market surveillance requirements for devices in Great Britain came into force on
   16 June 2025, and that every device must be registered with the MHRA before it is placed on
   the market. If the adviser finds the product is a device, `INCIDENT_PROCESS.md` must be
   reviewed against those requirements and the SaMD vigilance guidance (not read here).

### Intended population, and who it is not for

- **For:** adults living alone or spending long periods alone, able to wear a watch and to
  answer a short spoken or on-screen question, whose family or carers have agreed to receive
  alerts.
- **Not for, and the product must say so:**
  - anyone for whom a missed or late alert would be dangerous without another safeguard in place:
    the service can miss falls and can be down;
  - people who need continuous clinical monitoring of heart rhythm or any vital sign: it
    measures no ECG, oxygen or blood pressure (`care/capabilities.py`);
  - people who cannot consent and have no one with authority to consent for them;
  - use as the only means of summoning emergency help: it never dials emergency services.

Nothing in this section is a regulatory conclusion. Whether IRA CARE is a medical device stays a
question for a qualified adviser.
