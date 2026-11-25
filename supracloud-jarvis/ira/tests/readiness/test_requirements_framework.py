"""Completion is measured from a fixed list of atomic requirements (docs/progress/requirements.json).

The rules live in scripts/completion_summary.py; these tests hold the real list to them, prove
each rule refuses what it should (negative controls), check the seven measures on a small
fixture whose numbers are worked out by hand, keep the history append-only, and keep the
register's quoted numbers tied to a dated snapshot rather than to a live computation.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("completion_summary", ROOT / "scripts" / "completion_summary.py")
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)

REGISTER = ROOT / "docs" / "progress" / "COMPLETION_REGISTER.md"

# The first entries of the history, pinned: editing one changes its hash, and fixing the hash
# would need this test changed too, which a reviewer sees.
PINNED_HASHES = {
    1: "9d824667b03d6ee22fcad4ecd86918207fc310c3fc3730e6300f7ade273c791d",
    2: "bbd3292dca5cd8bfafc836032f6fb786d9cad46e3ebeb64097b11886d6d6fa9a",
}


def _doc() -> dict:
    return json.loads(cs.REQUIREMENTS.read_text(encoding="utf-8"))


def _item(**kw) -> dict:
    base = {"id": "C-1.a", "parent": "C-1", "title": "t", "area": "software",
            "hardware_independent": True, "state": "not_started", "evidence": []}
    base.update(kw)
    return base


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_named_case():\n    pass\n")
    (tmp_path / "docs.md").write_text("a plan\n")
    return tmp_path


# ---------------------------------------------------------------- the real list

def test_the_requirement_list_obeys_every_rule():
    problems = cs.validate(_doc())
    assert problems == [], "\n".join(problems)


def test_the_list_is_versioned_explained_and_atomic():
    doc = _doc()
    assert re.fullmatch(r"\d+\.\d+\.\d+", doc["version"])
    assert "docs_only" in doc["methodology"] and "denominator" in doc["methodology"]
    assert 120 <= len(doc["requirements"]) <= 260
    for area in cs.AREAS:
        assert any(r["area"] == area for r in doc["requirements"]), f"no {area} requirement"


def test_no_requirement_is_device_validated_without_a_device_run():
    # Nothing in this repository has run on a physical device yet.
    assert not [r["id"] for r in _doc()["requirements"] if r["state"] == "device_validated"]


def _register_row_ids() -> set[str]:
    ids, in_section = set(), False
    for line in REGISTER.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_section = bool(re.match(r"## [C-L](?:, [A-L])*\.", line))
            continue
        m = re.match(r"^\| ([A-L][-\w–]*) \|", line)
        if in_section and m and m.group(1) not in {"PR"}:
            ids.add(m.group(1))
    return ids


def test_every_register_row_has_at_least_one_atomic_requirement():
    rows = _register_row_ids()
    assert len(rows) >= 50
    parents = {r["parent"] for r in _doc()["requirements"]}
    assert sorted(rows - parents) == [], "register rows with no atomic requirement"
    assert sorted(parents - rows) == [], "requirements whose parent is not a register row"


def test_the_directives_scope_is_in_the_denominator_even_where_nothing_is_done():
    titles = " ".join(r["title"].lower() for r in _doc()["requirements"])
    for phrase in ("ios build", "android native build", "care plans", "staff roles", "promotion gate",
                   "speech-recognition accuracy", "activity model", "cuttlefish image built",
                   "cuttlefish image boots", "device-owner policy", "ota", "rollback", "provisioning",
                   "simulated sensors hal driver", "docker images", "staging", "load test", "backup",
                   "security review", "22-step care-journey", "evidence pack"):
        assert phrase in titles, phrase


# ---------------------------------------------------------------- the rules (negative controls)

def test_docs_only_is_never_acceptance(root):
    bad = _item(state="accepted", evidence=[{"kind": "docs_only", "ref": "docs.md"}])
    assert any("accepted needs" in p for p in cs.item_problems(bad, root))
    bad["evidence"] = [{"kind": "config_check", "ref": "docs.md"}]
    assert any("accepted needs" in p for p in cs.item_problems(bad, root))
    good = _item(state="accepted", evidence=[{"kind": "unit_test", "ref": "tests/test_x.py"}])
    assert cs.item_problems(good, root) == []


def test_a_missing_file_is_refused(root):
    bad = _item(state="accepted", evidence=[{"kind": "unit_test", "ref": "tests/test_gone.py"}])
    problems = cs.item_problems(bad, root)
    assert any("does not exist" in p for p in problems)
    assert any("accepted needs" in p for p in problems), "a missing file earns nothing"


def test_a_named_test_must_be_in_the_file(root):
    ok = _item(state="accepted", evidence=[{"kind": "unit_test", "ref": "tests/test_x.py::test_named_case"}])
    assert cs.item_problems(ok, root) == []
    bad = _item(state="accepted", evidence=[{"kind": "unit_test", "ref": "tests/test_x.py::test_invented"}])
    assert any("is not in" in p for p in cs.item_problems(bad, root))


def test_pr_refs_start_at_44_and_never_carry_acceptance_alone(root):
    old = _item(state="implemented", evidence=[{"kind": "unit_test", "ref": "#43"}])
    assert any("before #44" in p for p in cs.item_problems(old, root))
    assert cs.item_problems(_item(state="implemented", evidence=[{"kind": "unit_test", "ref": "#44"}]), root) == []
    pr_only = _item(state="accepted", evidence=[{"kind": "unit_test", "ref": "#44"}])
    assert any("accepted needs" in p for p in cs.item_problems(pr_only, root))


def test_verified_needs_integration_level_evidence(root):
    unit = _item(state="verified", evidence=[{"kind": "unit_test", "ref": "tests/test_x.py"},
                                             {"kind": "build", "ref": "tests/test_x.py"}])
    assert any("verified needs" in p for p in cs.item_problems(unit, root))
    for kind in ("db_test", "browser", "emulator", "model_eval", "device"):
        ok = _item(state="verified", evidence=[{"kind": kind, "ref": "tests/test_x.py"}])
        assert cs.item_problems(ok, root) == [], kind


def test_device_validated_needs_a_device(root):
    bad = _item(state="device_validated", evidence=[{"kind": "emulator", "ref": "tests/test_x.py"}])
    assert any("device_validated needs" in p for p in cs.item_problems(bad, root))


def test_blocked_needs_a_reason_and_only_blocked_has_one(root):
    assert any("needs blocked_by" in p for p in cs.item_problems(_item(state="blocked_external"), root))
    assert any("blocked_by is given" in p for p in cs.item_problems(_item(blocked_by="x"), root))
    assert cs.item_problems(_item(state="blocked_external", blocked_by="a watch"), root) == []


def test_schema_errors_are_named(root):
    assert cs.item_problems(_item(area="firmware"), root)
    assert cs.item_problems(_item(state="done"), root)
    assert cs.item_problems(_item(evidence=[{"kind": "screenshot", "ref": "docs.md"}]), root)
    assert cs.item_problems(_item(id="C-2.a"), root), "id must start with its parent"
    assert cs.item_problems(_item(hardware_independent="yes"), root)
    doc = {"version": "1.0.0", "methodology": "m", "requirements": [_item(), _item()]}
    assert any("duplicate id" in p for p in cs.validate(doc, root))


def test_the_real_list_fails_when_an_item_is_inflated():
    doc = _doc()
    first_docs_only = next(r for r in doc["requirements"]
                           if r["evidence"] and all(e["kind"] == "docs_only" for e in r["evidence"]))
    tampered = copy.deepcopy(doc)
    for r in tampered["requirements"]:
        if r["id"] == first_docs_only["id"]:
            r["state"] = "accepted"
    assert cs.validate(tampered)


# ---------------------------------------------------------------- the seven measures

def _fixture_items() -> list[dict]:
    def mk(id, area, hwi, state):
        return {"id": id, "area": area, "hardware_independent": hwi, "state": state}

    return [
        mk("s1", "software", True, "verified"),
        mk("s2", "software", True, "accepted"),
        mk("s3", "software", True, "implemented"),
        mk("s4", "software", True, "not_started"),
        mk("s5", "software", True, "blocked_external"),
        mk("s6", "software", False, "blocked_external"),
        mk("a1", "ai", True, "verified"),
        mk("a2", "ai", True, "implemented"),
        mk("a3", "ai", False, "blocked_external"),
        mk("o1", "os", True, "accepted"),
        mk("o2", "os", True, "implemented"),
        mk("o3", "os", True, "blocked_external"),
        mk("h1", "hardware", False, "blocked_external"),
        mk("h2", "hardware", True, "implemented"),
        mk("e1", "external", True, "implemented"),
        mk("e2", "external", True, "blocked_external"),
    ]


def test_the_seven_measures_on_a_hand_checked_fixture():
    m = cs.measures(_fixture_items())
    # 1, 2: hardware-independent software/ai/os = s1-s5, a1, a2, o1-o3 = 10 items.
    #    implemented or better: s1 s2 s3 a1 a2 o1 o2 = 7; accepted or better: s1 s2 a1 o1 = 4.
    assert (m["software_implementation"]["numerator"], m["software_implementation"]["denominator"]) == (7, 10)
    assert (m["software_acceptance"]["numerator"], m["software_acceptance"]["denominator"]) == (4, 10)
    # 3: all software/ai/os = those 10 plus s6 and a3 = 12; verified: s1, a1 = 2.
    assert (m["software_verification"]["numerator"], m["software_verification"]["denominator"]) == (2, 12)
    # 4: ai = a1 a2 a3; accepted: a1.
    assert (m["ai_development"]["numerator"], m["ai_development"]["denominator"]) == (1, 3)
    # 5: os, hardware-independent = o1 o2 o3; accepted: o1.
    assert (m["os_development"]["numerator"], m["os_development"]["denominator"]) == (1, 3)
    # 6: hardware = h1 h2; none accepted.  7: external = e1 e2; none accepted.
    assert (m["hardware_integration"]["numerator"], m["hardware_integration"]["denominator"]) == (0, 2)
    assert (m["external_readiness"]["numerator"], m["external_readiness"]["denominator"]) == (0, 2)
    assert m["software_implementation"]["percent"] == 70.0
    assert m["software_verification"]["percent"] == 16.7


def test_blocked_items_stay_in_every_denominator():
    items = _fixture_items()
    without_blocked = [r for r in items if r["state"] != "blocked_external"]
    a, b = cs.measures(items), cs.measures(without_blocked)
    assert b["software_acceptance"]["percent"] > a["software_acceptance"]["percent"], \
        "dropping blocked items would raise the figure, which is why they are kept"
    doc = {"version": "0", "requirements": [dict(r, title="t", blocked_by="x") if r["state"] == "blocked_external"
                                            else dict(r, title="t") for r in items]}
    s = cs.summary(doc)
    assert s["requirements"] == 16 and s["blocked_external"] == 6 and len(s["blocked"]) == 6
    assert s["completed"] == 4 and s["remaining"] == 12


# ---------------------------------------------------------------- history

def test_history_chain_is_intact_and_its_first_entries_are_pinned():
    history = cs.read_history()
    assert cs.chain_problems(history) == []
    assert len(history) >= len(PINNED_HASHES)
    for seq, h in PINNED_HASHES.items():
        assert history[seq - 1]["hash"] == h, f"history entry {seq} was edited"


def test_editing_an_earlier_entry_is_noticed():
    history = copy.deepcopy(cs.read_history())
    history[0]["measures"]["software_acceptance"]["numerator"] += 1
    assert cs.chain_problems(history)
    history = copy.deepcopy(cs.read_history())
    history[0]["hash"] = cs.entry_hash(dict(history[0], label="rewritten"))
    assert cs.chain_problems(history)


def test_a_snapshot_only_appends(tmp_path):
    path = tmp_path / "history.json"
    path.write_text(cs.HISTORY.read_text(encoding="utf-8"), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    entry = cs.append({"label": "test", "measures": {}}, path)
    after = path.read_text(encoding="utf-8")
    assert after.startswith(before[: -len("\n]\n")]), "earlier entries changed"
    assert entry["seq"] == len(cs.read_history()) + 1
    assert cs.chain_problems(cs.read_history(path)) == []


def test_a_broken_history_refuses_a_new_snapshot(tmp_path):
    history = copy.deepcopy(cs.read_history())
    history[0]["label"] = "edited"
    path = tmp_path / "history.json"
    path.write_text(cs.render_history(history), encoding="utf-8")
    with pytest.raises(ValueError):
        cs.append({"label": "next"}, path)


def test_every_entry_says_how_it_was_made():
    for e in cs.read_history():
        assert e["kind"] in {"computed", "recomputed_lower_bound", "not_recomputable"}
        assert re.fullmatch(r"[0-9a-f]{40}", e["commit"]) and e["recorded"] and e["method"]
        if e["kind"] == "not_recomputable":
            assert e["measures"] is None and e["reason"]
        else:
            assert set(e["measures"]) == {k for k, *_ in cs.MEASURES}


# ---------------------------------------------------------------- the register

def test_the_register_has_no_live_numbers_and_quotes_a_dated_snapshot():
    text = REGISTER.read_text(encoding="utf-8")
    assert "completion-summary:begin" not in text
    m = re.search(r"At snapshot \*\*(.+?)\*\*, `([0-9a-f]{7,40})`", text)
    assert m, "the register names the snapshot it quotes"
    label, commit = m.groups()
    entry = next((e for e in cs.read_history() if e["label"] == label), None)
    assert entry and entry["commit"].startswith(commit)
    quoted = re.findall(r"^\| (.+?) \| (\d+) / (\d+) \| ([\d.]+) % \|$", text, re.M)
    assert len(quoted) == len(cs.MEASURES)
    labels = {lab: key for key, lab, *_ in cs.MEASURES}
    for name, num, den, pct in quoted:
        got = entry["measures"][labels[name]]
        assert (got["numerator"], got["denominator"], got["percent"]) == (int(num), int(den), float(pct)), name
