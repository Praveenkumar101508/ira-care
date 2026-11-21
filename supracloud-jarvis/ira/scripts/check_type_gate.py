#!/usr/bin/env python3
"""P16-R6 — SEC-07: the type position, measured and ratcheted.

P16-01 recorded: **"mypy did not complete, so the type position is UNKNOWN"**,
and it stayed UNKNOWN for six batches.

It did not complete for a reason worth naming, because it is the same shape as
every other defect this programme has found:

    scripts/setup_vault.py: error: Source file found twice under different
    module names: "setup_vault" and "scripts.setup_vault"

**mypy exited 0.** Not because the tree type-checks — because it aborted on
module resolution before analysing anything. A tool that stopped at the front
door, reporting success. Run with `--explicit-package-bases
--namespace-packages` it completes and reports **703 errors across 196 files,
having checked 869 source files** — in CI, which is the reference environment
for that count. See below.

── The reference environment, and why it has to be named ──────────────────

**The error count depends on which packages are installed.** mypy resolves
attributes against the modules it can actually import, so an environment with
extra libraries reports a different number from one without. The first CI run
proved it: this gate was written against a development environment measuring
**705**, and CI — which installs only `requirements-test.txt` — measured
**703**. Neither is wrong. They are answers about different environments.

A ratchet over a number that moves with the environment is not a ratchet. So
**CI is the reference**, because it is the reproducible one: its packages are
pinned by a lockfile and a manifest in this repository. The ceiling is recorded
from there, and enforced there.

Outside CI the gate still runs, still reports the measurement, and still
enforces everything that is environment-INDEPENDENT — that mypy finished, and
that it analysed the files on disk. It does not enforce the ceiling, because a
developer's richer environment failing a ceiling recorded elsewhere would teach
people to ignore this gate, which is how a control dies.

── Why a ratchet and not a threshold ──────────────────────────────────────

703 errors cannot be fixed in a supply-chain batch, and a gate that demands
zero on day one gets switched off. So the count is a ceiling that may only ever
come DOWN: exceeding it fails, and dropping below it ALSO fails, with
instructions to lower the recorded number. A ratchet that is not tightened is a
threshold, and a threshold drifts.

── The rule that makes this honest ────────────────────────────────────────

**A falling error count is not evidence of improvement.** It is equally
consistent with a module no longer being analysed — an import that now fails,
a directory excluded, a `# type: ignore` at module scope, or a rename that
takes a file out of the discovered set. Each of those makes the number better
and the position worse.

So this gate records **`files_checked`** as well, and treats a DROP in it as a
failure regardless of the error count. It also derives the number of `.py`
files on disk independently and fails if mypy's checked count falls far below
it, so "the tool stopped finding the code" cannot pass as "the code got
cleaner".

That is the same property `check_advisory_gate.py` enforces with
`_MIN_CREDIBLE_AUDITED`, and the same one the container scan enforces by
counting what it could not fix.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

IRA_ROOT = Path(__file__).resolve().parents[1]
BASELINE = IRA_ROOT / ".mypy-baseline.json"

#: Directories excluded from the on-disk count for the sanity check below.
#: mypy is NOT told to skip these — they are simply not expected to appear in
#: its checked set, so counting them would make the comparison meaningless.
NOT_SOURCE = {".git", "__pycache__", ".mypy_cache", "node_modules", ".venv",
              "venv", ".next", "build", "dist"}

_SUMMARY = re.compile(
    r"Found (\d+) errors? in (\d+) files? \(checked (\d+) source files?\)")
_CLEAN = re.compile(r"Success: no issues found in (\d+) source files?")


def _on_disk() -> int:
    return sum(1 for path in IRA_ROOT.rglob("*.py")
               if not (set(path.relative_to(IRA_ROOT).parts) & NOT_SOURCE))


def _run() -> tuple[str, int]:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", ".", "--ignore-missing-imports",
         "--explicit-package-bases", "--namespace-packages"],
        cwd=IRA_ROOT, capture_output=True, text=True, timeout=3600)
    return proc.stdout, proc.returncode


def main() -> int:
    if not BASELINE.is_file():
        print(f"::error::{BASELINE.name} is missing. Without it a ratchet has "
              f"nothing to ratchet against.")
        return 2

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    max_errors = baseline.get("max_errors")
    min_files = baseline.get("min_files_checked")

    try:
        output, _ = _run()
    except FileNotFoundError:
        print("::error::mypy is not installed. The type position is UNKNOWN, "
              "which is NOT the same as clean.")
        return 2
    except subprocess.TimeoutExpired:
        print("::error::mypy timed out. UNKNOWN, not clean.")
        return 2

    match = _SUMMARY.search(output)
    clean = _CLEAN.search(output)
    if match:
        errors, files_with_errors, checked = (int(g) for g in match.groups())
    elif clean:
        errors, files_with_errors, checked = 0, 0, int(clean.group(1))
    else:
        # THE CASE THIS GATE EXISTS FOR. mypy exits 0 when it aborts on module
        # resolution, printing no summary at all — which is exactly how the
        # type position stayed UNKNOWN for six batches while a tool "ran".
        tail = output.strip()[-600:]
        print(f"::error::mypy produced no summary line, so it did not finish "
              f"analysing. This is how it failed at P16-01: a module "
              f"resolution error aborts the run and STILL EXITS 0. UNKNOWN, "
              f"not clean. Output:\n{tail}")
        return 2

    on_disk = _on_disk()
    print(f"TYPE: {errors} error(s) in {files_with_errors} file(s), "
          f"{checked} source files checked ({on_disk} .py on disk); "
          f"ceiling {max_errors}, floor {min_files} checked")

    problems: list[str] = []

    # THE REFERENCE ENVIRONMENT. See the module docstring: the error count moves
    # with the installed packages, so the ceiling is only meaningful where the
    # environment is pinned. CI is that place.
    reference = os.environ.get("CI", "").lower() in {"true", "1"}
    if not reference:
        print("  NOT the reference environment (CI is unset), so the ceiling "
              "is reported and not enforced — the count moves with the "
              "installed packages. Everything below this line still applies.")

    if reference and errors > max_errors:
        problems.append(
            f"{errors} type errors, above the recorded ceiling of "
            f"{max_errors}. The ratchet only goes down.")
    elif reference and errors < max_errors:
        problems.append(
            f"{errors} type errors, BELOW the recorded ceiling of "
            f"{max_errors} — good, and the ceiling must come down with it. "
            f"Set max_errors to {errors} in {BASELINE.name}. A ratchet that "
            f"is not tightened is a threshold, and a threshold drifts back.")
    elif not reference and errors > max_errors:
        print(f"  note: {errors} errors here, above the reference ceiling of "
              f"{max_errors}. Expected if this environment has packages CI "
              f"does not; investigate if it does not.")

    # A FALLING ERROR COUNT IS NOT EVIDENCE OF IMPROVEMENT. It is equally
    # consistent with code no longer being analysed.
    if checked < min_files:
        problems.append(
            f"mypy checked {checked} source files, below the recorded "
            f"{min_files}. Code has STOPPED being analysed — an import that "
            f"now fails, an exclusion, a module-scope ignore, or a rename. "
            f"Whatever the error count did, the position got worse.")

    # And an independent floor, derived rather than recorded, so that both
    # numbers moving together cannot hide it.
    if checked < on_disk * 0.8:
        problems.append(
            f"mypy checked {checked} files but there are {on_disk} .py files "
            f"on disk. It is not finding the code; a count that low is a "
            f"discovery failure, not a clean tree.")

    if problems:
        print("\n::error::type gate:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(f"\n✅ the type position is MEASURED: {errors} errors at or below the "
          f"ceiling, over {checked} files at or above the floor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
