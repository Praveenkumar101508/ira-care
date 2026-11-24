#!/usr/bin/env python3
"""Every row of docs/READINESS.md tells the truth in one of three states (round 3, phase 16).

    python scripts/check_docs_truth.py          # exit 1 with a list of offending rows

A row's State is exactly one of:
  works (proved by <what proves it>)     an automated test, a recorded run or a measured number
  needs hardware[ (<why>)]               built and unit-tested, never run on the real device
  needs the owner[ (<what only they can do>)]
A row that is partly one and partly another is two rows. Every path written in backticks in
the row's proof column must exist in the repository, so a proof cannot be a file that is not
there. The same rule is applied to docs/progress/FINAL_REPORT.md's tables when it exists.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = re.compile(r"^(works \(proved by .+\)|needs hardware( \(.+\))?|needs the owner( \(.+\))?)$")
PATH = re.compile(r"`([A-Za-z0-9_./\-\[\]@{}]+)`")
PATH_LIKE = re.compile(r"^(docs|scripts|supracloud-jarvis|dashboard|mobile|watch|deploy|e2e|tests|\.github)/|\.(md|py|ts|tsx|kt|json|sh|yml|yaml|svg|png|mp4|html|sql|glb|tflite)$")


def rows(path: Path):
    """Rows of the tables whose header has a State column; other tables are not readiness tables."""
    in_state_table = False
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.startswith("|"):
            in_state_table = False
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if "State" in cells:
            in_state_table = True
            continue
        if not in_state_table or set(line.replace("|", "").strip()) <= {"-", " "}:
            continue
        if len(cells) >= 3:
            yield n, cells


def check(path: Path) -> list[str]:
    problems = []
    for n, cells in rows(path):
        feature, state, proof = cells[0], cells[1], cells[2]
        if not STATE.match(state):
            problems.append(f"{path.relative_to(ROOT)}:{n}: state is not one of the three: {state!r} ({feature[:60]})")
        for p in PATH.findall(proof) + PATH.findall(state):
            candidate = p.split("::")[0].split("#")[0]
            pattern = candidate.replace("[id]", "*")
            if "/" in pattern:
                exists = any(ROOT.glob(pattern)) or any((ROOT / "supracloud-jarvis" / "ira").glob(pattern))
            else:  # a bare file name: anywhere in the repository outside dependency folders
                exists = any("node_modules" not in f.parts for f in ROOT.rglob(pattern))
            if PATH_LIKE.search(candidate) and not exists:
                problems.append(f"{path.relative_to(ROOT)}:{n}: proof names a file that is not there: {p}")
    return problems


def main() -> int:
    problems = []
    for name in ("docs/READINESS.md", "docs/progress/FINAL_REPORT.md"):
        f = ROOT / name
        if f.exists():
            problems += check(f)
    for p in problems:
        print(p)
    print(f"{len(problems)} problem(s)" if problems else "every row is in one of the three states and every proof exists")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
