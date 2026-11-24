#!/usr/bin/env python3
"""Build the labelled sentences the red-flag and answer classifier is trained on.

    python scripts/training/make_classifier_data.py > scripts/training/classifier_data.jsonl

Templates and fillers, not real people's words, and deliberately disjoint from
the 150 evaluation turns in tests/care/eval/turns.jsonl (the evaluation set is
the test; training on it would prove nothing). Each line: {"text", "needs_help",
"wellbeing", "said", "advice"}. `wellbeing` is good | ok | poor | unknown; `said`
is yes | no | unclear | none (none for open questions).
"""
from __future__ import annotations

import json
import pathlib
import random
import sys

rng = random.Random(20261123)
NAMES = ["", "dear", "love", "IRA"]
PREFIX = ["", "Well, ", "Oh, ", "Hmm, ", "To be honest ", "I'd say ", "Erm, "]

GOOD = ["I'm fine", "I'm very well", "I'm good thanks", "I feel great today", "quite well", "not bad at all", "I'm alright",
        "I'm happy", "lovely day, I'm cheerful", "better than yesterday", "I'm okay", "I'm well thank you", "right as rain",
        "I'm in good spirits", "I slept like a log", "I'm chipper today", "can't complain", "I'm comfortable"]
OK = ["I'm so-so", "a bit tired", "I'm managing", "not too bad", "I'm alright I suppose", "a little weary", "middling",
      "I'm getting by", "a bit stiff this morning", "I'm okay, just slow today", "fair to middling", "I'm coping"]
POOR = ["I feel awful", "I'm not well", "I'm poorly today", "I feel terrible", "I'm very low", "I'm lonely", "I feel rotten",
        "I'm unwell", "I'm miserable", "I feel so sad", "I'm worried sick", "I'm anxious", "I had a dreadful night", "I feel weak",
        "I'm really down", "I'm frightened", "I can't stop crying", "I feel sick", "everything hurts and I'm fed up"]
PAIN = ["my knee hurts", "my back is sore", "I've a headache", "my hip aches", "my chest feels tight", "my legs ache",
        "I'm dizzy when I stand", "my arm is sore", "I've a pain in my side", "my feet hurt"]
HELP = ["I've fallen", "I fell over", "I've had a fall", "I can't get up", "I'm on the floor", "help me", "I need help",
        "I can't breathe", "I have chest pain", "my chest hurts badly", "I'm bleeding", "I can't move", "I think I'm having a heart attack",
        "I collapsed", "I passed out", "I need an ambulance", "please send someone", "I'm stuck", "I can't feel my arm", "I fell down the stairs",
        "I've hurt myself badly", "I can't stand up", "something is very wrong", "I blacked out", "I'm choking"]
YES = ["yes I took them", "already taken", "I had them with breakfast", "yes, done", "took them an hour ago", "yes all done",
       "I've had them", "yes with my tea", "I did take them", "taken, yes", "yes I ate", "I had my lunch", "I've eaten thank you",
       "yes, soup", "I had a sandwich", "yes I've had something"]
NO = ["not yet", "I forgot", "no I haven't", "I'll take them later", "no, not today", "I ran out", "I haven't eaten", "no, nothing yet",
      "I didn't", "I missed them", "no I couldn't face it", "not hungry so no", "I skipped it", "haven't had a chance"]
UNCLEAR = ["what tablets", "I don't remember", "which ones", "maybe", "I'm not sure", "did I?", "possibly", "I can't recall",
           "what do you mean", "hmm"]
ADVICE = ["should I take another one", "can I double the dose", "is it safe with wine", "how many can I take", "what's wrong with me",
          "do I have an infection", "should I stop the pills", "is this serious", "what should I do about the swelling", "can I take the old antibiotics",
          "is it normal to feel like this", "should I go to hospital", "do I need a doctor", "what tablet is best for pain", "how much ibuprofen is safe",
          "should I skip them today", "can I have two sleeping pills", "do you think it's the flu", "what dose should I be on", "is this a stroke"]
NEUTRAL = ["the weather is dreadful", "my grandson phoned", "I'm watching the telly", "I did the crossword", "the cat came in",
           "I'm making tea", "I'm off to bed soon", "the post hasn't come", "I baked a cake", "it's my birthday next week",
           "tell me something nice", "what's on today", "goodnight", "thank you for asking", "I'm going to do some knitting"]

rows = []
def add(text, needs_help=False, wellbeing="unknown", said="none", advice=False, pain=False):
    rows.append({"text": text, "needs_help": needs_help, "wellbeing": wellbeing, "said": said, "advice": advice, "pain": pain})

def dress(core):
    p = rng.choice(PREFIX); n = rng.choice(NAMES)
    return f"{p}{core}{(' ' + n) if n else ''}".strip().capitalize()

for _ in range(6):
    for g in GOOD: add(dress(g), wellbeing="good")
    for o in OK: add(dress(o), wellbeing="ok")
    for p in POOR: add(dress(p), wellbeing="poor")
    for p in PAIN: add(dress(p), wellbeing=rng.choice(["ok", "poor"]), pain=True)
    for h in HELP: add(dress(h), needs_help=True, wellbeing="poor", pain=True)
    for y in YES: add(dress(y), said="yes", wellbeing=rng.choice(["good", "unknown"]))
    for nn in NO: add(dress(nn), said="no")
    for u in UNCLEAR: add(dress(u), said="unclear")
    for a in ADVICE: add(dress(a), advice=True)
    for ne in NEUTRAL: add(dress(ne), wellbeing="unknown")
# Mixed sentences: a dose answer with a mood, a complaint with a help request.
for _ in range(60):
    add(dress(f"{rng.choice(YES)}, and {rng.choice(POOR)}"), said="yes", wellbeing="poor")
    add(dress(f"{rng.choice(NO)}, {rng.choice(GOOD)} though"), said="no", wellbeing="good")
    add(dress(f"{rng.choice(PAIN)} and {rng.choice(HELP)}"), needs_help=True, wellbeing="poor", pain=True)
    add(dress(f"{rng.choice(GOOD)} but {rng.choice(ADVICE)}"), wellbeing="good", advice=True)
rng.shuffle(rows)
# Never a sentence that is also in the evaluation set: the evaluation is the test.
EVAL = pathlib.Path(__file__).resolve().parents[2] / "supracloud-jarvis" / "ira" / "tests" / "care" / "eval" / "turns.jsonl"
seen = {json.loads(line)["text"].lower() for line in EVAL.read_text().splitlines() if line.strip()} if EVAL.exists() else set()
for r in rows:
    if r["text"].lower() in seen:
        continue
    seen.add(r["text"].lower())
    sys.stdout.write(json.dumps(r, ensure_ascii=False) + "\n")
