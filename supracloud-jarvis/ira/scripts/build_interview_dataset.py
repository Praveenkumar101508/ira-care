#!/usr/bin/env python3
"""
Build IRA's interview-question dataset from the upstream company-wise CSV dump.

Upstream ships one directory per company holding five CSVs — one per recency
window — and every row repeats the question's title, URL, difficulty and
acceptance rate. Across 656 companies that is 41,001 rows and 14 MB to say 3,358
distinct things.

This normalises it into two tables:

  questions  one row per LeetCode problem, held once
  companies  company → window → [(question id, frequency %)]

which is ~760 KB of JSON — small enough to live in the repository, so IRA
answers from disk with no network call and no setup step.

Stored uncompressed on purpose: a gzip blob is unreviewable in a pull request and
is uniformly high-entropy, which is exactly what a secret scanner is built to
flag. Plain JSON costs ~580 KB more and is worth it.

Run it to refresh the snapshot:

    git clone --depth 1 https://github.com/codenihar/leetcode-companywise-interview-questions /tmp/lc
    python scripts/build_interview_dataset.py /tmp/lc

Output is byte-for-byte reproducible from the same input — everything is sorted,
and nothing timestamped goes in — so re-running against an unchanged upstream
produces no diff, and a real refresh produces one you can read.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

#: Upstream file name → the window label IRA uses. The upstream names are prose
#: ("more-than-six-months"); these are what an API caller would type.
WINDOWS: dict[str, str] = {
    "thirty-days.csv": "30d",
    "three-months.csv": "3m",
    "six-months.csv": "6m",
    "more-than-six-months.csv": "6m+",
    "all.csv": "all",
}

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "data" / "interview" / "leetcode-companies.json"

SOURCE_URL = "https://github.com/codenihar/leetcode-companywise-interview-questions"


def _percent(raw: str | None) -> float:
    """'57.5%' → 57.5. Malformed or missing becomes 0.0 rather than an exception:
    one bad cell in 41,001 must not fail the whole build."""
    text = (raw or "").strip().rstrip("%")
    try:
        return round(float(text), 1)
    except ValueError:
        return 0.0


def _slug_from_url(url: str | None) -> str:
    """The problem slug, which is the stable half of a LeetCode URL.

    Stored instead of the full URL because the prefix is identical on all 3,358
    rows; the reader rebuilds it.
    """
    return (url or "").strip().rstrip("/").rsplit("/", 1)[-1]


def _upstream_commit(root: Path) -> str:
    """The upstream commit this snapshot came from, so the data is traceable."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return result.stdout.strip()[:40] if result.returncode == 0 else "unknown"
    except Exception:  # noqa: BLE001 — provenance is best-effort, not load-bearing
        return "unknown"


def build(root: Path) -> dict:
    """Read the upstream tree and return the normalised dataset."""
    questions: dict[int, list] = {}
    companies: dict[str, dict[str, list]] = {}
    skipped: list[str] = []

    company_dirs = sorted(
        p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    if not company_dirs:
        raise SystemExit(f"No company directories under {root} — is that the upstream clone?")

    for cdir in company_dirs:
        windows: dict[str, list] = {}
        for filename, window in WINDOWS.items():
            path = cdir / filename
            if not path.exists():
                continue                     # not every company has every window
            rows: list[list] = []
            with path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    try:
                        qid = int(row["ID"])
                    except (KeyError, TypeError, ValueError):
                        continue             # header drift or a blank line
                    if qid not in questions:
                        questions[qid] = [
                            qid,
                            _slug_from_url(row.get("URL")),
                            (row.get("Title") or "").strip(),
                            (row.get("Difficulty") or "").strip(),
                            _percent(row.get("Acceptance %")),
                        ]
                    rows.append([qid, _percent(row.get("Frequency %"))])
            if rows:
                # Sorted by descending frequency at build time so every reader
                # gets "what this company asks most" without re-sorting 41k rows.
                rows.sort(key=lambda r: (-r[1], r[0]))
                windows[window] = rows
        if windows:
            companies[cdir.name] = windows
        else:
            skipped.append(cdir.name)

    if skipped:
        print(f"note: {len(skipped)} directories held no readable CSV: {', '.join(skipped)}",
              file=sys.stderr)

    return {
        "meta": {
            "source": SOURCE_URL,
            "upstream_commit": _upstream_commit(root),
            "snapshot": "2026-05-24",
            "companies": len(companies),
            "questions": len(questions),
            "windows": sorted(set(WINDOWS.values())),
            "note": (
                "Normalised from the upstream per-company CSV dump. Frequency is "
                "upstream's own figure; IRA does not compute or verify it."
            ),
        },
        "questions": [questions[qid] for qid in sorted(questions)],
        "companies": {slug: companies[slug] for slug in sorted(companies)},
    }


def write(data: dict, out: Path) -> None:
    """Write the dataset one record per line.

    Not `json.dumps(indent=2)` — that would quadruple the file — and not one
    enormous line either, which would render a refresh as a single changed 760 KB
    row that no reviewer can read. One line per question and per company means a
    re-scrape shows up as the handful of companies that actually moved.
    """
    def dump(value) -> str:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)

    lines = ["{", f'"meta":{dump(data["meta"])},', '"questions":[']
    questions = data["questions"]
    lines += [dump(row) + ("," if i < len(questions) - 1 else "")
              for i, row in enumerate(questions)]
    lines += ["],", '"companies":{']
    slugs = list(data["companies"])
    lines += [f'{dump(slug)}:{dump(data["companies"][slug])}'
              + ("," if i < len(slugs) - 1 else "")
              for i, slug in enumerate(slugs)]
    lines += ["}", "}"]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"{out}: {len(data['companies'])} companies, {len(data['questions'])} questions, "
        f"{out.stat().st_size / 1e6:.2f} MB"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path,
                        help="Path to a clone of the upstream company-wise repository")
    parser.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT,
                        help=f"Output path (default: {DEFAULT_OUT})")
    args = parser.parse_args()

    if not args.source.is_dir():
        raise SystemExit(f"{args.source} is not a directory")
    write(build(args.source), args.out)


if __name__ == "__main__":
    main()
