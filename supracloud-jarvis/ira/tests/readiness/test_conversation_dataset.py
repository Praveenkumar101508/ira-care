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
