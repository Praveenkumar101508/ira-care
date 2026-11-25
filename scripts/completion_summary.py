"""Weighted completion percentages, computed from docs/progress/COMPLETION_REGISTER.md.

The percentages are never typed by hand: this script reads each row's status cell and applies
the fixed scores and weights below. `--write` refreshes the block between the markers in the
register; `supracloud-jarvis/ira/tests/readiness/test_completion_register.py` fails if the
block and the rows disagree, or if a row's status uses a word outside the register's list.

Scores (how far a row has got; the strongest status word in its cell counts):
    not started 0 · in progress 0.25 · pre-existing 0.5 (built before, not re-verified)
    implemented 0.6 · unit tested 0.8 · trained 0.7 · evaluated 0.85
    integration / browser / emulator tested 0.9 · device tested 1.0
"needs external" alone scores 0; next to another word it marks the row as blocked outside
the software, and is counted separately.

Section weights (sum 100): C safety remediation 30 · D platform 10 · E OS 15 · F AI 20 ·
G server 10 · H, I hardware and compliance preparation 5 · J, K, L demo, verification,
evidence 10. Rows weigh equally within a section.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REGISTER = Path(__file__).resolve().parents[1] / "docs" / "progress" / "COMPLETION_REGISTER.md"
BEGIN, END = "<!-- completion-summary:begin -->", "<!-- completion-summary:end -->"

SCORES = {
    "device tested": 1.0, "integration tested": 0.9, "browser tested": 0.9, "emulator tested": 0.9,
    "evaluated": 0.85, "unit tested": 0.8, "trained": 0.7, "implemented": 0.6,
    "pre-existing": 0.5, "in progress": 0.25, "not started": 0.0,
}
WEIGHTS = {"C": 30, "D": 10, "E": 15, "F": 20, "G": 10, "HI": 5, "JKL": 10}
TESTED = {"device tested", "integration tested", "browser tested", "emulator tested", "evaluated", "unit tested"}


def _section(heading: str) -> str | None:
    m = re.match(r"## ([A-L](?:, [A-L])*)\.", heading)
    return "".join(x.strip() for x in m.group(1).split(",")) if m else None


def rows(text: str) -> list[dict]:
    out, sec = [], None
    for line in text.splitlines():
        if line.startswith("## "):
            sec = _section(line)
            continue
        if sec in WEIGHTS and re.match(r"^\| [A-L][-\w–]* \|", line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            status = cells[2].lower().replace("*", "")
            words = [w for w in SCORES if w in status]
            external = "needs external" in status or "needs external" in cells[3].lower().replace("*", "")
            if not words and "needs external" not in status:
                raise ValueError(f"row {cells[0]}: no status word in {cells[2]!r}")
            best = max(words, key=lambda w: SCORES[w]) if words else None
            out.append({"id": cells[0], "section": sec, "status": best, "score": SCORES[best] if best else 0.0,
                        "external": external, "programme": bool(re.search(r"#(4[4-9]|[5-9]\d)\b", cells[2] + " " + cells[3]))})
    return out


def summary(text: str) -> dict:
    rs = rows(text)
    by = {s: [r for r in rs if r["section"] == s] for s in WEIGHTS}

    def weighted(pred=None, value=lambda r: r["score"]) -> float:
        total = 0.0
        for s, w in WEIGHTS.items():
            if by[s]:
                total += w * sum((value(r) if pred is None or pred(r) else 0.0) for r in by[s]) / len(by[s])
        return round(total, 1)

    return {
        "rows": len(rs),
        "overall": weighted(),
        "implemented": weighted(value=lambda r: 1.0 if r["score"] >= SCORES["pre-existing"] else 0.0),
        "tested": weighted(value=lambda r: 1.0 if r["status"] in TESTED else 0.0),
        "merged_in_programme": weighted(value=lambda r: 1.0 if r["programme"] and r["score"] >= SCORES["implemented"] else 0.0),
        "ai": round(100 * sum(r["score"] for r in by["F"]) / len(by["F"]), 1) if by["F"] else 0.0,
        "os": round(100 * sum(r["score"] for r in by["E"]) / len(by["E"]), 1) if by["E"] else 0.0,
        "safety": round(100 * sum(r["score"] for r in by["C"]) / len(by["C"]), 1) if by["C"] else 0.0,
        "needs_external": sum(r["external"] for r in rs),
    }


def block(text: str) -> str:
    s = summary(text)
    return "\n".join([
        BEGIN,
        "| Measure | Value |",
        "|---|---|",
        f"| Overall, weighted | {s['overall']} % |",
        f"| Implemented (built, here or before) | {s['implemented']} % |",
        f"| Tested (unit, integration, evaluated or better) | {s['tested']} % |",
        f"| Merged and evidenced in this programme | {s['merged_in_programme']} % |",
        f"| Safety remediation (section C) | {s['safety']} % |",
        f"| AI components (section F) | {s['ai']} % |",
        f"| IRA CARE OS (section E) | {s['os']} % |",
        f"| Rows blocked in part by an external dependency | {s['needs_external']} of {s['rows']} |",
        "",
        "Computed by `scripts/completion_summary.py` from the status column, with the fixed scores and",
        "weights stated in that script. No device has been used, so no row is device tested.",
        END,
    ])


def main() -> int:
    text = REGISTER.read_text()
    new = block(text)
    if "--write" in sys.argv:
        if BEGIN in text:
            text = text[:text.index(BEGIN)] + new + text[text.index(END) + len(END):]
        else:
            text = text.replace("## Pull requests in this programme", "## Weighted completion\n\n" + new + "\n\n## Pull requests in this programme", 1)
        REGISTER.write_text(text)
    print(new)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
