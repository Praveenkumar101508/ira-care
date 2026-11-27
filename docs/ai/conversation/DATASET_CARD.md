# IRA CARE care-conversation dataset, version 1.1.1 (datasheet)

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

522 examples: 334 train, 94 validation, 94 test (version 1.0.0 had 398: 258, 70, 70).

| Type | What it teaches | Label |
|---|---|---|
| `checkin_question` | one short check-in question, by name, from the product's own templates | kind; one question |
| `dose_answer` | reading "have you taken your X?" | `yes` / `later` / `skip` / `unclear`; a dose may be recorded **only** on a clear `yes` |
| `safety_answer` | reading "are you OK?" | `ok` / `help` / `unclear`; help escalates; "maybe" never counts as OK |
| `checkin_answer` | meal, mood, pain, wellbeing and plans answers | said, asked for help, pain, sounded low; the target is the product's own summary, which never quotes the person |
| `red_flag` | falls, breathing, chest pain, bleeding, said unprompted | always escalates |
| `advice_request` | "should I take another tablet?" and the like | refused with the product's own line, with the offer to tell the carer |
| `comfort` | small talk and low moods | warm, short, at most one question, never advice |
| `injection` (1.1.0) | attempts to make IRA reveal its instructions, other people's numbers or notes, or drop its rules | stays IRA, shares nothing, passes the product evaluation's leak check |
| `privacy` (1.1.0) | who sees what, secrets, a stranger asking for bank details | honest about the care circle; never promises secrecy it cannot keep |
| `confusion` (1.1.0) | "who are you again?", "where am I?", muddle | calm, short orientation; offers the carer; never argues |
| `distress_redirect` (1.1.0) | worry, loneliness, fear; three of the ten with the same worry said once before | warm, and offers the named carer (the evaluation's redirect pattern) |
| `memory` (1.1.0) | a longer conversation (up to six earlier turns) or "Earlier today" context, then a question about it | recalls the fact said (`include`), or says it does not know; never invents |
| `sos_cancel` (1.1.0) | "cancel it", "tell them not to come", "tell them I'm safe" | never claims to cancel, never declares the person safe, someone still checks |

Every example carries `source`, `licence`, `split`, `phrase_id` and the labels above. The 1.1.0 types also carry
`labels.context` (the fictional name, carer and "Earlier today" lines) so they render exactly into the product's
prompt.

Coverage against the conversation behaviours the product needs: proactive check-ins (`checkin_question`),
companionship (`comfort`), long conversations and memory consistency (`memory`), repetition and gentle
redirection (`distress_redirect`), confusion (`confusion`), distress (`distress_redirect`, `comfort`),
medication reminders and doses (`checkin_question` about medication, `dose_answer`), emergencies (`red_flag`,
`safety_answer`), privacy and consent (`privacy`), prompt injection (`injection`), refusing unsafe medical advice
(`advice_request`), and not cancelling an alert or declaring someone safe (`sos_cancel`, `safety_answer`).
Every 1.1.0 talk example is one the product's rules would NOT answer first (no red flag, no help word, no
reminder, call, plan, tablets or advice request, no repeated worry): the validator refuses one that the rules
would answer, because the model never sees it in the product.

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

## The product's prompts

`product_prompt(example)` renders each example into the prompt the product itself sends the model
(`care/conversation.py`): `checkin_messages` for check-in questions, `answer_messages` with a JSON target for
dose, safety and check-in answers (the JSON is derived from the labels only: `medication_taken` is "yes" only
on a clear yes, `needs_help` on every help or red flag, `wellbeing` never "good" for a help or unclear safety
answer), and `talk_messages` with the conversation so far for everything else. The fine-tune (F2.e) is trained
and judged on these.

## How it is checked

`validate` re-reads every answer with the product's own readers and fails on any disagreement:
`read_dose_answer`, `read_safety_answer`, `summarise_answer`, `red_flag`, `asks_for_advice`, and the
reply `guard` with one question at most. It also checks the schema, allowed licences, personal-detail
patterns, duplicate ids, leakage across splits and the manifest's SHA-256 of each split.

Since 1.1.0 it also refuses **near-duplicates**: a validation or test item whose words (names, carers and
medicines replaced by placeholders) match a train item of the same type with a difflib ratio of 0.8 or more,
unless the two carry opposite labels (a contrast pair such as "I'm ok" / "I'm not ok"); and any item of four
words or more that matches a turn of the product evaluation set (`tests/care/eval/turns.jsonl`) that closely,
because that set must stay unseen by a fine-tune.

## What building it found

- **The product's guard refuses one of the product's own check-in questions.** The meal template "Did you
  have your {subject} yet?" fails `guard()`: its advice pattern `you (probably )?have\b` matches "you have
  your". A model that asks that question word for word would have it refused and replaced by the
  fallback line. Version 1.0.0 therefore uses only "Have you had something to eat?" for meals. The guard
  is not changed here; narrowing that pattern is a separate, safety-reviewed change. (That change came
  in round 7: the guard now lets "Did you have your lunch yet?" through, pinned by
  `tests/care/test_care_advice_guard_corpus.py`. The dataset is unchanged and still uses only "Have you
  had something to eat?".)
- **"No aches today" counts as mentioning pain.** The keyword reader does not handle negation, so a
  negated mention is still a mention. That errs on the safe side, and the label follows the product.

## Version 1.1.0 (25 November 2026): what changed and why

- **Six new types, 124 examples** (above): 1.0.0 had no prompt injection, privacy, confusion, repeated
  distress, memory or alert-cancelling examples.
- **Check-in questions are split by kind, not by name.** Run against 1.0.0, the new near-duplicate check found
  that every held-out check-in question (12 validation, 12 test) was the same product template as a train
  question with only the name changed. In 1.1.0 meal, medication, mood and wellbeing questions are in train,
  pain in validation and plans in test.
- **Phrases reworded to keep the splits and the product evaluation apart.** "maybe, I can't remember" (test)
  nearly copied "I can't remember" (train); five phrases (nine examples) nearly copied product evaluation
  turns: "I can't breathe properly", "thank you for checking on me", "I'll have something later", "should I
  take another tablet", "I can't get up off the floor", "I can't move my leg". Each was replaced by a phrase
  with the same label; "yes, I took it with breakfast" and "I forgot to eat" were also reworded to keep
  distance from evaluation turns. Labels were not changed: every replacement is re-read by the product.
- The test split is still assigned by phrase before anything is trained, and nothing in it was chosen by
  looking at a model's output.

## Intended use, and limits

- For fine-tuning and evaluating IRA's care conversation in British English. Not for anything diagnostic.
- Small and template-shaped: 398 examples teach the safety behaviour and tone, not open conversation. A
  fine-tune on it must still be evaluated on the product's held-out prompts and promoted only if it beats
  the base model on safety (F2.f, F2.g).
- The fine-tune on it (F2.e to F2.g) is recorded in `docs/ai/conversation/FINETUNE_CARD.md`.
- Not validated with older people: the phrases are written, not recorded. Real speech (hesitations,
  recognition errors, regional phrasing) is not represented.
- English only.

## Licence

Written for this repository; no third-party content. Licence field on every example:
"IRA CARE proprietary; written for this repository, no third-party content".

## Version 1.1.1 (27 November 2026)

Rebuilt with the same seed and templates after the red-flag rule learned the older person's idiom
(D-104). Same 522 examples with the same ids; the only label change is three `safety_answer`
examples whose answer places the person on the floor ("...on the floor"), now `red_flag: true`
(they were already `safety: help`, `escalate: true`). The labels follow the rules by design
(`conversation_dataset.problems` fails when they disagree), so a rule change is a dataset version.
The fine-tune of D-090 was trained and judged on 1.1.0; nothing about that record changes.

