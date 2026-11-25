#!/usr/bin/env python3
"""Completion of the owner's master directive, measured from docs/progress/requirements.json.

    python3 scripts/completion_summary.py                 # check the rules, print the measures
    python3 scripts/completion_summary.py --json          # the same, as JSON
    python3 scripts/completion_summary.py --snapshot LABEL
        # append a dated record (commit, date, the seven measures) to completion_history.json
    python3 scripts/completion_summary.py --at REV --snapshot LABEL
        # append a recomputed lower bound for an earlier commit (see `at_commit` below)

The requirements are a fixed, versioned list of atomic deliverables. Each has one state:

    not_started       nothing merged
    implemented       code or a document merged
    accepted          functional evidence: unit_test, db_test, build, browser, emulator, model_eval or device
    verified          integration-level evidence: db_test, browser, emulator, model_eval or device
    device_validated  device evidence
    blocked_external  cannot progress without something outside the software (blocked_by says what)

Rules enforced here (exit 1 when any is broken):
  * docs_only and config_check never count as acceptance of a working application, model or OS;
  * the evidence that earns accepted, verified or device_validated must name a file, not only a PR;
  * every file ref exists (a `path::name` ref also needs `name` in that file); a PR ref is #44 or later;
  * blocked_by is given exactly when the state is blocked_external;
  * nothing leaves a denominator: blocked and not-started items count in every measure they belong to.

The seven measures (numerator / denominator), never added together:
  1. software implementation   area software, ai or os, hardware-independent: implemented or better
  2. software acceptance       same denominator: accepted or better
  3. software verification     area software, ai or os (all): verified or better
  4. AI development            area ai: accepted or better
  5. OS development            area os, hardware-independent: accepted or better
  6. hardware integration      area hardware: accepted or better
  7. external readiness        area external: accepted or better

The history file is append-only: one entry per line, each carrying the hash of the one before,
so an edit to an earlier entry breaks the chain (tests/readiness/test_requirements_framework.py).
The register quotes a snapshot by label and commit, never a live number, so two pull requests
that both change requirements cannot leave it stale.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "docs" / "progress" / "requirements.json"
HISTORY = ROOT / "docs" / "progress" / "completion_history.json"

AREAS = ("software", "ai", "os", "hardware", "external")
STATES = ("not_started", "implemented", "accepted", "verified", "device_validated", "blocked_external")
KINDS = ("unit_test", "db_test", "build", "browser", "emulator", "model_eval", "device", "docs_only", "config_check")

ACCEPTANCE_KINDS = frozenset({"unit_test", "db_test", "build", "browser", "emulator", "model_eval", "device"})
VERIFICATION_KINDS = frozenset({"db_test", "browser", "emulator", "model_eval", "device"})
DEVICE_KINDS = frozenset({"device"})
NEEDS = {"accepted": ACCEPTANCE_KINDS, "verified": VERIFICATION_KINDS, "device_validated": DEVICE_KINDS}

IMPLEMENTED = frozenset({"implemented", "accepted", "verified", "device_validated"})
ACCEPTED = frozenset({"accepted", "verified", "device_validated"})
VERIFIED = frozenset({"verified", "device_validated"})
SOFTWARE = frozenset({"software", "ai", "os"})
MIN_PR = 44

ID = re.compile(r"^[A-L][A-Za-z0-9\-–]*\.[a-z]{1,2}$")
PR_REF = re.compile(r"^#(\d+)$")

MEASURES = (
    ("software_implementation", "Hardware-independent software implementation",
     lambda r: r["area"] in SOFTWARE and r["hardware_independent"], IMPLEMENTED),
    ("software_acceptance", "Hardware-independent software acceptance",
     lambda r: r["area"] in SOFTWARE and r["hardware_independent"], ACCEPTED),
    ("software_verification", "Software verification",
     lambda r: r["area"] in SOFTWARE, VERIFIED),
    ("ai_development", "AI development (accepted, area ai)",
     lambda r: r["area"] == "ai", ACCEPTED),
    ("os_development", "OS development (accepted, area os, hardware-independent)",
     lambda r: r["area"] == "os" and r["hardware_independent"], ACCEPTED),
    ("hardware_integration", "Hardware integration (accepted, area hardware)",
     lambda r: r["area"] == "hardware", ACCEPTED),
    ("external_readiness", "External and commercial readiness (accepted, area external)",
     lambda r: r["area"] == "external", ACCEPTED),
)


# ---------------------------------------------------------------- rules

def ref_problem(ref: str, root: Path) -> str | None:
    """None when the ref is a PR from #44 on, or a file (and symbol) that exists under root."""
    m = PR_REF.match(ref)
    if m:
        return None if int(m.group(1)) >= MIN_PR else f"PR ref {ref} is before #{MIN_PR}"
    if ref.startswith("#"):
        return f"malformed PR ref {ref!r}"
    path, _, symbol = ref.partition("::")
    f = root / path
    if not path or not f.is_file():
        return f"file ref {path!r} does not exist"
    if symbol and symbol not in f.read_text(encoding="utf-8", errors="replace"):
        return f"{symbol!r} is not in {path}"
    return None


def item_problems(r: dict, root: Path) -> list[str]:
    rid = r.get("id", "?")
    out: list[str] = []
    for key, typ in (("id", str), ("parent", str), ("title", str), ("area", str),
                     ("hardware_independent", bool), ("state", str), ("evidence", list)):
        if not isinstance(r.get(key), typ):
            out.append(f"{rid}: '{key}' missing or not {typ.__name__}")
    if out:
        return out
    if not ID.match(rid):
        out.append(f"{rid}: id is not of the form <register row>.<letter>")
    elif not rid.startswith(r["parent"] + "."):
        out.append(f"{rid}: id does not start with its parent {r['parent']!r}")
    if r["area"] not in AREAS:
        out.append(f"{rid}: area {r['area']!r} is not one of {AREAS}")
    state = r["state"]
    if state not in STATES:
        out.append(f"{rid}: state {state!r} is not one of {STATES}")
    blocked_by = r.get("blocked_by")
    if state == "blocked_external" and not (isinstance(blocked_by, str) and blocked_by.strip()):
        out.append(f"{rid}: blocked_external needs blocked_by")
    if state != "blocked_external" and blocked_by is not None:
        out.append(f"{rid}: blocked_by is given but the state is {state}")
    good_files: set[str] = set()
    for e in r["evidence"]:
        if not isinstance(e, dict) or set(e) != {"kind", "ref"}:
            out.append(f"{rid}: evidence entry {e!r} is not {{kind, ref}}")
            continue
        if e["kind"] not in KINDS:
            out.append(f"{rid}: evidence kind {e['kind']!r} is not one of {KINDS}")
        problem = ref_problem(str(e["ref"]), root)
        if problem:
            out.append(f"{rid}: {problem}")
        elif not PR_REF.match(e["ref"]):
            good_files.add(e["kind"])
    if state == "implemented" and not r["evidence"]:
        out.append(f"{rid}: implemented needs at least one evidence ref")
    if state in NEEDS and not (good_files & NEEDS[state]):
        out.append(f"{rid}: {state} needs evidence of kind {sorted(NEEDS[state])} naming a file")
    return out


def validate(doc: dict, root: Path = ROOT) -> list[str]:
    out: list[str] = []
    if not isinstance(doc.get("version"), str) or not doc["version"]:
        out.append("requirements: 'version' missing")
    if not isinstance(doc.get("methodology"), str) or not doc["methodology"]:
        out.append("requirements: 'methodology' missing")
    reqs = doc.get("requirements")
    if not isinstance(reqs, list) or not reqs:
        return out + ["requirements: 'requirements' must be a non-empty list"]
    seen: set[str] = set()
    for r in reqs:
        if not isinstance(r, dict):
            out.append(f"requirement {r!r} is not an object")
            continue
        if r.get("id") in seen:
            out.append(f"{r.get('id')}: duplicate id")
        seen.add(r.get("id"))
        out += item_problems(r, root)
    return out


# ---------------------------------------------------------------- measures

def _ratio(num: int, den: int) -> dict:
    return {"numerator": num, "denominator": den, "percent": round(100 * num / den, 1) if den else None}


def measures(reqs: list[dict]) -> dict:
    out = {}
    for key, _label, member, done in MEASURES:
        pool = [r for r in reqs if member(r)]
        out[key] = _ratio(sum(r["state"] in done for r in pool), len(pool))
    return out


def summary(doc: dict) -> dict:
    reqs = doc["requirements"]
    completed = sum(r["state"] in ACCEPTED for r in reqs)
    blocked = [r for r in reqs if r["state"] == "blocked_external"]
    return {
        "requirements_version": doc["version"],
        "measures": measures(reqs),
        "requirements": len(reqs),
        "completed": completed,
        "remaining": len(reqs) - completed,
        "blocked_external": len(blocked),
        "by_state": {s: sum(r["state"] == s for r in reqs) for s in STATES},
        "by_area": {a: sum(r["area"] == a for r in reqs) for a in AREAS},
        "blocked": [{"id": r["id"], "title": r["title"], "blocked_by": r["blocked_by"]} for r in blocked],
    }


# ---------------------------------------------------------------- history

def entry_hash(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def read_history(path: Path = HISTORY) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def chain_problems(history: list[dict]) -> list[str]:
    out, prev = [], None
    for n, e in enumerate(history, 1):
        if e.get("seq") != n:
            out.append(f"history entry {n}: seq is {e.get('seq')!r}")
        if e.get("prev_hash") != prev:
            out.append(f"history entry {n}: prev_hash does not match entry {n - 1}")
        if e.get("hash") != entry_hash(e):
            out.append(f"history entry {n}: hash does not match its content (edited after it was written?)")
        prev = e.get("hash")
    return out


def render_history(history: list[dict]) -> str:
    lines = [json.dumps(e, sort_keys=True, ensure_ascii=False) for e in history]
    return "[\n" + ",\n".join(lines) + "\n]\n"


def append(record: dict, path: Path = HISTORY) -> dict:
    """Append one entry; earlier entries are kept byte for byte (the file is only ever extended)."""
    history = read_history(path)
    problems = chain_problems(history)
    if problems:
        raise ValueError("history is not intact: " + "; ".join(problems))
    entry = dict(record, seq=len(history) + 1, prev_hash=history[-1]["hash"] if history else None)
    entry["hash"] = entry_hash(entry)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    new = render_history(history + [entry])
    if old and not new.startswith(old[: -len("\n]\n")]):
        raise ValueError("refusing to write: an earlier entry would change")
    path.write_text(new, encoding="utf-8")
    return entry


# ---------------------------------------------------------------- git

def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout.strip()


def _unchanged_since(rev: str, path: str) -> bool:
    try:
        _git("cat-file", "-e", f"{rev}:{path}")
    except subprocess.CalledProcessError:
        return False
    return subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", rev, "--", path]).returncode == 0


LOWER_BOUND_METHOD = (
    "Same requirement list. An item keeps its present state only if every evidence ref is a file that "
    "existed at this commit with exactly today's content; a PR ref, a missing file or a changed file drops "
    "it to not_started. A lower bound: earlier work whose evidence was later edited is not credited. "
    "Assumes the suites passed at this commit, as CI on main required."
)


def at_commit(doc: dict, rev: str) -> dict:
    """The same requirement list, with each item credited at `rev` only if its evidence was there verbatim.

    An item keeps its present state only when every evidence ref is a file that existed at `rev`
    with exactly today's content. A PR ref (#44 or later) did not exist then, and a file that has
    changed since may not have supported the claim then, so either drops the item to not_started.
    Blocked items stay blocked. This is a lower bound: work done before `rev` whose evidence was
    later edited is not credited. It assumes the suites passed at `rev`, as CI on main required.
    """
    reqs = []
    for r in doc["requirements"]:
        r = dict(r)
        if r["state"] in IMPLEMENTED:
            refs = [e["ref"] for e in r["evidence"]]
            if not refs or any(PR_REF.match(x) or not _unchanged_since(rev, x.partition("::")[0]) for x in refs):
                r["state"] = "not_started"
        reqs.append(r)
    return dict(doc, requirements=reqs)


# ---------------------------------------------------------------- output

def text_report(s: dict) -> str:
    lines = [f"Requirements version {s['requirements_version']}: {s['requirements']} atomic requirements", ""]
    for key, label, _m, _d in MEASURES:
        m = s["measures"][key]
        pct = "n/a" if m["percent"] is None else f"{m['percent']} %"
        lines.append(f"  {label:<62} {m['numerator']:>3} / {m['denominator']:<3}  {pct}")
    lines += ["",
              f"  Completed (accepted or better): {s['completed']}; remaining: {s['remaining']}",
              f"  Blocked by something external: {s['blocked_external']}",
              "  By state: " + ", ".join(f"{k} {v}" for k, v in s["by_state"].items()),
              "  By area: " + ", ".join(f"{k} {v}" for k, v in s["by_area"].items()),
              "", "Blocked items:"]
    lines += [f"  {b['id']:<8} {b['title']} — {b['blocked_by']}" for b in s["blocked"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="print the measures as JSON")
    ap.add_argument("--snapshot", metavar="LABEL", help="append a dated record to completion_history.json")
    ap.add_argument("--at", metavar="REV", help="recompute a lower bound for an earlier commit")
    ap.add_argument("--note", metavar="TEXT", help="a caveat stored with the snapshot")
    args = ap.parse_args(argv)

    doc = json.loads(REQUIREMENTS.read_text(encoding="utf-8"))
    problems = validate(doc) + chain_problems(read_history())
    if problems:
        print("\n".join(problems), file=sys.stderr)
        print(f"{len(problems)} problem(s)", file=sys.stderr)
        return 1

    scored = at_commit(doc, args.at) if args.at else doc
    s = summary(scored)
    print(json.dumps(s, indent=1, ensure_ascii=False) if args.json else text_report(s))

    if args.snapshot:
        rev = args.at or "HEAD"
        record = {
            "label": args.snapshot,
            "recorded": _dt.date.today().isoformat(),
            "commit": _git("rev-parse", rev),
            "commit_date": _git("show", "-s", "--format=%cs", rev),
            "requirements_version": doc["version"],
            "requirements_sha256": hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest(),
            "kind": "recomputed_lower_bound" if args.at else "computed",
            "method": LOWER_BOUND_METHOD if args.at else "requirements.json scored by scripts/completion_summary.py",
            "measures": s["measures"],
            "completed": s["completed"],
            "remaining": s["remaining"],
            "blocked_external": s["blocked_external"],
            "requirements": s["requirements"],
        }
        if args.note:
            record["note"] = args.note
        entry = append(record)
        print(f"\nappended history entry {entry['seq']}: {entry['label']} at {entry['commit'][:7]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
