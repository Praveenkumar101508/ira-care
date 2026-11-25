# IRA CARE care-conversation dataset, version 1.0.0 (datasheet)

Requirement F2.d. Files: `docs/ai/conversation/dataset/{train,validation,test}.jsonl` and
`MANIFEST.json` (version, seed, SHA-256 and counts per split). Built and checked by
`scripts/training/conversation_dataset.py`; `tests/readiness/test_conversation_dataset.py` fails if the
committed files stop passing the checks or stop matching what the builder makes.

## Motivation

A fine-tune of IRA's conversation model (F2.e) needs examples of how IRA must talk to an older person:
a short warm check-in, one question at a time, and above all the safe reading of answers. The product
already enforces these with rules (`care/checkins.py`, `care/conversation.py`); this dataset turns those
rules into labelled examples, so a fine-tuned model is taught the same behaviour the rules hold it to.

## Composition

398 examples: 258 train, 70 validation, 70 test.

| Type | What it teaches | Label |
|---|---|---|
| `checkin_question` | one short check-in question, by name, from the product's own templates | kind; one question |
| `dose_answer` | reading "have you taken your X?" | `yes` / `later` / `skip` / `unclear`; a dose may be recorded **only** on a clear `yes` |
| `safety_answer` | reading "are you OK?" | `ok` / `help` / `unclear`; help escalates; "maybe" never counts as OK |
| `checkin_answer` | meal, mood, pain, wellbeing and plans answers | said, asked for help, pain, sounded low; the target is the product's own summary, which never quotes the person |
| `red_flag` | falls, breathing, chest pain, bleeding, said unprompted | always escalates |
| `advice_request` | "should I take another tablet?" and the like | refused with the product's own line, with the offer to tell the carer |
| `comfort` | small talk and low moods | warm, short, at most one question, never advice |

Every example carries `source`, `licence`, `split`, `phrase_id` and the labels above.

## Collection

**All examples are synthetic. No real person's words are in it.** No public dialogue set is licensed for
commercial use (`docs/DATASETS.md`, "Empathetic, elderly and care conversations"), so none was used. The
answer phrases were written by hand, each with the label the product must give it, and expanded over
fictional names, carers and medicines, using a fixed seed (20261125). The names are common British first
names chosen as fiction; no surnames, addresses, phone numbers or dates of birth appear, and the
validator rejects text that looks like a phone number, an NHS-number-like digit string, a postcode, an
email or a street address.

## Splits

By phrase, not by example: every expansion of one hand-written phrase is in the same split, assigned
round-robin within each label (three in five to train, one to validation, one to test), so every label
appears in every split and no phrasing leaks from training into test.

## How it is checked

`validate` re-reads every answer with the product's own readers and fails on any disagreement:
`read_dose_answer`, `read_safety_answer`, `summarise_answer`, `red_flag`, `asks_for_advice`, and the
reply `guard` with one question at most. It also checks the schema, allowed licences, personal-detail
patterns, duplicate ids, leakage across splits and the manifest's SHA-256 of each split.

## What building it found

- **The product's guard refuses one of the product's own check-in questions.** The meal template "Did you
  have your {subject} yet?" fails `guard()`: its advice pattern `you (probably )?have\b` matches "you have
  your". A model that asks that question word for word would have it refused and replaced by the
  fallback line. Version 1.0.0 therefore uses only "Have you had something to eat?" for meals. The guard
  is not changed here; narrowing that pattern is a separate, safety-reviewed change.
- **"No aches today" counts as mentioning pain.** The keyword reader does not handle negation, so a
  negated mention is still a mention. That errs on the safe side, and the label follows the product.

## Intended use, and limits

- For fine-tuning and evaluating IRA's care conversation in British English. Not for anything diagnostic.
- Small and template-shaped: 398 examples teach the safety behaviour and tone, not open conversation. A
  fine-tune on it must still be evaluated on the product's held-out prompts and promoted only if it beats
  the base model on safety (F2.f, F2.g).
- **The fine-tune itself (F2.e) is not run here.** It needs a GPU session on the owner's account.
- Not validated with older people: the phrases are written, not recorded. Real speech (hesitations,
  recognition errors, regional phrasing) is not represented.
- English only.

## Licence

Written for this repository; no third-party content. Licence field on every example:
"IRA CARE proprietary; written for this repository, no third-party content".
