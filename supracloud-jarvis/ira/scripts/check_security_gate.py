#!/usr/bin/env python3
"""P16-02 — SEC-04: Bandit, gated, with the suppressions made visible.

── Why a baseline at all, and why this one ─────────────────────────────────

Bandit reports **over 150** results on this tree. Gating on zero would mean
either fixing them all in one batch or turning the gate off, and a gate that is
turned off is worse than no gate: the status row claiming it exists stays green.

So this gates at the threshold the Phase 1 baseline used for its own headline —
**severity ≥ MEDIUM and confidence ≥ MEDIUM**, which is 15 findings — and fails
on anything NEW at that threshold. Everything below it is **counted and printed
on every run**, never hidden, because "N findings we are not gating" is a
different sentence from silence and the reader is entitled to the first one.

The exact totals are printed rather than written here. P16-01 retyped its
figures from a console listing and got two of them wrong; a number in a
docstring is a number nobody re-runs.

── A suppression file is a decision ────────────────────────────────────────

`.bandit-baseline.json` is not a list of things that are fine. It is a list of
things that were **already there when the gate went in**, each of which
P16-05 must classify by execution. `REASONS` below states, per Bandit test, why
the class is carried rather than fixed today — grouped by test rather than by
line, because the reason is a property of the construct, not of the line number.

P16-05 has now done that, and it corrected this file. The **B608** note here
used to say *"four of the six are inside `core/privacy/` … the other two
(`owner_profile.py`, `tasks/manager.py`) are NOT behind RLS"*. Reading the
connection at each site rather than the directory, the split is four and two the
other way round: `tasks/manager.py` runs inside `workspace_connection` and **is**
RLS-bound, while `erasure.py:730` runs on a raw `acquire()` and is **not**. Only
`owner_profile.py` was on the right side of the original sentence.

The directory was a proxy for the boundary, and a proxy is what this programme
keeps catching. `REASONS` below now states what each construct does.

── A carried finding is its code, not its line number ──────────────────────

Each baseline entry records the source line Bandit flagged (`code`), and a
finding is carried when its test, its file and that line's text match an entry.
Until 23 October entries were matched by line NUMBER, so a pull request that
added one line anywhere above a carried finding failed this gate with the same
finding reported as "NEW" (#129: three carried lines, moved by its own edits).
Matching counts identical lines, so a second copy of a carried line is new, and
a carried line whose text changes is new: an edited flagged line deserves a
fresh look. Layout between tokens does not count; every character inside a
string literal or a comment does, so an edited literal is new even when only
its spacing changed. `line` stays in each entry as where it was last seen.

── What this gate does NOT do ──────────────────────────────────────────────

It does not prove the 15 are safe. It proves that **number 16 cannot arrive
unnoticed**. Those are different claims and only the second one is being made.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tokenize
from collections import Counter
from pathlib import Path

IRA_ROOT = Path(__file__).resolve().parents[1]
BASELINE = IRA_ROOT / ".bandit-baseline.json"

GATED_SEVERITY = {"MEDIUM", "HIGH"}
GATED_CONFIDENCE = {"MEDIUM", "HIGH"}

#: Why each carried class is carried. A test id absent from here is NOT
#: allowed into the baseline — see `_check_reasons`, which fails on one.
REASONS = {
    "B608": ("SQL built by string formatting — CLASSIFIED BY EXECUTION, "
             "P16-05. All six interpolate an IDENTIFIER and parameterise every "
             "VALUE: `erasure.py:781` iterates a literal three-table tuple; "
             "`erasure.py:817` takes its table from `SUBJECT_TABLES`, a module "
             "constant; `store.py:161` interpolates one of two literals "
             "(`NOW()` / `deleted_at`); `store.py:211` joins fragments written "
             "in the function itself and the number of its last placeholder; `owner_profile.py:106` and "
             "`tasks/manager.py:121` build a SET clause from column names "
             "already filtered through an allowlist (`FIELDS`/`BOOL_FIELDS`, "
             "and `allowed`). No caller-supplied string reaches an identifier "
             "position. FOUR run inside `workspace_connection`/"
             "`tenant_connection` and are RLS-bound: `store.py` x2, "
             "`tasks/manager.py`, `erasure.py:817`. TWO run on a raw "
             "`acquire()`: `erasure.py:781` (the LangGraph checkpoint tables, "
             "which have no tenant column — the function's own comments say "
             "so, and it scopes by thread-id prefixes, passed as a parameter "
             "array with LIKE metacharacters escaped) and `owner_profile.py:106` (the singleton owner row, "
             "`WHERE id IS TRUE`). Carried, not cleared: the constructs are "
             "safe as written and one allowlist edit away from not being, "
             "which is why they stay visible rather than being suppressed."),
    "B108": ("A hard-coded /tmp path — CLASSIFIED BY EXECUTION, P16-05, and "
             "the four are not one finding. TWO are hardening directives that "
             "Bandit is reading backwards: `docker_manager.py:231` "
             "(`--tmpfs /tmp:rw,noexec,nosuid`) and `executors.py:148` "
             "(bwrap `--tmpfs /tmp`) REPLACE the shared /tmp with a private "
             "one, so 'fixing' either would remove a control. ONE is an "
             "allow-root for path checking, not a temp file: "
             "`cmd_safety.py:31`, the shared validator's default; the chat "
             "executor passes its own private directory instead. The fourth, "
             "`playbooks.py`, WAS real and is FIXED here: "
             "the forensic snapshot directory is now created 0o700 and "
             "verified (not a symlink, owned by us, not group/world-writable) "
             "and the file is opened O_EXCL|O_NOFOLLOW. The line stays flagged "
             "because the constant is still `/tmp/ira-logs`; what changed is "
             "that nothing is assumed about what sits there. THREE more were "
             "carried until 23 October 2026 and are gone: the chat executor's "
             "working directory and `HOME` (`executor.py`) and the Action "
             "Guard's dry-run preview of it (`action_guard.py`). The executor "
             "now runs in a private 0700 directory, not /tmp."),
    "B314": ("`xml.etree` parsing on the Android actuator path — MEASURED, "
             "P16-05, and NOT reachable. This interpreter expands internal "
             "entities but refuses external ones, so the exposure is "
             "denial-of-service, not disclosure. `parse_ui_xml` extracts "
             "`<hierarchy>…</hierarchy>` with a regex BEFORE parsing, which "
             "discards the DOCTYPE where entity declarations live; a reference "
             "inside the element then names an undefined entity and parsing "
             "fails closed to `[]`. That regex is not a security control — it "
             "exists to find the element in adb output — so the property is "
             "pinned by `tests/test_p1605_uiautomator_entities.py`, whose "
             "negative control requires the naive parse to expand the bomb."),
    "B310": ("THREE sites. (1) `scripts/_net.py` — the ONE place the supply-chain gates reach the network. P16-R3 routed four separate `urlopen` calls (OSV, PyPI metadata, the PyTorch index listing, PEP 658 sidecars) through it rather than adding four baseline entries repeating the same paragraph. The URL's scheme is CHECKED there, not asserted: anything but `https` raises, so a mis-built `file:` URL is a crash and not a quiet local read. Deliberately not marked `# nosec` — an inline suppression removes the finding from the report, while the baseline keeps it counted and explained here. (2) `urlopen` in the model-availability probe — FIXED, P16-05, "
             "FINDING AK. The call carried `# noqa: S310 - localhost only` "
             "over a URL built from `IRA_OLLAMA_BASE_URL`/`OLLAMA_BASE_URL` "
             "that nothing constrained, so `file:///etc` was fetched and "
             "decoded. The suppression's reason was also false about this "
             "repository's own deployments — the portable and test compose "
             "files set `http://ollama:11434` and `host.docker.internal`, "
             "neither of them localhost. The scheme is now checked against "
             "{http, https} before `urlopen`, and a refusal degrades to "
             "`reachable=False` (UNKNOWN) rather than to an empty model set. "
             "Bandit still reports the line because it reads the call, not the "
             "guard above it; the finding stays visible deliberately. "
             "(3) `core/billing/provider.py` — the payment provider's one "
             "outbound call. `IRA_STRIPE_API_BASE` is env-controlled and was "
             "unconstrained, so a `file:` base would have had /etc/passwd "
             "parsed as a provider response; Bandit found that on the first "
             "scan of the new module. The scheme is now checked against "
             "{http, https} before anything is opened — `http` because the "
             "checkout path is exercised against a local socket, `https` "
             "because that is what production uses — and the refusal is a "
             "ProviderCallFailed, which the routes turn into "
             "CHECKOUT_UNAVAILABLE rather than into a silent success. "
             "Carried here rather than `# nosec` for the same reason as (1): "
             "the baseline keeps it counted and explained where somebody can "
             "disagree with it."),
}


def _run_bandit() -> dict:
    """Run Bandit and return its JSON. A crash is FAILED, never CLEAN."""
    proc = subprocess.run(
        [sys.executable, "-m", "bandit", "-r", str(IRA_ROOT),
         "-x", f"{IRA_ROOT}/tests,{IRA_ROOT}/.venv,{IRA_ROOT}/node_modules",
         "-f", "json", "-q"],
        capture_output=True, text=True)
    if not proc.stdout.strip():
        print(f"::error::bandit produced no output (exit {proc.returncode}). "
              f"That is UNKNOWN, not clean.\n{proc.stderr[-2000:]}")
        raise SystemExit(2)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"::error::bandit output was not JSON ({exc}). UNKNOWN, not clean.")
        raise SystemExit(2) from None


def _relative(filename: str) -> str:
    try:
        return str(Path(filename).resolve().relative_to(IRA_ROOT))
    except ValueError:
        return filename.lstrip("./")


_SOURCE: dict[str, list[str]] = {}


def _source_line(file: str, line: int) -> str:
    """The text of one line of IRA_ROOT/<file>, indentation removed."""
    lines = _SOURCE.get(file)
    if lines is None:
        try:
            lines = (IRA_ROOT / file).read_text(encoding="utf-8", errors="replace").split("\n")
        except OSError:
            lines = []
        _SOURCE[file] = lines
    return lines[line - 1].strip() if 0 < line <= len(lines) else ""


_LAYOUT = {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}


def _normalised(code: str) -> str:
    """The line as its tokens, so layout outside literals does not count.

    String and comment tokens keep every character: whitespace inside a literal
    is part of its value, and an edited value deserves a fresh look. A line that
    does not tokenize on its own (an unterminated triple-quoted string, an
    unmatched bracket) is compared exactly, indentation aside: when unsure, the
    finding is reported as new rather than carried.
    """
    code = code.strip()
    try:
        tokens = [t.string for t in tokenize.generate_tokens(io.StringIO(code + "\n").readline)
                  if t.type not in _LAYOUT]
    except (tokenize.TokenError, SyntaxError):
        return code
    return json.dumps(tokens, ensure_ascii=False)


def _identity(test_id: str, file: str, code: str) -> tuple[str, str, str]:
    return (test_id, file, _normalised(code))


def compare(entries: list[dict], gated: list[dict], source_line=_source_line):
    """Match gated Bandit results against the baseline by (test, file, line text).

    Returns (new, gone, moved):
      new   - [(test_id, file, line, code, carried, found)] for every site of a
              (test, file, code) that the tree has more of than the baseline carries;
      gone  - [(test_id, file, line)] baseline entries the tree no longer has;
      moved - [(test_id, file, old_line, new_line)] carried findings now on another line.
    """
    carried = Counter(_identity(e["test_id"], e["file"], e["code"]) for e in entries)
    recorded: dict[tuple, list[int]] = {}
    for e in entries:
        recorded.setdefault(_identity(e["test_id"], e["file"], e["code"]), []).append(e["line"])
    sites: dict[tuple, list[int]] = {}
    text: dict[tuple, str] = {}
    for r in gated:
        file = _relative(r["filename"])
        line = source_line(file, r["line_number"])
        ident = _identity(r["test_id"], file, line)
        sites.setdefault(ident, []).append(r["line_number"])
        text.setdefault(ident, line.strip())

    new, gone, moved = [], [], []
    for ident in sorted(set(sites) | set(carried)):
        test_id, file, _ = ident
        at, was = sorted(sites.get(ident, [])), sorted(recorded.get(ident, []))
        if len(at) > carried[ident]:
            new += [(test_id, file, line, text[ident], carried[ident], len(at)) for line in at]
        elif len(at) < carried[ident]:
            gone += [(test_id, file, line) for line in was if line not in at][:carried[ident] - len(at)]
        if len(at) == len(was) and at != was:
            moved += [(test_id, file, a, b) for a, b in zip(was, at) if a != b]
    return new, gone, moved


def _check_reasons(entries: list[dict]) -> list[str]:
    """Every carried test id must have a written reason."""
    return sorted({e["test_id"] for e in entries} - set(REASONS))


def main() -> int:
    if not BASELINE.is_file():
        print(f"::error::{BASELINE.name} is missing. The gate has no baseline "
              f"to compare against and cannot distinguish a clean tree from an "
              f"unscanned one.")
        return 2

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    entries = baseline["entries"]

    missing = _check_reasons(entries)
    if missing:
        print(f"::error::baseline carries {missing} with no entry in REASONS. "
              f"A suppression without a stated reason is a decision nobody made.")
        return 1
    uncoded = [e for e in entries if not str(e.get("code", "")).strip()]
    if uncoded:
        print(f"::error::{len(uncoded)} baseline entr(y/ies) carry no `code`, e.g. "
              f"{uncoded[0]}. A finding is matched by its source line's text; "
              f"without it the gate cannot tell a moved finding from a new one.")
        return 2

    data = _run_bandit()
    results = data["results"]
    gated = [r for r in results
             if r["issue_severity"] in GATED_SEVERITY
             and r["issue_confidence"] in GATED_CONFIDENCE]

    new, gone, moved = compare(entries, gated)

    print(f"bandit: {len(results)} results over {len(data.get('metrics', {})) - 1} files")
    print(f"  gated threshold (severity>=MEDIUM and confidence>=MEDIUM): {len(gated)}")
    print(f"  BELOW the threshold, reported but NOT gated: {len(results) - len(gated)}")
    print(f"  carried in the baseline: {len(entries)}")
    for test_id in sorted({e['test_id'] for e in entries}):
        n = sum(1 for e in entries if e["test_id"] == test_id)
        print(f"    {test_id} x{n}: {REASONS[test_id][:96]}...")

    if moved:
        print("\n  carried findings on a new line (the code moved, not a new finding):")
        for test_id, filename, was, now in moved:
            print(f"    {test_id}  {filename}:{was} -> {now}")
        print("  Not a failure. Update `line` in the baseline when convenient.")

    if new:
        print("\n::error::NEW findings at the gated threshold:")
        for test_id, filename, line, code, carried, found in new:
            extra = f"  (the baseline carries {carried} of this line, the tree has {found})" if carried else ""
            print(f"  {test_id}  {filename}:{line}{extra}")
        print("Fix it, or add it to .bandit-baseline.json WITH a reason in "
              "REASONS — in a diff, where somebody can disagree with it. "
              "An entry to carry it:")
        for test_id, filename, line, code, _, _ in new:
            print("    " + json.dumps({"test_id": test_id, "file": filename,
                                       "line": line, "code": code}, ensure_ascii=False))
        return 1

    if gone:
        print("\n  baseline entries no longer reported (fixed, or the line's text changed):")
        for test_id, filename, line in gone:
            print(f"    {test_id}  {filename}:{line}")
        print("  Not a failure. Remove them from the baseline so it keeps "
              "describing the tree rather than its history.")

    print("\n✅ no new findings at the gated threshold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
