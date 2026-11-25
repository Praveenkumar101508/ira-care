"""IRA CARE care-conversation dataset (requirement F2.d): build it, or check it.

    python3 scripts/training/conversation_dataset.py build      # writes docs/ai/conversation/dataset/
    python3 scripts/training/conversation_dataset.py validate   # exits non-zero on any problem

No public dialogue set is licensed for commercial use (docs/DATASETS.md, "Empathetic, elderly and
care conversations"), so every example is SYNTHETIC: hand-written phrases, each with the label the
product must give it, expanded over fictional names, medicines and the product's own question
templates with a fixed seed. Nothing comes from a real person.

Splits are by PHRASE, not by example: every expansion of one phrase lands in the same split, so a
model cannot pass the test split by having memorised the training phrasing (no template leakage).

`validate` re-reads every answer example with the product's own rule-based readers
(care/checkins.py: read_dose_answer, read_safety_answer, summarise_answer; care/conversation.py:
red_flag, asks_for_advice, guard) and fails if a label disagrees. A disagreement is a finding to
look at, never something to relabel quietly.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IRA = ROOT / "supracloud-jarvis" / "ira"
OUT = ROOT / "docs" / "ai" / "conversation" / "dataset"
VERSION = "1.0.0"
SEED = 20261125
SOURCE = "synthetic:ira-care-templates"
LICENCE = "IRA CARE proprietary; written for this repository, no third-party content"
ALLOWED_LICENCES = {LICENCE}
SPLITS = ("train", "validation", "test")

# ── Fictional context ─────────────────────────────────────────────────────────
NAMES = ("Edith", "Arthur", "Mabel", "Harold", "Joan", "Walter", "Irene", "Stanley", "Doris", "Frank",
         "Margaret", "Bert")
CARERS = ("Sarah", "James", "Priya", "Tom", "Aisha", "Daniel")
MEDICINES = ("morning tablets", "evening tablet", "water tablet", "blood pressure tablet", "inhaler",
             "eye drops", "vitamin D", "heart tablet")
OTHER_MEDICINE = "painkillers"

# ── Phrase banks: (phrase, intended label). Hand-written. ─────────────────────
DOSE = [  # answer to "Have you taken your {medicine}?"  -> yes | later | skip | unclear
    ("yes", "yes"), ("yes I have", "yes"), ("yes, I took it with breakfast", "yes"), ("I've taken it", "yes"),
    ("yes, just now", "yes"), ("all done, thank you", "yes"), ("took it an hour ago", "yes"),
    ("yes I did", "yes"), ("already taken", "yes"), ("yep, done", "yes"),
    ("not yet", "later"), ("I'll take it later", "later"), ("I haven't yet", "later"),
    ("I forgot, I'll do it now", "later"), ("not yet, after my tea", "later"),
    ("I'm going to take it after lunch", "later"), ("no, not yet", "later"), ("I'll have it in a minute", "later"),
    ("no", "skip"), ("nope", "skip"), ("I'm not taking it today", "skip"), ("I'm skipping it today", "skip"),
    ("no, not today", "skip"),
    ("maybe", "unclear"), ("I can't remember", "unclear"), ("I think so", "unclear"), ("not sure", "unclear"),
    ("maybe, I can't remember", "unclear"), ("I took the other one", "unclear"), ("okay", "unclear"),
    ("I took my painkillers", "unclear"), ("I don't know", "unclear"), ("probably, I'm not sure", "unclear"),
    ("I took it yesterday", "unclear"), ("hmm", "unclear"),
]
SAFETY = [  # answer to "{name}, are you OK?"  -> ok | help | unclear
    ("yes I'm fine", "ok"), ("I'm okay thanks", "ok"), ("I'm fine", "ok"), ("yes", "ok"),
    ("all right thank you", "ok"), ("I'm ok", "ok"), ("yes I'm alright", "ok"), ("fine thanks", "ok"),
    ("I don't need any help", "ok"), ("I'm good", "ok"),
    ("help", "help"), ("I've fallen", "help"), ("I can't get up", "help"), ("no, I'm on the floor", "help"),
    ("I feel dizzy", "help"), ("my chest hurts", "help"), ("I'm hurt", "help"), ("I need help", "help"),
    ("no", "help"), ("I'm not ok", "help"), ("yes but I can't get up", "help"), ("I feel faint", "help"),
    ("I'm stuck", "help"), ("I'm bleeding", "help"), ("I feel awful", "help"),
    ("maybe", "unclear"), ("I think so", "unclear"), ("not sure", "unclear"), ("I don't know", "unclear"),
    ("sort of", "unclear"), ("hmm", "unclear"), ("what?", "unclear"), ("I suppose", "unclear"),
]
GENERAL = [  # (kind, phrase, intended {said?, help, pain, low})
    ("meal", "yes, I had toast", {"said": "yes"}), ("meal", "yes I've eaten", {"said": "yes"}),
    ("meal", "I had soup earlier", {"said": "unclear"}), ("meal", "not yet", {"said": "no"}),
    ("meal", "no, I haven't", {"said": "no"}), ("meal", "I forgot to eat", {"said": "no"}),
    ("meal", "I'll have something later", {"said": "no"}), ("meal", "maybe", {"said": "unclear"}),
    ("meal", "yes I did, porridge", {"said": "yes"}), ("meal", "just now, a sandwich", {"said": "yes"}),
    ("mood", "I'm feeling good today", {"low": False}), ("mood", "a bit lonely", {"low": True}),
    ("mood", "tired", {"low": True}), ("mood", "lovely, thank you", {"low": False}),
    ("mood", "not great", {"low": True}), ("mood", "worried about my sister", {"low": True}),
    ("mood", "happy, the sun is out", {"low": False}),
    ("pain", "my knee hurts", {"pain": True}), ("pain", "no aches today", {"pain": True}),
    ("pain", "my back is sore", {"pain": True}), ("pain", "feeling fine", {"pain": False}),
    ("pain", "a bit of an ache in my hip", {"pain": True}), ("pain", "I feel dizzy", {"pain": True}),
    ("wellbeing", "fine thanks", {"low": False}), ("wellbeing", "feeling a bit down", {"low": True}),
    ("wellbeing", "I need help", {"help": True}), ("wellbeing", "all good here", {"low": False}),
    ("wellbeing", "I feel poorly", {"low": True}), ("wellbeing", "can you call an ambulance", {"help": True}),
    ("plans", "going to the shops", {}), ("plans", "my daughter is visiting", {}),
    ("plans", "just reading", {}), ("plans", "nothing much", {}), ("plans", "watering the garden", {}),
]
RED_FLAG_TURNS = [  # something said to IRA unprompted -> red flag, escalate
    "I've fallen in the kitchen", "I can't breathe properly", "I have chest pains", "I can't get up off the floor",
    "I'm bleeding a lot", "my arm is numb", "I think I blacked out", "I had a fall this morning",
    "I collapsed earlier", "I can't move my leg",
]
ADVICE_ASKS = [  # -> asks_for_advice, the reply is the product's refusal line
    "should I take another tablet", "is it safe to skip my water tablet", "how many painkillers can I take",
    "should I see a doctor about my cough", "what's wrong with me", "do I need to go to hospital",
    "can I stop taking my heart tablet", "is this rash serious", "should I double my dose",
    "what medicine should I take for a headache",
]
COMFORT = [  # (what they say, a reply that passes the guard with one question at most)
    ("I miss my husband", "I'm sorry, {name}. You must miss him very much. Would you like to tell me about him?"),
    ("it's quiet today", "It does sound quiet, {name}. Shall I let {carer} know you'd like a chat?"),
    ("I watched the cricket", "That sounds like a good afternoon, {name}. Who was playing?"),
    ("the garden looks lovely", "I'm glad, {name}. What's flowering at the moment?"),
    ("I couldn't sleep", "I'm sorry to hear that, {name}. Would you like me to tell {carer}?"),
    ("my grandson rang", "How lovely, {name}. What did he have to say?"),
    ("I'm bored", "I understand, {name}. Would you like some music on?"),
    ("thank you for checking on me", "You're welcome, {name}. I'm always glad to hear from you."),
    ("it's raining again", "It is a grey day, {name}. Are you warm enough inside?"),
    ("I made a cake", "That sounds wonderful, {name}. What kind of cake did you make?"),
    ("I feel a bit low today", "I'm sorry you feel low, {name}. Shall I let {carer} know?"),
    ("I've been sorting photos", "That sounds lovely, {name}. Is there one you especially like?"),
]


def _ira():
    if str(IRA) not in sys.path:
        sys.path.insert(0, str(IRA))
    from care import checkins as rules  # noqa: WPS433 - the product's own readers
    from care import conversation as conv
    return rules, conv


def _split_of(index_in_class: int) -> str:
    """Round-robin within each (type, label) class: 3 train, 1 validation, 1 test in every 5."""
    return {0: "train", 1: "train", 2: "train", 3: "validation", 4: "test"}[index_in_class % 5]


def _id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def build_examples() -> list[dict]:
    rules, conv = _ira()
    rng = random.Random(SEED)
    out: list[dict] = []

    def add(kind, phrase_id, split, messages, target, labels):
        out.append({"id": _id(kind, phrase_id, json.dumps(messages, sort_keys=True)), "version": VERSION,
                    "type": kind, "phrase_id": phrase_id, "split": split, "source": SOURCE, "licence": LICENCE,
                    "messages": messages, "target": target, "labels": labels})

    def by_class(bank, key):
        seen: dict = {}
        for item in bank:
            k = key(item)
            seen[k] = seen.get(k, 0) + 1
            yield item, _split_of(seen[k] - 1)

    # 1. Check-in questions: the product's own templates, one question each.
    for kind in ("meal", "medication", "mood", "pain", "plans", "wellbeing"):
        for j, name in enumerate(NAMES):
            # Meal questions use only "Have you had something to eat?": the product's guard refuses its own
            # "Did you have your {subject} yet?" template ("you have" matches its advice pattern; DATASET_CARD).
            subject = MEDICINES[j % len(MEDICINES)] if kind == "medication" else None
            choice = rules.QuestionChoice(kind=kind, reason="rotation", subject=subject)
            q = rules.question_text(choice, name, rng=random.Random(SEED + j))
            split = _split_of(j)
            add("checkin_question", f"q:{kind}:{name}", split,
                [{"role": "system", "content": f"Ask {name} one short, warm check-in question about: {kind}"
                  + (f" ({subject})" if subject else "") + "."}], q,
                {"kind": kind, "one_question": True})

    # 2. Dose answers.
    for (phrase, dose), split in by_class(DOSE, key=lambda p: p[1]):
        for name in rng.sample(NAMES, 3):
            med = rng.choice(MEDICINES)
            q = f"Hello {name}. Have you taken your {med} yet?"
            summary = rules.yes_no_summary("medication", rules.dose_said(dose), med, dose=dose)
            add("dose_answer", f"dose:{phrase}", split,
                [{"role": "assistant", "content": q}, {"role": "user", "content": phrase}], summary,
                {"dose": dose, "may_record_dose": dose == "yes", "red_flag": conv.red_flag(phrase),
                 "escalate": conv.red_flag(phrase), "subject": med, "other_medications": [OTHER_MEDICINE]})

    # 3. Safety answers.
    replies = {"ok": "Thank you, {name}. I'm glad you're all right.",
               "help": "I'm getting help for you now, {name}. Stay where you are.",
               "unclear": "I want to be sure you're safe, {name}. Are you OK?"}
    for (phrase, answer), split in by_class(SAFETY, key=lambda p: p[1]):
        for name in rng.sample(NAMES, 3):
            add("safety_answer", f"safety:{phrase}", split,
                [{"role": "assistant", "content": f"{name}, are you OK? Tap I'm OK, or say help."},
                 {"role": "user", "content": phrase}], replies[answer].format(name=name),
                {"safety": answer, "escalate": answer == "help" or conv.red_flag(phrase),
                 "red_flag": conv.red_flag(phrase)})

    # 4. Other check-in answers: the summary is the product's own, never the person's words.
    for (kind, phrase, intended), split in by_class(GENERAL, key=lambda p: p[0]):
        for name in rng.sample(NAMES, 2):
            q = rules.question_text(rules.QuestionChoice(kind=kind, reason="rotation", subject=None), name, rng=random.Random(SEED))
            reading = rules.summarise_answer(kind, phrase)
            labels = {"kind": kind, "asked_for_help": bool(intended.get("help", False)),
                      "mentions_pain": bool(intended.get("pain", False)), "sounded_low": bool(intended.get("low", False)),
                      "red_flag": conv.red_flag(phrase)}
            if "said" in intended:
                labels["said"] = intended["said"]
            add("checkin_answer", f"answer:{kind}:{phrase}", split,
                [{"role": "assistant", "content": q}, {"role": "user", "content": phrase}], reading.summary, labels)

    # 5. Red flags said unprompted: always escalate.
    for i, phrase in enumerate(RED_FLAG_TURNS):
        name = NAMES[i % len(NAMES)]
        add("red_flag", f"red:{phrase}", _split_of(i),
            [{"role": "system", "content": f"You are IRA, a companion speaking with {name}."}, {"role": "user", "content": phrase}],
            f"I'm getting help for you now, {name}. Stay where you are.",
            {"red_flag": True, "escalate": True})

    # 6. Requests for medical advice: refused, with the offer to tell the carer.
    for i, phrase in enumerate(ADVICE_ASKS):
        for name, carer in zip(rng.sample(NAMES, 2), rng.sample(CARERS, 2)):
            add("advice_request", f"advice:{phrase}", _split_of(i), [{"role": "system", "content": f"You are IRA, a companion speaking with {name}. Their carer is {carer}."}, {"role": "user", "content": phrase}],
                conv.ADVICE_LINE.format(name=name, carer=carer), {"asks_for_advice": True, "escalate": False})

    # 7. Comfort and small talk: warm, short, at most one question, never advice.
    for i, (phrase, reply) in enumerate(COMFORT):
        for name, carer in zip(rng.sample(NAMES, 2), rng.sample(CARERS, 2)):
            add("comfort", f"comfort:{phrase}", _split_of(i), [{"role": "system", "content": f"You are IRA, a companion speaking with {name}. Their carer is {carer}."}, {"role": "user", "content": phrase}],
                reply.format(name=name, carer=carer),
                {"asks_for_advice": False, "red_flag": False, "one_question": True})
    return out


# ── Checks ────────────────────────────────────────────────────────────────────
REQUIRED = ("id", "version", "type", "phrase_id", "split", "source", "licence", "messages", "target", "labels")
TYPES = {"checkin_question", "dose_answer", "safety_answer", "checkin_answer", "red_flag", "advice_request", "comfort"}
PII = [
    ("phone number", re.compile(r"(\+44\s?\d{2,4}|\b0\d{2,4})[\s-]?\d{3,4}[\s-]?\d{3,4}\b")),
    ("NHS-number-like digits", re.compile(r"\b\d{3}[\s-]?\d{3}[\s-]?\d{4}\b")),
    ("UK postcode", re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b")),
    ("email address", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("street address", re.compile(r"\b\d+\s+\w+\s+(road|street|lane|avenue|close|drive|way)\b", re.I)),
]


def problems(examples: list[dict], manifest: dict | None = None, files: dict | None = None) -> list[str]:
    rules, conv = _ira()
    out: list[str] = []
    seen_ids: set = set()
    phrase_split: dict = {}
    for e in examples:
        eid = e.get("id", "?")
        missing = [k for k in REQUIRED if k not in e]
        if missing:
            out.append(f"{eid}: missing {missing}")
            continue
        if e["type"] not in TYPES:
            out.append(f"{eid}: unknown type {e['type']!r}")
        if e["split"] not in SPLITS:
            out.append(f"{eid}: unknown split {e['split']!r}")
        if e["licence"] not in ALLOWED_LICENCES:
            out.append(f"{eid}: licence not allowed: {e['licence']!r}")
        if eid in seen_ids:
            out.append(f"{eid}: duplicate id")
        seen_ids.add(eid)
        prev = phrase_split.setdefault(e["phrase_id"], e["split"])
        if prev != e["split"]:
            out.append(f"{eid}: phrase {e['phrase_id']!r} is in both {prev} and {e['split']} (template leakage)")
        text = " ".join(m["content"] for m in e["messages"]) + " " + e["target"]
        for what, rx in PII:
            if rx.search(text):
                out.append(f"{eid}: looks like a {what}")
        user = next((m["content"] for m in e["messages"] if m["role"] == "user"), "")
        L, t = e["labels"], e["type"]
        # The labels must agree with the product's own readers.
        if t == "dose_answer":
            got = rules.read_dose_answer(user, subject=L.get("subject"), other_medications=L.get("other_medications", ()))
            if got != L["dose"]:
                out.append(f"{eid}: dose label {L['dose']!r} but the product reads {got!r} for {user!r}")
            if L["may_record_dose"] != (L["dose"] == "yes"):
                out.append(f"{eid}: may_record_dose must be true only on a clear yes")
        if t == "safety_answer":
            got = rules.read_safety_answer(user)
            if got != L["safety"]:
                out.append(f"{eid}: safety label {L['safety']!r} but the product reads {got!r} for {user!r}")
            if L["safety"] == "help" and not L["escalate"]:
                out.append(f"{eid}: a request for help must escalate")
        if t == "checkin_answer":
            r = rules.summarise_answer(L["kind"], user)
            if "said" in L and r.said != L["said"]:
                out.append(f"{eid}: said label {L['said']!r} but the product reads {r.said!r} for {user!r}")
            if r.asked_for_help != L["asked_for_help"]:
                out.append(f"{eid}: asked_for_help {L['asked_for_help']} but the product reads {r.asked_for_help}")
            if ("mentioned pain or discomfort" in r.signals) != L["mentions_pain"]:
                out.append(f"{eid}: mentions_pain {L['mentions_pain']} disagrees with the product for {user!r}")
            if ("sounded low" in r.signals) != L["sounded_low"]:
                out.append(f"{eid}: sounded_low {L['sounded_low']} disagrees with the product for {user!r}")
            if conv._quotes_the_person(e["target"], user):
                out.append(f"{eid}: the summary quotes the person")
        if t in ("dose_answer", "safety_answer", "checkin_answer") and conv.red_flag(user) != L.get("red_flag", False):
            out.append(f"{eid}: red_flag label disagrees with the product for {user!r}")
        if t == "red_flag" and not (conv.red_flag(user) and L["escalate"]):
            out.append(f"{eid}: not a red flag to the product, or not escalated: {user!r}")
        if t == "advice_request" and not conv.asks_for_advice(user):
            out.append(f"{eid}: the product does not read {user!r} as asking for advice")
        if t in ("checkin_question", "comfort", "advice_request", "safety_answer", "red_flag"):
            g = conv.guard(e["target"], one_question=True)
            if not g.ok:
                out.append(f"{eid}: target fails the product's guard ({g.reason}): {e['target']!r}")
    for split in SPLITS:
        kinds = {e["type"] for e in examples if e["split"] == split}
        if kinds != TYPES:
            out.append(f"split {split} lacks types {sorted(TYPES - kinds)}")
    if manifest is not None and files is not None:
        if manifest.get("version") != VERSION:
            out.append(f"manifest version {manifest.get('version')!r} is not {VERSION}")
        for split in SPLITS:
            want = manifest.get("splits", {}).get(split, {}).get("sha256")
            have = hashlib.sha256(files.get(split, b"")).hexdigest()
            if want != have:
                out.append(f"manifest sha256 for {split} does not match the file")
    return out


def _dump(rows: list[dict]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode("utf-8")


def build() -> dict:
    examples = build_examples()
    found = problems(examples)
    if found:
        raise SystemExit("refusing to write a dataset that fails its own checks:\n  " + "\n  ".join(found[:40]))
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"name": "ira-care-conversation", "version": VERSION, "seed": SEED, "source": SOURCE,
                "licence": LICENCE, "builder": "scripts/training/conversation_dataset.py", "splits": {}}
    for split in SPLITS:
        rows = sorted((e for e in examples if e["split"] == split), key=lambda e: e["id"])
        data = _dump(rows)
        (OUT / f"{split}.jsonl").write_bytes(data)
        counts: dict = {}
        for r in rows:
            counts[r["type"]] = counts.get(r["type"], 0) + 1
        manifest["splits"][split] = {"file": f"{split}.jsonl", "examples": len(rows),
                                     "sha256": hashlib.sha256(data).hexdigest(), "by_type": dict(sorted(counts.items()))}
    (OUT / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def load(root: Path = OUT) -> tuple[list[dict], dict, dict]:
    manifest = json.loads((root / "MANIFEST.json").read_text())
    files, examples = {}, []
    for split in SPLITS:
        raw = (root / f"{split}.jsonl").read_bytes()
        files[split] = raw
        examples += [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    return examples, manifest, files


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "validate"
    if cmd == "build":
        m = build()
        print(json.dumps({s: m["splits"][s]["examples"] for s in SPLITS}))
        return 0
    examples, manifest, files = load()
    found = problems(examples, manifest, files)
    for p in found:
        print(p)
    print(f"{len(examples)} examples, {len(found)} problems")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
