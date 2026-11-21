#!/usr/bin/env python3
"""P16-05 — SEC-05: the advisory position as a GATE, not a number.

The Phase 1 baseline said *"101 advisories across 17 production packages"*.
Fifteen phases later that sentence describes a tree that no longer exists, and
re-counting it would produce a second number with the same shelf life. What was
missing was never the count. It was the control that notices the next one.

── What this enforces ──────────────────────────────────────────────────────

`pip-audit` is run against **`requirements.lock`** — the resolved set, the same
input DEP-01 and DEP-03 read — and its answer is compared against `DECLARED`
below. Four things fail:

1. **A package with advisories that is not in `DECLARED`.** Either a new
   dependency arrived carrying advisories, or an existing one was bumped into
   a vulnerable version. This is the case the gate exists for.
2. **A declared package whose locked version has changed.** The note attached
   to it was written about a specific version; once that moves, the note is
   describing something else. Same drift check DEP-03 applies to licences.
3. **A declared package whose advisory count has grown.** Upstream published
   something new. That is not this repository's doing, and the gate still goes
   red — because the alternative is a counter that only ever gets read after an
   incident. The remedy is one line: raise the count, having looked at what
   was added.
4. **A declared package that no longer has advisories, or is no longer
   locked.** The list must describe the tree, not its history.

── An audit that could not run is UNKNOWN, never clean ─────────────────────

Carried Phase 13 semantics, item S: a scanner that inspected nothing must not
report CLEAN. `pip-audit` needs to reach the OSV database; if it cannot, or
returns something this script cannot parse, or audits a suspiciously small
number of packages, the gate exits **non-zero with UNKNOWN** rather than
observing zero advisories and calling it success.

── What is deliberately NOT claimed ────────────────────────────────────────

`torch` and `torchaudio` are outside the lock — see `LOCK_EXCLUDED` in
`check_dependency_gates.py` for why, and note that the reason is a supply-chain
protection, not an oversight. They are therefore outside this audit. Their
advisory status is **UNKNOWN**, it is printed on every run, and `UNAUDITED`
below fails the gate if they ever quietly enter the lock without this list
being updated — which would otherwise let the largest packages in the tree
become "audited clean" without anybody deciding that.

This gate does not fix the 93 advisories. Upgrading `langchain` or `starlette`
across a major version is not a supply-chain batch's work, and pretending
otherwise would produce either a broken tree or a fake number.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

IRA_ROOT = Path(__file__).resolve().parents[1]
LOCK = IRA_ROOT / "requirements.lock"

#: Packages known to carry advisories, as measured against this lock.
#:
#: `count` is the number of advisories `pip-audit` reports; `version` is the
#: locked version the count was taken at. Both are checked, so neither can
#: drift without the gate saying so.
#:
#: These are RECORDED, not accepted. Whether a hosted product ships them is a
#: decision, and it belongs in `SECURITY_ADVISORY_POSITION.md` where it can be
#: read, not in the fact that CI is green.
DECLARED: dict[str, tuple[str, int, str]] = {
    # EMPTY. 93 advisory records -> 0.
    #
    # P16-R1a closed 54 (pypdf, yt-dlp, protobuf by upgrade; ecdsa and pyasn1
    # by removing python-jose). P16-R1b closed 34 (the framework, plus removing
    # `langchain` and `langchain-openai`, which were declared and never
    # imported). P16-R2 closed the last 5 by raising torch to 2.11.0+cpu, which
    # unblocked transformers 5.15.0.
    #
    # A CORRECTION worth keeping: two of those last five were reported as
    # having "no fix version" and were nearly dispositioned by a reachability
    # argument. They use OSV `last_affected` (5.0.0-rc0 and 5.2.0) rather than
    # a `fixed` event, which is why pip-audit showed no fix. "No fix version
    # listed" is NOT "affects all versions" — they simply do not apply at
    # 5.15.0.
    #
    # An empty dict is not the same as a disabled gate: the checks below still
    # run, and any package that acquires an advisory fails until it is declared
    # here with a reason.
}

#: Outside the lock, therefore outside this audit. Their advisory status is
#: UNKNOWN. If one enters the lock, this list must be updated deliberately.
UNAUDITED: dict[str, str] = {
    # EMPTY, and that is the point. P16-R2 closed the torch family's advisory,
    # checksum and licence UNKNOWNs by locking it from a single index and
    # auditing it at its base version — see check_torch_supply_chain.py.
    #
    # The line this gate used to print, "NOT AUDITED — torch, torchaudio", was
    # concealing 23 advisories on torch 2.4.1. Anything added here must come
    # with the same standard of reason.
}

#: Below this, assume the audit did not really happen. The lock carries 160
#: packages (189 before P16-R1a removed `langchain`, R1b `python-jose`, and
#: R3 the torch subtree and `pygithub`); a run that inspected a handful found
#: nothing because it looked at nothing.
_MIN_CREDIBLE_AUDITED = 100


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked_versions() -> dict[str, str]:
    pin = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;#\\]+)")
    out: dict[str, str] = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or line.startswith(" "):
            continue
        m = pin.match(line)
        if m:
            out[_norm(m.group(1))] = m.group(2)
    return out


def _run_audit() -> tuple[dict | None, str]:
    """Return (parsed report, error). Exactly one is meaningful."""
    if not LOCK.is_file():
        return None, f"{LOCK.name} is missing — there is nothing to audit"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip_audit", "-r", str(LOCK),
             "--disable-pip", "-f", "json", "--progress-spinner", "off"],
            capture_output=True, text=True, timeout=900,
        )
    except FileNotFoundError:
        return None, "pip-audit is not installed"
    except subprocess.TimeoutExpired:
        return None, "pip-audit timed out — the advisory database was not read"

    # Exit 1 means "vulnerabilities found", which is the expected case here.
    # Anything else, or unparseable output, means the audit did not happen.
    try:
        report = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        tail = (proc.stderr or proc.stdout or "")[-400:]
        return None, (f"pip-audit produced no parseable report "
                      f"(exit {proc.returncode}): {tail}")
    if proc.returncode not in (0, 1):
        return None, (f"pip-audit exited {proc.returncode}: "
                      f"{(proc.stderr or '')[-400:]}")
    return report, ""


def main() -> int:
    report, error = _run_audit()
    if report is None:
        print(f"::error::SEC-05: the advisory position is UNKNOWN, which is "
              f"NOT the same as clean: {error}")
        return 2

    deps = report.get("dependencies") or []
    if len(deps) < _MIN_CREDIBLE_AUDITED:
        print(f"::error::SEC-05: pip-audit reported on {len(deps)} package(s), "
              f"below the {_MIN_CREDIBLE_AUDITED} this lock is known to "
              f"contain. An audit that inspected almost nothing found nothing. "
              f"UNKNOWN, not clean.")
        return 2

    locked = _locked_versions()
    found: dict[str, tuple[str, int, list[str]]] = {}
    for dep in deps:
        vulns = dep.get("vulns") or []
        if vulns:
            found[_norm(dep["name"])] = (
                dep.get("version", "?"), len(vulns),
                sorted(v.get("id", "?") for v in vulns),
            )

    declared = {_norm(k): v for k, v in DECLARED.items()}
    problems: list[str] = []

    print(f"SEC-05: {len(deps)} package(s) audited from {LOCK.name}, "
          f"{len(found)} carrying {sum(c for _, c, _ in found.values())} "
          f"advisory/advisories")
    for name in sorted(found, key=lambda n: -found[n][1]):
        version, count, _ = found[name]
        mark = " " if name in declared else "NEW"
        print(f"  {mark:>3} {name:<32} {version:<14} {count:>3}")

    if UNAUDITED:
        print(f"  NOT AUDITED — status UNKNOWN, not clean: "
              f"{', '.join(sorted(UNAUDITED))}")
    else:
        print("  nothing is excluded from audit. The torch family is locked "
              "and audited separately — see check_torch_supply_chain.py")

    for name in sorted(set(found) - set(declared)):
        version, count, ids = found[name]
        problems.append(
            f"{name} {version} carries {count} advisory/advisories and is NOT "
            f"declared: {ids[:6]}. Either it just entered the tree, or a bump "
            f"moved it into a vulnerable version. Decide, then record it in "
            f"DECLARED with what it is and how it arrived.")

    for name in sorted(set(declared) & set(found)):
        want_version, want_count, _ = declared[name]
        version, count, ids = found[name]
        if version != want_version:
            problems.append(
                f"{name}: declared at {want_version}, locked at {version}. The "
                f"note recorded against it was written about a different "
                f"version. Re-read it and update the entry.")
        elif count > want_count:
            problems.append(
                f"{name} {version}: {count} advisories now, {want_count} "
                f"declared. {count - want_count} published upstream since. "
                f"Current IDs: {ids[:8]}. Look at what was added, then raise "
                f"the count — a counter nobody updates is one nobody reads.")
        elif count < want_count:
            problems.append(
                f"{name} {version}: {count} advisories now, {want_count} "
                f"declared. Advisories were withdrawn or re-scoped. Lower the "
                f"count so the declaration keeps matching the measurement.")

    for name in sorted(set(declared) - set(found)):
        why = ("it is no longer in the lock at all"
               if name not in locked else
               f"it is locked at {locked[name]} and now reports none")
        problems.append(
            f"{name} is declared as carrying advisories, but {why}. Remove the "
            f"entry so the list describes the tree rather than its history.")

    for name in sorted(UNAUDITED):
        if _norm(name) in locked:
            problems.append(
                f"{name} is listed as NOT audited but now appears in the lock "
                f"at {locked[_norm(name)]}. It is inside the audit now — take "
                f"it out of UNAUDITED and declare its real position, rather "
                f"than letting it read as UNKNOWN while being measured.")

    if problems:
        print("\n::error::SEC-05 (advisory gate):")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\n✅ every package carrying an advisory is declared, at the locked "
          "version, at the recorded count")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
