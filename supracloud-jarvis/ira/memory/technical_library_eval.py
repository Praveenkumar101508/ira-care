"""
Retrieval-quality evaluation for the Technical Library.

A golden set is JSONL, one question per line::

    {"question": "...", "expected_title": "SQL Guide", "expected_page": 12, "answerable": true}
    {"question": "...", "expected_doc_id": "<sha256>", "expected_page": null, "answerable": true}
    {"question": "...", "answerable": false}

``expected_doc_id`` (preferred — it survives a rename) or ``expected_title``
names the source; ``expected_page`` narrows it to a page, or is null to accept
any chunk of that document. ``answerable: false`` marks a question the library
must NOT answer.

Metrics, all over ANSWERABLE questions unless stated:

* hit@1 / hit@5 — share with a matching chunk in the top 1 / top 5
* MRR           — mean of 1/rank of the first matching chunk (0 if none)
* citation accuracy (with answering) — share of grounded answers citing the
  expected source
* correct refusal (unanswerable questions) — with answering, share refused;
  without, share for which retrieval returned nothing. The second is a proxy
  and is labelled as one.
* latency p50 / p95 — milliseconds per retrieval (and per answer)

The functions here are pure; the DB-driving loop lives in
``scripts/eval_technical_library.py`` so this module is testable offline.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class GoldenItem:
    question: str
    answerable: bool
    expected_doc_id: str | None = None
    expected_title: str | None = None
    expected_page: int | None = None


def load_golden(path: Path | str) -> list[GoldenItem]:
    """Parse a golden JSONL file. Raises ValueError naming the bad line."""
    items = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except ValueError as e:
            raise ValueError(f"{path}:{n}: not JSON ({e})") from e
        if "question" not in raw or "answerable" not in raw:
            raise ValueError(f"{path}:{n}: needs 'question' and 'answerable'")
        answerable = bool(raw["answerable"])
        if answerable and not (raw.get("expected_doc_id") or raw.get("expected_title")):
            raise ValueError(f"{path}:{n}: an answerable question needs expected_doc_id or expected_title")
        items.append(GoldenItem(
            question=str(raw["question"]),
            answerable=answerable,
            expected_doc_id=raw.get("expected_doc_id") or None,
            expected_title=raw.get("expected_title") or None,
            expected_page=raw.get("expected_page"),
        ))
    return items


def matches(item: GoldenItem, chunk: dict) -> bool:
    """Whether a retrieved chunk is the source the golden item expects."""
    if item.expected_doc_id:
        same_doc = chunk.get("doc_id") == item.expected_doc_id
    else:
        same_doc = (chunk.get("title") or "").strip().lower() == (item.expected_title or "").strip().lower()
    if not same_doc:
        return False
    return item.expected_page is None or chunk.get("page") == item.expected_page


def first_match_rank(item: GoldenItem, chunks: list[dict]) -> int | None:
    for rank, chunk in enumerate(chunks, start=1):
        if matches(item, chunk):
            return rank
    return None


def _norm_label(label: str | None) -> str:
    return " ".join((label or "").lower().replace("p. ", "p.").split())


def cites_expected(item: GoldenItem, answer: dict) -> bool:
    """Whether a grounded answer CITES the expected source (not merely retrieved it)."""
    if not answer.get("grounded"):
        return False
    cited = {_norm_label(c) for c in answer.get("citations", [])}
    for src in answer.get("sources", []):
        if _norm_label(src.get("citation")) in cited and matches(item, src):
            return True
    return False


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


@dataclass
class ModeResult:
    """Everything measured for one retrieval mode."""

    mode: str
    ranks: list[int | None] = field(default_factory=list)          # answerable only
    empty_on_unanswerable: list[bool] = field(default_factory=list)
    retrieval_ms: list[float] = field(default_factory=list)
    refused_on_unanswerable: list[bool] = field(default_factory=list)
    cited_expected: list[bool] = field(default_factory=list)       # answerable & grounded
    answer_ms: list[float] = field(default_factory=list)
    answered: bool = False

    def summary(self) -> dict:
        n = len(self.ranks)
        hit = lambda k: (sum(1 for r in self.ranks if r is not None and r <= k) / n) if n else None  # noqa: E731
        out = {
            "mode": self.mode,
            "answerable_questions": n,
            "unanswerable_questions": len(self.empty_on_unanswerable),
            "hit@1": _round(hit(1)),
            "hit@5": _round(hit(5)),
            "mrr": _round(sum(1 / r for r in self.ranks if r) / n) if n else None,
            "retrieval_p50_ms": _round(percentile(self.retrieval_ms, 50), 1),
            "retrieval_p95_ms": _round(percentile(self.retrieval_ms, 95), 1),
        }
        if self.answered:
            out["correct_refusal_rate"] = _rate(self.refused_on_unanswerable)
            out["correct_refusal_basis"] = "answer refused"
            out["citation_accuracy"] = _rate(self.cited_expected)
            out["answer_p50_ms"] = _round(percentile(self.answer_ms, 50), 1)
            out["answer_p95_ms"] = _round(percentile(self.answer_ms, 95), 1)
        else:
            out["correct_refusal_rate"] = _rate(self.empty_on_unanswerable)
            out["correct_refusal_basis"] = "retrieval returned nothing (proxy; run with --answer for the real rate)"
            out["citation_accuracy"] = None
        return out


def _rate(flags: list[bool]) -> float | None:
    return _round(sum(flags) / len(flags)) if flags else None


def _round(value, digits: int = 3):
    return None if value is None else round(value, digits)


def format_table(summaries: list[dict]) -> str:
    """A Markdown comparison table, one row per mode."""
    cols = ["mode", "hit@1", "hit@5", "mrr", "correct_refusal_rate",
            "citation_accuracy", "retrieval_p50_ms", "retrieval_p95_ms"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for s in summaries:
        lines.append("| " + " | ".join("—" if s.get(c) is None else str(s.get(c)) for c in cols) + " |")
    return "\n".join(lines)


__all__ = [
    "GoldenItem", "load_golden", "matches", "first_match_rank", "cites_expected",
    "percentile", "ModeResult", "format_table",
]
