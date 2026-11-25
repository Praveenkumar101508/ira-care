#!/usr/bin/env python3
"""P16-04 — DEP-03: the licence position, enumerated and kept honest.

── Why this reads the LOCK and not the environment ─────────────────────────

P16-01 ran `pip-licenses`, which reported 163 packages and zero unknown
licences. It was reading the **installed container**: its copyleft hits were
`python-apt`, `PyGObject` and `launchpadlib`, none of which are dependencies of
this project. A real tool, run correctly, answering the wrong question.

Worse, it was wrong in both directions. It **missed** the package that actually
matters — `icalendar-searcher`, **AGPL-3.0-or-later**, which arrives
transitively via `caldav` and is not installed in that container at all.

So the inventory is generated from `requirements.lock` — the resolved
dependency set — by asking PyPI for each package **at its locked version**.

── What this gate enforces ─────────────────────────────────────────────────

1. **Every locked package appears in the inventory.** A package added to the
   lock without a licence answer fails, rather than being silently absent.
2. **The inventory's versions match the lock.** A stale inventory describes a
   tree that no longer exists — the failure this phase opened by disbelieving.
3. **Copyleft and undeclared packages are DECLARED, not merely present.** Each
   is listed in `NOTABLE` with what it is and how it arrived. A new one fails
   the gate; it does not appear quietly in a JSON file nobody diffs.

── What this gate does NOT do ──────────────────────────────────────────────

**It does not decide whether any licence is acceptable.** That is the owner's,
explicitly and permanently: enumerating the position is engineering, ruling a
copyleft dependency in or out of a commercial hosted product is not. The gate
makes the position visible and keeps it visible. It never approves it.

It also does not cover **npm** (732 packages) or the **vendored `third_party/`
trees**, which carry their terms in `NOTICE.md` files rather than package
metadata. Both are recorded as `UNKNOWN` in the register rather than omitted.

── FINDING AO: the copyleft detector did not detect copyleft ───────────────

The original pattern was `\\b(GPL|AGPL|LGPL|MPL|...)\\b`. The trailing `\\b`
requires a non-word character after the abbreviation, so it matched
`LGPL-3.0-only` and missed every trove-classifier spelling:

    "GNU Lesser General Public License v3 (LGPLv3)"   -> no match
    "GPLv2", "AGPLv3", "GPLv3+"                        -> no match

`psycopg` and `psycopg-binary` are **LGPLv3** and were reported by this gate as
unremarkable for four batches, because the word it was looking for had a `v`
after it. `pygithub` was caught only by the parenthesised `(LGPL)` at the end
of its string — an accident, not the check working.

That is the exact shape of defect this programme exists to remove: a scanner
that runs, inspects the right files, prints a number, and reports clean because
its pattern did not fire. The pattern now allows a version suffix and also
matches the spelled-out phrases, and
`tests/test_p16r3_licence_detection.py` asserts it against every form above —
including negative controls, so a pattern that matched *everything* would fail
too.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

IRA_ROOT = Path(__file__).resolve().parents[1]
#: BOTH locks. FINDING AM moved six packages into the torch lock; they are
#: still installed, and an inventory built from one file would have dropped
#: them and reported a smaller, cleaner position produced by looking away.
LOCKS = (IRA_ROOT / "requirements.lock", IRA_ROOT / "requirements-torch.lock")
INVENTORY = IRA_ROOT / "dependency-licences.json"

COPYLEFT = re.compile(
    # Abbreviations, with an optional version suffix: LGPLv3, GPLv2, AGPLv3+.
    r"\b(?:A?GPL|LGPL|MPL|EUPL|CDDL|EPL|CeCILL|OSL|SSPL)(?:\s*v?\d[\d.]*)?"
    # …and the spelled-out forms, which is how trove classifiers write them.
    r"|General Public License"
    r"|Mozilla Public License"
    r"|Eclipse Public License"
    r"|Common Development and Distribution License",
    re.I)

#: Every copyleft-family or undeclared package, with what it is and how it got
#: here. A package that becomes notable and is not in here FAILS the gate.
#:
#: These are recorded, NOT approved. Acceptability is the owner's decision.
NOTABLE = {
    "psycopg": (
        "LGPL-3.0-or-later, as the PostgreSQL driver. **Surfaced by FINDING "
        "AO** — the classifier string is 'GNU Lesser General Public License v3 "
        "(LGPLv3)' and the old pattern did not match it, so this gate called "
        "the tree clean of it for four batches. Unavoidable: it is what "
        "`langgraph-checkpoint-postgres` requires, and dropping it means "
        "dropping LangGraph's Postgres checkpointer. Recorded, not approved."),
    "psycopg-binary": (
        "LGPL-3.0-or-later. The compiled distribution of `psycopg`, arriving "
        "with it. Same finding, same answer."),
    "caldav": (
        "GPL-3.0-or-later OR Apache-2.0 — dual, and **the Apache-2.0 option is "
        "the one taken**. Recorded here rather than assumed, which is what "
        "P16-04 said had to happen. P16-R3 also pinned this to 2.1.1: 3.x "
        "hard-requires `icalendar-searcher` (AGPL-3.0-or-later) as a "
        "mandatory dependency, and 2.1.1 does not. That removal is FINDING "
        "AJ's disposition — REMOVED, not accepted."),
    "recurring-ical-events": (
        "LGPL-3.0-or-later. Transitive, via the caldav chain. LGPL linking in "
        "a Python service is generally accepted as dynamic use; 'generally "
        "accepted' is not a determination. OWNER DECISION."),
    "psycopg-pool": (
        "LGPL-3.0-only. Connection pooling for PostgreSQL. Same LGPL question "
        "as above and the same answer: recorded, not decided."),
    "certifi": "MPL-2.0. File-level copyleft; the CA bundle is not modified.",
    "orjson": "MPL-2.0 AND (Apache-2.0 OR MIT). File-level copyleft.",
    "tqdm": "MPL-2.0 AND MIT. File-level copyleft.",
    "x-wr-timezone": (
        "LGPL-3.0-or-later. Transitive, via `recurring-ical-events` — the same "
        "caldav chain that carries FINDING AJ, and the same LGPL question as "
        "`recurring-ical-events` itself. Surfaced by P16-R1a's regeneration, "
        "not newly introduced: it was in the tree before and the previous "
        "inventory recorded a different licence string for it. That is worth "
        "noting on its own — a licence answer is only as current as the fetch "
        "that produced it. OWNER DECISION, with the rest of the LGPL set."),
}
#: Resolved and no longer here, kept as a record of how they were closed:
#:
#: `pygithub` (LGPL) — **REMOVED** in P16-R3. It carried four plain REST calls
#: in `utils/career_tools.py`; `httpx` (BSD-3-Clause, already a dependency)
#: makes them directly. No feature lost, one copyleft dependency fewer, and no
#: legal conclusion asserted to get there.
#:
#: `fsspec` — was UNDECLARED in PyPI metadata, which is not permission. The
#: published wheel ships `dist-info/licenses/LICENSE` reading "BSD 3-Clause
#: License"; `scripts/build_licence_inventory.py` now reads it from the
#: artifact and records the artifact's sha256 beside the answer.
#:
#: The NVIDIA/CUDA set (`cuda-toolkit`, `nvidia-*`) — **REMOVED** by FINDING
#: AM. They were never installed: they came from the PyPI *GPU* torch build,
#: which this product does not use. The deployed `+cpu` wheel needs no CUDA
#: package at all.

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;#\\]+)")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked() -> dict[str, str]:
    out = {}
    for lock in LOCKS:
        for line in lock.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or line.startswith(" "):
                continue
            m = _PIN.match(line)
            if m:
                out[_norm(m.group(1))] = m.group(2)
    return out


def main() -> int:
    if not INVENTORY.is_file():
        print(f"::error::{INVENTORY.name} is missing. The licence position is "
              f"UNKNOWN, which is not the same as clean.")
        return 2

    inv = json.loads(INVENTORY.read_text(encoding="utf-8"))["packages"]
    inv = {_norm(k): v for k, v in inv.items()}
    locked = _locked()
    problems: list[str] = []

    missing = sorted(set(locked) - set(inv))
    if missing:
        problems.append(
            f"{len(missing)} locked package(s) have no licence answer: "
            f"{missing[:8]}{'...' if len(missing) > 8 else ''}. Regenerate the "
            f"inventory — a package with no answer is UNKNOWN, not permissive.")

    drifted = [f"{n} (lock {v}, inventory {inv[n]['version']})"
               for n, v in locked.items()
               if n in inv and inv[n]["version"] != v]
    if drifted:
        problems.append(
            f"the inventory disagrees with the lock for {len(drifted)}: "
            f"{drifted[:5]}. It describes a tree that no longer exists.")

    unresolved = sorted(n for n, v in inv.items() if not v.get("resolved"))
    if unresolved:
        problems.append(
            f"{len(unresolved)} package(s) could not be resolved when the "
            f"inventory was built: {unresolved}. UNKNOWN, not clean.")

    # An answer read out of an artifact rather than out of metadata must name
    # the artifact it was read from, or "we checked the wheel" becomes an
    # unfalsifiable escape hatch from the UNDECLARED rule below.
    unevidenced = sorted(
        n for n, v in inv.items()
        if str(v.get("source", "")).startswith("artifact-licence-file")
        and not v.get("artifact_sha256"))
    if unevidenced:
        problems.append(
            f"{unevidenced} claim a licence read from the published artifact "
            f"but record no artifact sha256. An answer that cannot be traced "
            f"to the thing it was read from is not evidence.")

    notable = {_norm(k) for k in NOTABLE}
    found = {n for n, v in inv.items()
             if n in locked
             and (COPYLEFT.search(v["licence"]) or v["licence"] == "UNDECLARED")}
    undeclared = sorted(n for n in found if inv[n]["licence"] == "UNDECLARED")
    copyleft = sorted(found - set(undeclared))

    print(f"DEP-03: {len(locked)} locked, {len(inv)} in the inventory")
    print(f"  copyleft-family: {len(copyleft)}")
    for n in copyleft:
        print(f"    {n:24} {inv[n]['licence'][:60]}")
    print(f"  UNDECLARED licence: {len(undeclared)}")
    for n in undeclared:
        print(f"    {n}")
    print("  NOT COVERED, recorded as UNKNOWN rather than omitted: npm (732 "
          "packages) and the vendored third_party/ trees")

    new = sorted(found - notable)
    if new:
        problems.append(
            f"{len(new)} copyleft or undeclared package(s) are NOT declared in "
            f"NOTABLE: {new}. Each needs an entry saying what it is and how it "
            f"arrived — in a diff, where somebody can read it. This gate "
            f"records; it does not approve.")

    stale = sorted(notable - set(locked))
    if stale:
        problems.append(
            f"NOTABLE declares {stale}, which the lock no longer contains. "
            f"Remove them so the list describes the tree, not its history.")

    if problems:
        print("\n::error::DEP-03 (licence inventory):")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\n✅ every locked package has a licence answer; every copyleft and "
          "undeclared one is declared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
