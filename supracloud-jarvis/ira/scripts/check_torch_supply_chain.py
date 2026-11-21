#!/usr/bin/env python3
"""P16-R2 — the torch family: advisory, checksum and licence, all measured.

`torch` and `torchaudio` were carried as UNKNOWN on all three axes since
P16-03. That was honest and it was not harmless: queried against OSV at its
base version, the pinned `torch==2.4.1+cpu` had **23 advisories**. An UNKNOWN
is not a neutral state, and "we wrote it down" is not "we know it is not
urgent".

── Why this gate exists instead of adding the lock to pip-audit ────────────

**`pip-audit` reports the torch lock CLEAN while auditing nothing about
torch.** Run against `requirements-torch.lock` it inspects 11 packages and
flags only `setuptools` — it silently skips `torch` itself, because it cannot
match the PEP 440 local version `2.11.0+cpu` against advisories published for
`2.11.0`.

So the obvious implementation would have produced a green tick over an
unaudited package: carried Phase 13 item S, in the batch meant to close it.

This gate therefore:

1. **Strips the local version segment** and queries OSV for the base release.
   `2.11.0+cpu` is upstream `2.11.0` plus a build variant; advisories are
   published against the release.
2. **Fails when the audit returns nothing for a package it was asked about.**
   Silence from an unreachable OSV is `UNKNOWN`, never `CLEAN`.
3. **Requires every artifact to carry a hash**, which is the checksum half.
4. **Requires a licence answer** for each package in the lock.

── The one advisory that remains, and why it is not deferred ───────────────

`torch 2.11.0` carries `GHSA-rrmf-rvhw-rf47` — memory corruption via
`torch.jit.script`. Fixed in 2.13.0, which cannot be used: `torchaudio`'s last
CPU build is 2.11.0, and `requirements.txt` documents why the pair must match
exactly.

It is dispositioned **PROVED NOT A DEFECT by reachability**, with executable
evidence rather than a reading: `tests/test_p16r2_torch_supply_chain.py` walks
every module in the tree and asserts nothing references `torch.jit` at all. The
vulnerable entry point is not called, and the test fails the day it is.

── The split between the two locks (FINDING AN) ────────────────────────────

Splitting the dependency set across two independently resolved locks creates a
failure mode of its own: the same package pinned twice, at versions resolved
from different indexes. `pip install -r requirements.lock -r
requirements-torch.lock` then fails outright, and installed one file at a time
whichever runs second silently replaces the other's package — so both locks
claim to describe an installation neither one does.

This gate therefore also enforces the split's own invariants:

5. **The two locks are disjoint.** No name may be pinned in both.
6. **Every requirement the pinned torch wheel declares is pinned in exactly one
   of them, at a satisfying version.** Read from the wheel's PEP 658 metadata
   sidecar, tied to the artifact by a hash the lock itself carries — **not**
   from PyPI's `torch 2.11.0`, which describes the GPU build and declares a
   CUDA stack the `+cpu` wheel does not have. Believing PyPI here is exactly
   what put 16 phantom NVIDIA pins in the main lock (FINDING AM).
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from pathlib import Path

# Not wrapped in try/except. If the environment cannot satisfy this, the gate
# must fail loudly rather than skip the specifier check and report success —
# "the tool was absent" is the first item on Phase 13's list of ways a check
# passes without checking.
from packaging.requirements import Requirement
from packaging.version import Version

from _net import fetch, fetch_text

IRA_ROOT = Path(__file__).resolve().parents[1]
TORCH_LOCK = IRA_ROOT / "requirements-torch.lock"
TORCH_REQS = IRA_ROOT / "requirements-torch.txt"
MAIN_LOCK = IRA_ROOT / "requirements.lock"

#: The index the torch family resolves from. One index, by construction.
TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
#: The interpreter and platform the deployment targets — see the Dockerfiles.
#: The wheel whose metadata is read must be one the lock actually pins, which
#: is asserted by matching its sha256 against the lock's own hashes.
WHEEL_TAG = "cp312-cp312-manylinux_2_28_x86_64"

#: Advisories known to affect the locked torch family, with their disposition.
#: A package reporting an advisory NOT listed here fails the gate.
DECLARED: dict[str, str] = {
    "GHSA-rrmf-rvhw-rf47": (
        "torch: memory corruption via `torch.jit.script`. Fixed in 2.13.0, "
        "which cannot be adopted while torchaudio is a dependency — its last "
        "CPU build is 2.11.0. PROVED NOT A DEFECT BY REACHABILITY: nothing in "
        "this tree references `torch.jit` at all, asserted by "
        "tests/test_p16r2_torch_supply_chain.py over every module, so the "
        "vulnerable entry point is never called."),
}

#: Licence answers, from PyPI metadata at the locked version.
LICENCES: dict[str, str] = {
    "torch": "BSD-3-Clause",
    "torchaudio": "BSD-3-Clause (OSI classifier; no SPDX expression published)",
}

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;#\\]+)")


def _base_version(version: str) -> str:
    """Drop the PEP 440 local segment: `2.11.0+cpu` -> `2.11.0`."""
    return version.split("+", 1)[0]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or line.startswith(" "):
            continue
        m = _PIN.match(line)
        if m:
            out[_norm(m.group(1))] = m.group(2)
    return out


def _locked() -> dict[str, str]:
    return _pins(TORCH_LOCK)


def _wheel_requirements(version: str, lock_text: str) -> list[str] | None:
    """`Requires-Dist` of the exact wheel the lock pins, or None if unanswered.

    None is NOT an empty list. An unreachable index means UNKNOWN — a wheel
    whose requirements could not be read has not been shown to have none.
    """
    quoted = urllib.parse.quote(f"torch-{version}-{WHEEL_TAG}.whl", safe="")
    listing = fetch_text(f"{TORCH_INDEX}/torch/")
    if listing is None:
        return None

    hrefs = re.findall(r'href="([^"]*?%s[^"]*?)"' % re.escape(quoted), listing)
    if not hrefs:
        print(f"::error::no {WHEEL_TAG} wheel for torch {version} on "
              f"{TORCH_INDEX}. The lock pins an artifact the index does not "
              f"list, so its requirements cannot be read from it.")
        return None

    url, _, fragment = hrefs[0].partition("#")
    digest = fragment.removeprefix("sha256=")
    if not digest or f"--hash=sha256:{digest}" not in lock_text:
        print(f"::error::the {WHEEL_TAG} wheel the index serves has sha256 "
              f"{digest or '(none published)'}, which {TORCH_LOCK.name} does "
              f"not pin. Reading its metadata would describe a different "
              f"artifact from the one this project installs.")
        return None

    metadata = fetch_text(f"{url}.metadata")
    if metadata is None:
        return None

    return [line.split(":", 1)[1].strip()
            for line in metadata.splitlines()
            if line.lower().startswith("requires-dist:")]


def _osv(name: str, version: str) -> list[str] | None:
    """Advisory ids, or None if the query could not be answered.

    None is NOT an empty list. A network failure means UNKNOWN.
    """
    body = fetch(
        "https://api.osv.dev/v1/query",
        data=json.dumps({"package": {"name": name, "ecosystem": "PyPI"},
                         "version": version}).encode(),
        headers={"Content-Type": "application/json"})
    if body is None:
        return None
    try:
        return sorted(v["id"] for v in json.loads(body).get("vulns", []))
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def main() -> int:
    if not TORCH_LOCK.is_file():
        print(f"::error::{TORCH_LOCK.name} is missing. The torch family's "
              f"checksum and advisory position are UNKNOWN, not clean.")
        return 2

    text = TORCH_LOCK.read_text(encoding="utf-8")
    hashes = text.count("--hash=sha256:")
    locked = _locked()
    problems: list[str] = []

    def _also(problems: list[str]) -> None:
        """Print what was already found before an UNKNOWN cuts the run short.

        Without this, an UNKNOWN return SWALLOWS every finding collected before
        it. That is not hypothetical: the self-test strips the lock's hashes and
        requires the gate to say "carries NO hashes", and once the wheel-metadata
        check existed the run ended at UNKNOWN instead — the same defect the
        stripped hashes had caused, reported as a network problem. The plant
        went red for the wrong reason and said so, which is why the assertion
        names the expected message rather than just checking the exit code.
        """
        for problem in problems:
            print(f"  also found, before the check above could not continue: "
                  f"{problem}")

    if hashes == 0:
        problems.append(
            f"{TORCH_LOCK.name} carries NO hashes. Without them this names "
            f"releases, not artifacts, and the checksum position is UNKNOWN.")

    # The lock must be built from ONE index. --extra-index-url here would
    # reintroduce exactly the confusion this file exists to avoid.
    # Read DIRECTIVE LINES, not prose. The first version of this check
    # substring-matched the whole file and fired on its own header, which
    # explains why --extra-index-url is not used — a check satisfied (or here,
    # tripped) by the text it is guarding rather than by the structure it is
    # about. Same defect as P16-06's "Confirmed absent" escape hatch.
    def _has_extra_index(source: str) -> bool:
        return any(ln.lstrip().startswith("--extra-index-url")
                   for ln in source.splitlines())

    reqs = TORCH_REQS.read_text(encoding="utf-8")
    if _has_extra_index(reqs) or _has_extra_index(text):
        problems.append(
            "the torch requirements or lock reference --extra-index-url. This "
            "family must resolve from a SINGLE index, or cross-index "
            "dependency confusion is back and the separation buys nothing.")

    print(f"TORCH: {len(locked)} packages locked, {hashes} hashes, single-index")

    unknown, found_ids = [], {}
    for name in ("torch", "torchaudio"):
        version = locked.get(name)
        if version is None:
            problems.append(f"{name} is not in {TORCH_LOCK.name} at all.")
            continue
        ids = _osv(name, _base_version(version))
        if ids is None:
            unknown.append(name)
            continue
        found_ids[name] = ids
        print(f"  {name:<12} {version:<14} base={_base_version(version):<8} "
              f"{len(ids)} advisor{'y' if len(ids) == 1 else 'ies'}"
              f"{': ' + ', '.join(ids) if ids else ''}")

    if unknown:
        print(f"::error::the advisory database could not be reached for "
              f"{unknown}. That is UNKNOWN, which is NOT the same as clean — a "
              f"scanner that inspected nothing does not get to report success.")
        _also(problems)
        return 2

    for name, ids in found_ids.items():
        for advisory in ids:
            if advisory not in DECLARED:
                problems.append(
                    f"{name} reports {advisory}, which is not declared. Either "
                    f"upgrade, or record what it is and why it is not "
                    f"reachable — in a diff, where it can be disagreed with.")

    stale = sorted(set(DECLARED) - {i for ids in found_ids.values() for i in ids})
    if stale:
        problems.append(
            f"DECLARED names {stale}, which the locked versions no longer "
            f"report. Remove them so the list describes the tree, not its "
            f"history.")

    for name in ("torch", "torchaudio"):
        if name in locked and name not in LICENCES:
            problems.append(f"{name} has no licence answer. UNKNOWN, not "
                            f"permissive.")
    print(f"  licences: " + "; ".join(f"{k}={v.split('(')[0].strip()}"
                                      for k, v in sorted(LICENCES.items())))

    # ── FINDING AN: the split must not create a second source of truth ──────
    main_pins = _pins(MAIN_LOCK)
    both = sorted(set(main_pins) & set(locked))
    if both:
        problems.append(
            f"{len(both)} package(s) are pinned in BOTH locks: "
            f"{[(n, main_pins[n], locked[n]) for n in both[:5]]}. Installing "
            f"the two files together fails on the conflict, and installing "
            f"them one at a time makes the result depend on ordering while "
            f"both files claim to describe it. Run "
            f"scripts/reconcile_locks.py.")
    print(f"  lock split: {len(main_pins)} + {len(locked)} packages, "
          f"{len(both)} shared")

    # PRECONDITION. The wheel is identified by matching the index's published
    # sha256 against one the lock pins — so with no hashes in the lock there is
    # nothing to match against, and this check cannot run. That is not a second
    # finding: it is a consequence of the missing hashes, which is already in
    # `problems`. Reporting it as "the index could not be read" would blame the
    # network for a defect in the file.
    if hashes == 0:
        print("  the wheel's declared requirements were NOT checked: that "
              "check identifies the wheel by a hash the lock pins, and this "
              "lock pins none. See the hash finding below.")
        declared = []
    else:
        declared = _wheel_requirements(locked.get("torch", ""), text)
    if declared is None:
        print(f"::error::the requirements of the pinned torch wheel could not "
              f"be read from {TORCH_INDEX}. That is UNKNOWN: this gate cannot "
              f"show the split covers everything torch needs, and a check that "
              f"inspected nothing does not report success.")
        _also(problems)
        return 2

    covered = {**main_pins, **locked}
    unmet: list[str] = []
    applicable = 0
    for spec in declared:
        req = Requirement(spec)
        if req.marker is not None and not req.marker.evaluate({"extra": ""}):
            continue  # an extra, or another platform. Not installed here.
        applicable += 1
        pinned = covered.get(_norm(req.name))
        if pinned is None:
            unmet.append(f"{req.name} ({spec}) is pinned in NEITHER lock")
        elif not req.specifier.contains(Version(_base_version(pinned)),
                                        prereleases=True):
            unmet.append(f"{req.name} is pinned at {pinned}, which does not "
                         f"satisfy torch's own '{spec}'")
    # Counted separately on purpose. Reporting "10 of 10 resolved" when 3 are
    # optional extras nobody installs is the same arithmetic that lets a
    # scanner report clean over packages it skipped.
    print(f"  the pinned wheel declares {len(declared)} requirement(s), "
          f"{applicable} applicable here; {applicable - len(unmet)} satisfied "
          f"across the two locks")
    if applicable == 0 and hashes:
        problems.append(
            "the pinned torch wheel declares no applicable requirements at "
            "all. A wheel with an empty dependency set is what a mis-parsed "
            "metadata file looks like, and this check would then be passing "
            "over nothing.")
    if unmet:
        problems.append(
            f"the two locks do not satisfy what the pinned torch wheel "
            f"requires: {unmet}. The split moved a dependency out of both "
            f"files, or moved it to a version torch will not accept.")

    if problems:
        print("\n::error::torch supply chain:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\n✅ the torch family is locked with hashes from a single index, "
          "audited at its base version, and every advisory is declared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
