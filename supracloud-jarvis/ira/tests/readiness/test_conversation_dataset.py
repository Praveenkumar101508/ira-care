"""The care-conversation dataset (F2.d) is valid, licensed, free of personal details, and agrees
with the product's own readers. Built and checked by scripts/training/conversation_dataset.py."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("conversation_dataset", ROOT / "scripts/training/conversation_dataset.py")
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)


def _loaded():
    return cd.load()


def test_the_committed_dataset_passes_every_check():
    examples, manifest, files = _loaded()
    assert len(examples) == sum(manifest["splits"][s]["examples"] for s in cd.SPLITS)
    assert cd.problems(examples, manifest, files) == []


def test_rebuilding_gives_the_committed_files():
    """Deterministic: the builder with its seed reproduces the committed examples exactly."""
    examples, _, _ = _loaded()
    rebuilt = cd.build_examples()
    assert sorted(e["id"] for e in rebuilt) == sorted(e["id"] for e in examples)


def _one(t):
    examples, _, _ = _loaded()
    return examples, next(e for e in examples if e["type"] == t)


def test_a_dose_label_the_product_would_not_give_is_caught():
    examples, e = _one("dose_answer")
    bad = copy.deepcopy(e)
    bad["messages"][-1]["content"] = "maybe, I can't remember"
    bad["labels"].update(dose="yes", may_record_dose=True)
    found = cd.problems([x for x in examples if x is not e] + [bad])
    assert any("product reads 'unclear'" in p for p in found)


def test_a_help_answer_labelled_ok_is_caught():
    examples, e = _one("safety_answer")
    bad = copy.deepcopy(e)
    bad["messages"][-1]["content"] = "I've fallen and I can't get up"
    bad["labels"].update(safety="ok", escalate=False)
    found = cd.problems([x for x in examples if x is not e] + [bad])
    assert any("safety label 'ok'" in p for p in found)


def test_personal_details_unlicensed_data_and_leakage_are_caught():
    examples, e = _one("comfort")
    pii = copy.deepcopy(e)
    pii["id"] = "pii"
    pii["messages"][-1]["content"] = "ring me on 07700 900123 at 12 Station Road"
    lic = copy.deepcopy(e)
    lic["id"] = "lic"
    lic["licence"] = "CC BY-NC 4.0"
    leak = copy.deepcopy(e)
    leak["id"] = "leak"
    leak["split"] = "test" if e["split"] != "test" else "train"
    found = cd.problems(examples + [pii, lic, leak])
    assert any("phone number" in p for p in found)
    assert any("street address" in p for p in found)
    assert any("licence not allowed" in p for p in found)
    assert any("template leakage" in p for p in found)


def test_advice_in_a_reply_and_a_tampered_split_are_caught():
    examples, manifest, files = _loaded()
    e = next(x for x in examples if x["type"] == "comfort")
    bad = copy.deepcopy(e)
    bad["target"] = "You should take two tablets and see a doctor."
    found = cd.problems([x for x in examples if x is not e] + [bad])
    assert any("fails the product's guard" in p for p in found)
    tampered = dict(files, test=files["test"] + b'{"x": 1}\n')
    assert any("sha256 for test" in p for p in cd.problems(examples, manifest, tampered))
    assert json.loads(json.dumps(manifest))["version"] == cd.VERSION


# ── Version 1.1.0 ─────────────────────────────────────────────────────────────

def test_a_held_out_item_that_nearly_copies_a_train_item_is_caught():
    examples, _, _ = _loaded()
    train = next(e for e in examples if e["split"] == "train" and e["type"] == "dose_answer")
    copy_ = copy.deepcopy(train)
    copy_["id"], copy_["split"], copy_["phrase_id"] = "near", "test", "dose:near-copy"
    copy_["messages"][-1]["content"] = "maybe, " + train["messages"][-1]["content"]
    found = cd.problems(examples + [copy_])
    assert any(p.startswith("near: test item") and "nearly copies train item" in p for p in found)


def test_check_in_questions_one_name_apart_are_near_duplicates():
    """1.0.0 split check-in questions by name, so test held the same question as train; 1.1.0 splits by kind."""
    examples, _, _ = _loaded()
    q = next(e for e in examples if e["type"] == "checkin_question" and e["split"] == "train")
    other = copy.deepcopy(q)
    other["id"], other["split"], other["phrase_id"] = "q-copy", "test", "q:copy"
    name = q["target"].split()[1].strip(".,")
    other["target"] = q["target"].replace(name, "Walter" if name != "Walter" else "Edith")
    assert any(p.startswith("q-copy:") for p in cd.near_duplicates(examples + [other]))
    held_out_kinds = {e["labels"]["kind"] for e in examples if e["type"] == "checkin_question" and e["split"] != "train"}
    train_kinds = {e["labels"]["kind"] for e in examples if e["type"] == "checkin_question" and e["split"] == "train"}
    assert held_out_kinds and not held_out_kinds & train_kinds


def test_an_item_copying_the_product_evaluation_set_is_caught():
    examples, _, _ = _loaded()
    turns = cd._eval_turns()
    assert turns, "the product evaluation set is where the dataset expects it"
    e = next(x for x in examples if x["type"] == "comfort" and x["split"] == "train")
    bad = copy.deepcopy(e)
    bad["id"] = "evalcopy"
    bad["messages"][-1]["content"] = "Thank you for checking on me"   # talk_ordinary-14, word for word
    assert any(p.startswith("evalcopy:") and "product evaluation turn" in p for p in cd.near_duplicates([bad], turns))


def test_new_talk_types_are_checked_against_the_product():
    examples, _, _ = _loaded()
    sos = next(e for e in examples if e["type"] == "sos_cancel")
    cancels = copy.deepcopy(sos)
    cancels["target"] = "I've cancelled the alert. You're safe now."
    mem = next(e for e in examples if e["type"] == "memory" and e["labels"].get("include") == ["porridge"])
    forgets = copy.deepcopy(mem)
    forgets["target"] = "You didn't have breakfast today."
    ruled = copy.deepcopy(next(e for e in examples if e["type"] == "privacy"))
    ruled["messages"][-1]["content"] = "I've fallen and I can't get up"
    rest = [x for x in examples if x is not sos and x is not mem]
    found = cd.problems(rest + [cancels, forgets, ruled])
    assert any("cancels an alert or declares the person safe" in p for p in found)
    assert any("memory reply does not carry ['porridge']" in p for p in found)
    assert any("the product's rules answer" in p for p in found)


def test_every_example_renders_to_the_products_own_prompt():
    """The fine-tune is trained and judged on the prompts the product sends (care/conversation.py)."""
    import sys
    sys.path.insert(0, str(ROOT / "supracloud-jarvis" / "ira"))
    from care.conversation import StructuredAnswer
    examples, _, _ = _loaded()
    tasks = {}
    for e in examples:
        task, messages, target = cd.product_prompt(e)
        tasks[task] = tasks.get(task, 0) + 1
        assert messages[0]["role"] == "system" and messages[-1]["role"] == "user" and target
        if task == "answer":
            s = StructuredAnswer.model_validate(json.loads(target))
            if e["type"] == "dose_answer":
                assert (s.medication_taken == "yes") == (e["labels"]["dose"] == "yes")
            if e["labels"].get("escalate") or e["labels"].get("asked_for_help") or e["labels"].get("safety") == "help":
                assert s.needs_help
            if e["labels"].get("safety") in ("help", "unclear"):
                assert s.wellbeing != "good"
    assert set(tasks) == {"answer", "talk", "checkin"}
