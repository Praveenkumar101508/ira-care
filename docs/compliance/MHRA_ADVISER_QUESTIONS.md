# IRA CARE: questions for an MHRA regulatory adviser

**DRAFT FOR REVIEW BY A QUALIFIED PERSON.** The build cannot decide these; it can only put
them accurately. The adviser's answers decide what may be said and sold.

## The product, in one paragraph

A watch app, server, phone app and dashboard that check in with a person who lives alone,
notice three kinds of event (no answer and no movement; a heart-rate reading that stays
outside a range a carer set; a hard impact followed by stillness), ask the person first,
and tell the care circle in order. It makes no diagnosis, gives no advice (a filter enforces
it), and never contacts emergency services. See `INTENDED_PURPOSE.md`.

## Qualification

1. With the intended purpose as written, is IRA CARE a medical device under the UK MDR 2002
   (as amended), or outside it as a well-being, safety or telecare product? What in the
   wording or the function would move it across the line?
2. The heart-rate rule compares readings with a range the carer sets, with no built-in
   threshold, and only opens a question to the person. Is "a reading outside the range your
   carer set" monitoring of a physiological process for a medical purpose?
3. The fall detector is a heuristic on the accelerometer (impact, then stillness), stated as
   not a medical claim, with real-world accuracy unmeasured. Does offering it change the
   classification? Does recommending the watch maker's own fall detection as a second layer
   help or complicate matters?
4. The conversation feature uses a language model to compose check-in questions and replies,
   behind a guard that removes diagnosis words and advice. Is there anything in composing a
   question about how someone feels, or reading their answer into "wellbeing, pain
   mentioned, medication taken", that constitutes a medical purpose?
5. Medication: IRA asks "have you taken your tablets?" and records yes, no or unclear. Is a
   reminder and a record of a self-reported dose within the exemption for simple
   adherence support, or not?

## If it is a device

6. Which class, and which conformity route? Which harmonised or designated standards would
   be expected (software lifecycle, risk management, usability, cybersecurity)?
7. What would the clinical evaluation need to contain for a product whose benefit claim is
   "help arrives sooner"? Would a pilot with real devices be sufficient, and of what size?
8. What labelling and instructions are required, and are the current statements ("not a
   medical device", "never dials emergency services") compatible with device status?

## If it is not a device

9. What must the product avoid saying or doing to stay outside? Specifically: may the
   dashboard show a heart-rate chart; may a report say "3 readings outside the range"; may a
   carer who is a clinician set the range?
10. Does the DCB0129 / DCB0160 expectation apply when the deployer is an NHS or social care
    body, even if the product is not a device?

## Operational

11. What incident reporting applies (Yellow Card, DSPT incident reporting, or none) for a
    missed escalation or a false fall alarm with consequences?
12. Is there anything in the planned Apple Watch version (`docs/APPLE_WATCH_PLAN.md`) that
    changes the answers above?
13. What should the privacy notice and terms say about regulatory status, in your view?

## What we can show the adviser

`INTENDED_PURPOSE.md`, `CLINICAL_SAFETY_CASE.md` (the hazard log), `DPIA.md`,
`DTAC_ANSWERS.md`, `docs/PRODUCT_SPEC.md`, `docs/DECISIONS.md`, the test suites, and the demo
(`docs/demo/DEMO_SCRIPT.md`).
