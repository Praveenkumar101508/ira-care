"""
interview/dataset.py — loading and querying the question bank.

The file on disk is normalised (see ``scripts/build_interview_dataset.py``): one
row per problem, plus company → window → [(question id, frequency)]. This module
loads it once, builds the reverse index the normalised form deliberately omits,
and answers queries against it.

Design notes:

  * **Loaded lazily, cached forever.** Parsing and indexing 760 KB costs tens of
    milliseconds. Doing it at import time would tax every process that imports
    IRA for a feature most requests never touch; doing it per request would tax
    the ones that do.
  * **Sorted at build time, not read time.** Company rows arrive already ordered
    by descending frequency, so the common query — "top 20 for Stripe" — is a
    slice, not a sort of 41,001 rows.
  * **Missing data is a clear refusal.** If the file is absent the module raises
    :class:`DatasetUnavailable` naming the rebuild command, rather than returning
    empty results that read as "this company asks nothing".
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("ira.interview")

DATASET_PATH = Path(__file__).resolve().parent.parent / "data" / "interview" / "leetcode-companies.json"

#: Recency windows, narrowest first. Order is the UI's order too.
WINDOWS = ("30d", "3m", "6m", "6m+", "all")
DEFAULT_WINDOW = "all"

DIFFICULTIES = ("Easy", "Medium", "Hard")

PROBLEM_URL = "https://leetcode.com/problems/{slug}"

#: Slug fragments whose title-case form would be wrong. Display names are derived
#: from directory slugs upstream, so this is presentation only — the slug stays
#: the identifier and no lookup depends on getting a name pretty.
_ACRONYMS = {
    "ibm", "amd", "adp", "sap", "hp", "hcl", "tcs", "ey", "kpmg", "pwc", "bny",
    "jp", "ubs", "aws", "gm", "ge", "3m", "att", "ai", "api", "it", "hsbc",
    "nvidia", "epam", "dbs", "rbc", "td", "bnp", "icici", "hdfc", "sbi", "lg",
    "zs", "mit", "nyc", "usaa", "cvs", "ups", "sas", "qa", "ml",
}


class DatasetUnavailable(RuntimeError):
    """The question bank is not on disk. Names the fix in its message."""


class _Bank:
    """The loaded dataset plus the indexes queries need."""

    def __init__(self, raw: dict) -> None:
        self.meta: dict = raw.get("meta", {})
        # qid → [qid, slug, title, difficulty, acceptance]
        self.questions: dict[int, list] = {row[0]: row for row in raw.get("questions", [])}
        self.by_slug: dict[str, list] = {row[1]: row for row in self.questions.values() if row[1]}
        # slug → window → [[qid, frequency], ...] (pre-sorted, descending)
        self.companies: dict[str, dict[str, list]] = raw.get("companies", {})

        # qid → sorted company slugs. Built here rather than stored: it is pure
        # derivation, and storing it would roughly double the file for no gain.
        asked_by: dict[int, set[str]] = {}
        for slug, windows in self.companies.items():
            for qid, _freq in windows.get("all", []) or _any_window(windows):
                asked_by.setdefault(qid, set()).add(slug)
        self.asked_by: dict[int, list[str]] = {
            qid: sorted(slugs) for qid, slugs in asked_by.items()
        }

        self.company_totals: dict[str, int] = {
            slug: len(windows.get("all") or _any_window(windows))
            for slug, windows in self.companies.items()
        }


def _any_window(windows: dict[str, list]) -> list:
    """The widest window a company actually has.

    Most companies carry ``all``; a few only have a narrower slice. Falling back
    keeps them queryable instead of silently reporting zero questions.
    """
    for window in reversed(WINDOWS):
        rows = windows.get(window)
        if rows:
            return rows
    return []


_BANK: Optional[_Bank] = None
_LOCK = threading.Lock()


def _bank() -> _Bank:
    """The loaded bank, inflating and indexing on first use."""
    global _BANK
    with _LOCK:
        if _BANK is not None:
            return _BANK
        if not DATASET_PATH.exists():
            raise DatasetUnavailable(
                f"The interview question bank is missing at {DATASET_PATH}. "
                "Rebuild it with: git clone --depth 1 "
                "https://github.com/codenihar/leetcode-companywise-interview-questions /tmp/lc "
                "&& python scripts/build_interview_dataset.py /tmp/lc"
            )
        try:
            with DATASET_PATH.open(encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception as exc:  # noqa: BLE001 — a corrupt file must name itself
            raise DatasetUnavailable(
                f"The interview question bank at {DATASET_PATH} could not be read: {exc}"
            ) from exc
        _BANK = _Bank(raw)
        logger.info(
            "interview bank loaded: %d companies, %d questions",
            len(_BANK.companies), len(_BANK.questions),
        )
        return _BANK


def reset_cache() -> None:
    """Test hook: drop the cached bank so the next call reloads."""
    global _BANK
    with _LOCK:
        _BANK = None


# ── Presentation ─────────────────────────────────────────────────────────────

def display_name(slug: str) -> str:
    """A human name derived from the upstream directory slug.

    Derived, not authoritative — upstream stores no company names, only folder
    names. Every API payload returns the slug alongside so nothing has to parse
    this back.
    """
    words = []
    for word in slug.replace("_", "-").split("-"):
        if not word:
            continue
        words.append(word.upper() if word.lower() in _ACRONYMS else word.capitalize())
    return " ".join(words) or slug


def _question_dict(row: list, *, frequency: Optional[float] = None) -> dict:
    payload = {
        "id": row[0],
        "slug": row[1],
        "title": row[2],
        "difficulty": row[3],
        "acceptance": row[4],
        "url": PROBLEM_URL.format(slug=row[1]),
    }
    if frequency is not None:
        payload["frequency"] = frequency
    return payload


def resolve_window(window: Optional[str]) -> str:
    """Validate a window label, or raise with the list of real ones."""
    chosen = (window or DEFAULT_WINDOW).strip()
    if chosen not in WINDOWS:
        raise ValueError(f"Unknown window {chosen!r}; expected one of {', '.join(WINDOWS)}")
    return chosen


def resolve_company(name: str) -> Optional[str]:
    """Map anything the owner might type to a company slug.

    Exact slug, then a slugified form of free text ("Goldman Sachs" →
    "goldman-sachs"), then a unique prefix. An ambiguous prefix returns None
    rather than guessing — picking one of four matching companies silently is how
    someone ends up drilling the wrong list.
    """
    text = (name or "").strip().lower()
    if not text:
        return None
    bank = _bank()
    if text in bank.companies:
        return text

    slugified = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if slugified in bank.companies:
        return slugified

    matches = [slug for slug in bank.companies if slug.startswith(slugified)]
    return matches[0] if len(matches) == 1 else None


# ── Queries ──────────────────────────────────────────────────────────────────

def stats() -> dict:
    """Dataset provenance and coverage — what every response is grounded in."""
    bank = _bank()
    return {
        **bank.meta,
        "companies": len(bank.companies),
        "questions": len(bank.questions),
        "windows": list(WINDOWS),
        "difficulties": list(DIFFICULTIES),
    }


def list_companies(query: str = "", limit: Optional[int] = None) -> list[dict]:
    """Companies in the bank, most questions first. ``query`` is a substring match."""
    bank = _bank()
    needle = (query or "").strip().lower().replace(" ", "-")
    rows = [
        {
            "slug": slug,
            "name": display_name(slug),
            "questions": bank.company_totals.get(slug, 0),
            "windows": [w for w in WINDOWS if bank.companies[slug].get(w)],
        }
        for slug in bank.companies
        if not needle or needle in slug
    ]
    rows.sort(key=lambda r: (-r["questions"], r["slug"]))
    return rows[:limit] if limit else rows


def company(name: str) -> Optional[dict]:
    """One company's summary: totals per window and difficulty mix."""
    slug = resolve_company(name)
    if slug is None:
        return None
    bank = _bank()
    windows = bank.companies[slug]

    mix = {level: 0 for level in DIFFICULTIES}
    for qid, _freq in windows.get("all") or _any_window(windows):
        row = bank.questions.get(qid)
        if row and row[3] in mix:
            mix[row[3]] += 1

    return {
        "slug": slug,
        "name": display_name(slug),
        "questions": bank.company_totals.get(slug, 0),
        "by_window": {w: len(windows.get(w, [])) for w in WINDOWS if windows.get(w)},
        "by_difficulty": mix,
    }


def company_questions(
    name: str,
    *,
    window: Optional[str] = None,
    difficulty: Optional[str] = None,
    min_frequency: float = 0.0,
    limit: int = 50,
    offset: int = 0,
) -> Optional[dict]:
    """What a company asks, most frequent first.

    Returns None for an unknown company so the caller can 404 rather than serve
    an empty list that reads like "they ask nothing".
    """
    slug = resolve_company(name)
    if slug is None:
        return None

    bank = _bank()
    chosen = resolve_window(window)
    rows = bank.companies[slug].get(chosen)
    if rows is None:
        # Asked for a window this company lacks — say so instead of falling back
        # to a different one and mislabelling the result.
        return {
            "company": display_name(slug), "slug": slug, "window": chosen,
            "available_windows": [w for w in WINDOWS if bank.companies[slug].get(w)],
            "total": 0, "returned": 0, "questions": [],
            "note": f"No data for the {chosen} window; try one of the available windows.",
        }

    wanted = (difficulty or "").strip().capitalize()
    if wanted and wanted not in DIFFICULTIES:
        raise ValueError(f"Unknown difficulty {difficulty!r}; expected {', '.join(DIFFICULTIES)}")

    selected = []
    for qid, freq in rows:                    # already sorted by frequency
        if freq < min_frequency:
            continue
        row = bank.questions.get(qid)
        if row is None or (wanted and row[3] != wanted):
            continue
        selected.append(_question_dict(row, frequency=freq))

    page = selected[offset: offset + limit] if limit else selected[offset:]
    return {
        "company": display_name(slug),
        "slug": slug,
        "window": chosen,
        "difficulty": wanted or None,
        "total": len(selected),
        "returned": len(page),
        "offset": offset,
        "questions": page,
        "frequency_note": "Frequency is upstream's figure; IRA does not compute it.",
    }


def question(identifier: str | int) -> Optional[dict]:
    """One problem by numeric id or slug, with every company known to ask it."""
    bank = _bank()
    row = None
    if isinstance(identifier, int) or str(identifier).isdigit():
        row = bank.questions.get(int(identifier))
    if row is None:
        row = bank.by_slug.get(str(identifier).strip().lower())
    if row is None:
        return None

    slugs = bank.asked_by.get(row[0], [])
    return {
        **_question_dict(row),
        "asked_by": [{"slug": s, "name": display_name(s)} for s in slugs],
        "company_count": len(slugs),
    }


def search(text: str, limit: int = 25) -> list[dict]:
    """Problems whose title contains ``text``, most widely asked first.

    Ranking by how many companies ask it is the useful order here: a title search
    is usually "which of these should I actually do", and breadth answers that.
    """
    needle = (text or "").strip().lower()
    if not needle:
        return []
    bank = _bank()
    hits = [row for row in bank.questions.values() if needle in row[2].lower()]
    hits.sort(key=lambda row: (-len(bank.asked_by.get(row[0], [])), row[0]))
    return [
        {**_question_dict(row), "company_count": len(bank.asked_by.get(row[0], []))}
        for row in hits[:limit]
    ]


def questions_for(company_slugs: Iterable[str], window: str = DEFAULT_WINDOW) -> dict[int, float]:
    """qid → best frequency across several companies. Used by the planner."""
    bank = _bank()
    chosen = resolve_window(window)
    merged: dict[int, float] = {}
    for slug in company_slugs:
        windows = bank.companies.get(slug)
        if not windows:
            continue
        for qid, freq in windows.get(chosen) or _any_window(windows):
            if freq > merged.get(qid, -1.0):
                merged[qid] = freq
    return merged


def lookup(qid: int) -> Optional[list]:
    """The raw question row, for callers that already hold an id."""
    return _bank().questions.get(qid)


def company_count(qid: int) -> int:
    """How many companies ask a given problem."""
    return len(_bank().asked_by.get(qid, []))


__all__ = [
    "DATASET_PATH",
    "DIFFICULTIES",
    "DEFAULT_WINDOW",
    "WINDOWS",
    "DatasetUnavailable",
    "company",
    "company_count",
    "company_questions",
    "display_name",
    "list_companies",
    "lookup",
    "question",
    "questions_for",
    "reset_cache",
    "resolve_company",
    "resolve_window",
    "search",
    "stats",
]
