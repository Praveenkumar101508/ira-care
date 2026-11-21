#!/usr/bin/env python3
"""P16-03 — DEP-01 and DEP-02: reproducibility, checked rather than described.

Two gates, reported independently so a failure names which invariant broke.

── DEP-01: the lock and the manifest must agree ────────────────────────────

`requirements.lock` carries a hash per artifact. A lockfile CI does not verify
is documentation: `requirements.txt` gets bumped, nobody regenerates, and the
lock silently describes a tree that no longer exists — which is exactly the
shape of the Phase 1 baselines this phase opened by disbelieving.

So: every pinned requirement must appear in the lock **at the same version**,
and the lock must actually carry hashes. A lock with no hashes is a pin list
with extra steps.

── DEP-02: "MUST match requirements.txt" must actually match ───────────────

`requirements-test.txt` carries that comment against several packages, each
with a stated reason — `bcrypt` at `4.0.1` because passlib 1.7.4's backend
self-test breaks on `>=4.1`, and the delegated-access tests hash for real.
The Phase 1 baseline called enforcing it a *"trivial test"*. It stayed trivial
and unwritten for fifteen phases.

**The markers are read from the file, never hard-coded.** A hard-coded list of
six names is correct until somebody adds a seventh, and then it is silently
incomplete — the same defect as P15-04's non-recursive glob, which let a whole
Terraform module escape every boundary check by being a new directory.

── The exclusions are named, not silent ────────────────────────────────────

`torch` and `torchaudio` are absent from the lock on purpose (see its header:
`uv` refuses to resolve across both indexes because the PyTorch index also
serves `requests`, and best-match across indexes is dependency confusion). They
are listed in `LOCK_EXCLUDED` with that reason, and a package excluded without
an entry there **fails this gate** — an exclusion nobody wrote down is an
exclusion nobody decided.
"""
from __future__ import annotations

import re
from pathlib import Path

IRA_ROOT = Path(__file__).resolve().parents[1]
REQS = IRA_ROOT / "requirements.txt"
TEST_REQS = IRA_ROOT / "requirements-test.txt"
LOCK = IRA_ROOT / "requirements.lock"

#: Packages deliberately outside the lock, and why. Absence from the lock is
#: only acceptable for a name in here.
LOCK_EXCLUDED = {
    "torch": ("locked SEPARATELY in requirements-torch.lock, not unlocked. "
              "P16-R2 resolves the torch family from a single index "
              "(download.pytorch.org/whl/cpu) with --generate-hashes, so "
              "cross-index dependency confusion is structurally impossible "
              "rather than merely disabled. Absent from THIS lock because a "
              "`+cpu` local version is not on PyPI; its advisory, checksum and "
              "licence position are all enforced by "
              "scripts/check_torch_supply_chain.py."),
    "torchaudio": ("as torch, and it MUST match torch exactly or pip hits "
                   "ResolutionImpossible — see the note in requirements.txt."),
}

#: `name==version`, `name[extra]==version`, `name[a,b] == version`.
#:
#: The extras group is not optional decoration — P16-05 (FINDING AL) found this
#: pattern without it, which meant `uvicorn[standard]`, `psycopg[binary]`,
#: `python-jose[cryptography]` and `passlib[bcrypt]` matched NOTHING and were
#: therefore never compared against the lock. DEP-01 reported success over 52
#: of 56 pinned requirements and said nothing about the other four; two of them
#: are the cryptography and password-hashing stacks.
#:
#: `_must_match_markers` uses the same pattern, so a MUST-match marker written
#: against a package with an extra would have been dropped just as quietly.
#: None is today — all six markers are on plain names — which is precisely why
#: this was invisible: the hole was in the part nobody had reached yet.
#:
#: Same defect class as P15-04's non-recursive glob and P16-01's `pip-licenses`
#: run: a real check, correctly implemented, silently covering less than it
#: appeared to.
_PIN = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;#\\]+)"
)

#: Requirements given a RANGE rather than an exact pin. Reproducibility for
#: these rests entirely on the lock; the manifest permits a different artifact.
_RANGE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*[<>!~]=?")


#: The platform the locks describe: the Linux tree CI and the container build
#: install. `requirements.txt` also carries a macOS variant of the torch pair
#: (`torch==2.11.0 ; sys_platform == "darwin"`), because the `+cpu` wheels do
#: not exist for macOS. Keyed by name, that second line would silently REPLACE
#: the Linux pin in `_pins` — so a line whose marker excludes this platform is
#: not part of the tree the gate checks, and is skipped rather than merged.
_LOCK_PLATFORM = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix"}


def _applies_to_lock_platform(line: str) -> bool:
    """False only for a requirement whose environment marker excludes Linux."""
    spec = line.split("#", 1)[0]
    if ";" not in spec:
        return True
    from packaging.markers import Marker
    return Marker(spec.split(";", 1)[1].strip()).evaluate(_LOCK_PLATFORM)


def _norm(name: str) -> str:
    """PEP 503 normalisation — `Foo_Bar` and `foo-bar` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(path: Path) -> dict[str, tuple[str, int]]:
    """`{normalised name: (version, line number)}` for every `name==version`."""
    out: dict[str, tuple[str, int]] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#") or line.lstrip().startswith("-"):
            continue
        if not _applies_to_lock_platform(line):
            continue
        m = _PIN.match(line)
        if m:
            out[_norm(m.group(1))] = (m.group(2), n)
    return out


def _ranges(path: Path) -> set[str]:
    """Names given a range specifier rather than `==`."""
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#") or line.lstrip().startswith("-"):
            continue
        if not _applies_to_lock_platform(line):
            continue
        if _PIN.match(line):
            continue
        m = _RANGE.match(line)
        if m:
            out.add(_norm(m.group(1)))
    return out


def _must_match_markers() -> dict[str, tuple[str, int]]:
    """Packages in requirements-test.txt carrying a MUST-match marker.

    Discovered by reading the marker, so a seventh entry is covered the day it
    is written rather than the day somebody remembers to update a list here.
    """
    out: dict[str, tuple[str, int]] = {}
    for n, line in enumerate(TEST_REQS.read_text(encoding="utf-8").splitlines(), 1):
        if "must match requirements.txt" not in line.lower():
            continue
        m = _PIN.match(line)
        if m:
            out[_norm(m.group(1))] = (m.group(2), n)
    return out


def check_lock() -> list[str]:
    """DEP-01."""
    problems: list[str] = []
    if not LOCK.is_file():
        return [f"{LOCK.name} is missing — nothing pins an artifact, only releases"]

    lock_text = LOCK.read_text(encoding="utf-8")
    hashes = lock_text.count("--hash=sha256:")
    if hashes == 0:
        problems.append(
            f"{LOCK.name} carries NO hashes. A lock without hashes is a pin "
            f"list with extra steps — it names releases, not artifacts.")

    locked = _pins(LOCK)
    declared = _pins(REQS)

    for name, (version, line) in sorted(declared.items()):
        if name in locked:
            if locked[name][0] != version:
                problems.append(
                    f"{name}: requirements.txt:{line} pins {version}, the lock "
                    f"records {locked[name][0]}. Regenerate the lock — see its "
                    f"header — or the lock describes a tree that no longer "
                    f"exists.")
        elif name not in LOCK_EXCLUDED:
            problems.append(
                f"{name} (requirements.txt:{line}) is not in the lock and is "
                f"not in LOCK_EXCLUDED. An exclusion nobody wrote down is an "
                f"exclusion nobody decided.")

    # An excluded package must be ABSENT, not merely allowed to differ.
    #
    # This gate's first run caught exactly that: stripping torch/torchaudio
    # from the resolver INPUT did not keep them out of the OUTPUT — they came
    # back transitively, unpinned, at 2.13.0 and 2.11.0 while the deployment
    # installs 2.4.1+cpu. A lock quietly naming a different major version of
    # the largest package in the tree is worse than no lock, because it looks
    # authoritative. Absence is the assertion; "present at some version" is not.
    for name in sorted(LOCK_EXCLUDED):
        if name in locked:
            problems.append(
                f"{name} is declared excluded but appears in the lock at "
                f"{locked[name][0]}, while requirements.txt pins "
                f"{declared.get(name, ('?',))[0]}. It re-entered as a "
                f"transitive dependency and resolved from PyPI. Strip it from "
                f"the lock — see the regeneration note in its header.")

    stale = sorted(set(LOCK_EXCLUDED) - set(declared))
    if stale:
        problems.append(
            f"LOCK_EXCLUDED names {stale}, which requirements.txt no longer "
            f"pins. Remove them so the exclusion list keeps describing the "
            f"tree rather than its history.")

    # ORPHANS — FINDING AM, and the rule that would have caught it on day one.
    #
    # Excluding a package from the lock does not exclude its dependencies. The
    # resolver pulls torch (sentence-transformers requires it), the strip
    # removed the two package blocks, and 25 packages stayed behind that
    # nothing else needed — the entire NVIDIA CUDA stack among them, in a lock
    # whose whole point is `+cpu`.
    #
    # A locked package whose every parent is excluded is in the lock for no
    # reason. Computed from the lock's own `# via` annotations, so it needs no
    # list to maintain.
    via = re.compile(r"^\s+#\s+(?:via\s+)?([A-Za-z0-9][A-Za-z0-9._-]*)\s*$", re.M)
    excluded = {_norm(n) for n in LOCK_EXCLUDED}
    orphans = []
    for block in re.split(r"\n(?=[A-Za-z0-9][A-Za-z0-9._-]*==)", lock_text):
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==", block)
        if not m:
            continue
        parents = {_norm(p) for p in via.findall(block)} - {"via"}
        if parents and parents <= excluded:
            orphans.append(_norm(m.group(1)))
    if orphans:
        problems.append(
            f"{len(orphans)} locked package(s) have NO parent outside the "
            f"excluded set {sorted(excluded)}: {sorted(orphans)[:10]}. They are "
            f"in the lock only because an excluded package pulled them, and "
            f"installing this lock would install them. Run "
            f"scripts/strip_torch_subtree.py after regenerating.")

    print(f"DEP-01: {len(declared)} pinned in requirements.txt, "
          f"{len(locked)} in the lock, {hashes} hashes, "
          f"{len(LOCK_EXCLUDED)} declared exclusion(s):")
    for name, why in sorted(LOCK_EXCLUDED.items()):
        print(f"    {name}: {why[:100]}...")

    # Printed, not failed: a range is a deliberate choice here, and the lock
    # does pin the artifact. But a reader comparing "pinned" against the file's
    # line count deserves to be told the difference rather than left to infer
    # it from a number that quietly excludes them.
    ranged = sorted(_ranges(REQS))
    if ranged:
        print(f"  {len(ranged)} requirement(s) carry a RANGE, not an exact pin "
              f"— reproducibility for these rests entirely on the lock: "
              f"{', '.join(ranged)}")
    return problems


def check_cross_manifest() -> list[str]:
    """DEP-02."""
    problems: list[str] = []
    markers = _must_match_markers()
    if not markers:
        return ["requirements-test.txt carries no MUST-match markers at all. "
                "Either they were removed — in which case the invariant they "
                "protected is now unprotected — or the marker wording changed "
                "and this check silently stopped covering anything."]

    declared = _pins(REQS)
    for name, (version, line) in sorted(markers.items()):
        if name not in declared:
            problems.append(
                f"{name} (requirements-test.txt:{line}) says it MUST match "
                f"requirements.txt, which does not pin it at all.")
        elif declared[name][0] != version:
            problems.append(
                f"{name}: requirements-test.txt:{line} pins {version}, "
                f"requirements.txt:{declared[name][1]} pins "
                f"{declared[name][0]}. The comment says these MUST match; they "
                f"have drifted.")

    # EVERY shared pin must agree — not only the marked ones.
    #
    # P16-R1b: the `test` CI job installs requirements-test.txt, NOT
    # requirements.txt. Seven pins had drifted, including fastapi 0.115.5 vs
    # 0.141.1, so the unit suite was validating a different dependency tree than
    # production — and that is how a framework upgrade got "verified" against
    # the framework it replaced, with CI green throughout.
    #
    # The MUST-match markers still carry the REASON a particular pin matters.
    # They were never sufficient as the mechanism: a marker covers the package
    # somebody remembered to annotate, and says nothing about the other
    # eighteen. Same shape as FINDING AL — a check that covered less than it
    # appeared to.
    test_pins = _pins(TEST_REQS)
    shared = sorted(set(declared) & set(test_pins))
    drifted_any = [f"{n} (requirements.txt {declared[n][0]}, "
                   f"requirements-test.txt {test_pins[n][0]})"
                   for n in shared if declared[n][0] != test_pins[n][0]]
    if drifted_any:
        problems.append(
            f"{len(drifted_any)} package(s) are pinned in BOTH manifests at "
            f"different versions: {drifted_any}. The test job installs "
            f"requirements-test.txt, so every one of these is a package the "
            f"suite does not actually exercise at its production version.")

    print(f"DEP-02: {len(markers)} MUST-match marker(s), read from the file: "
          f"{', '.join(sorted(markers))}")
    print(f"        {len(shared)} pin(s) shared by both manifests, all agreeing")
    return problems


def main() -> int:
    failures = []
    for label, fn in (("DEP-01 (lockfile)", check_lock),
                      ("DEP-02 (cross-manifest)", check_cross_manifest)):
        found = fn()
        if found:
            failures.append((label, found))

    if failures:
        for label, found in failures:
            print(f"\n::error::{label}:")
            for problem in found:
                print(f"  - {problem}")
        return 1

    print("\n✅ the lock agrees with the manifest, and the MUST-match pins match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
