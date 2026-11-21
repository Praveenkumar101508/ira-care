"""
interview/plan.py — turning a question bank into something you can actually work through.

A company's raw list is not a study plan. Amazon's "all" window is 1,000+ problems
ordered by frequency, which means the first forty are mostly Medium array
questions — drill them in order and you finish a week later having never seen a
graph.

So the planner does three things the raw list does not:

  * **Spreads difficulty.** A fixed Easy/Medium/Hard ratio, because a plan that is
    90% Medium trains one gear.
  * **Prefers breadth on ties.** Between two problems a company asks equally
    often, the one more companies ask is the better use of an evening — it pays
    off in every other interview too.
  * **Merges companies without double-counting.** Prepping for four companies at
    once should surface what they overlap on, and each problem once.

Everything it ranks with comes from the dataset. It invents no difficulty, no
topic and no frequency, and it says which numbers are upstream's.
"""
from __future__ import annotations

from typing import Optional, Sequence

from interview import dataset as ds

#: Default difficulty shape. Weighted to Medium because that is what most
#: on-site loops actually ask, with enough Easy to warm up and enough Hard to
#: stop the plan being comfortable.
DEFAULT_MIX: dict[str, float] = {"Easy": 0.25, "Medium": 0.55, "Hard": 0.20}

MAX_SIZE = 200


def build_plan(
    companies: Sequence[str],
    *,
    size: int = 25,
    window: Optional[str] = None,
    mix: Optional[dict[str, float]] = None,
) -> dict:
    """A ranked, difficulty-balanced practice list for one or more companies.

    Raises ``ValueError`` for an unusable request — no companies resolved, a bad
    window, a nonsense size — rather than returning a plan built on a guess.
    """
    if not companies:
        raise ValueError("Name at least one company to build a plan for.")
    if size < 1:
        raise ValueError("A plan needs at least one question.")
    size = min(size, MAX_SIZE)

    chosen_window = ds.resolve_window(window)

    resolved: list[str] = []
    unknown: list[str] = []
    for name in companies:
        slug = ds.resolve_company(name)
        (resolved.append(slug) if slug else unknown.append(name))
    # De-duplicate while keeping the order the owner asked in.
    resolved = list(dict.fromkeys(resolved))

    if not resolved:
        raise ValueError(
            f"None of {', '.join(companies)} is in the question bank. "
            "Try the exact company slug — /interview/companies lists them."
        )

    merged = ds.questions_for(resolved, chosen_window)
    if not merged:
        return _empty_plan(resolved, unknown, chosen_window)

    ratios = _normalise_mix(mix or DEFAULT_MIX)

    # Bucket by difficulty, each already ranked: frequency first, then how many
    # companies ask it, then id so the output is stable across runs.
    buckets: dict[str, list[tuple[int, float]]] = {level: [] for level in ds.DIFFICULTIES}
    for qid, freq in merged.items():
        row = ds.lookup(qid)
        if row and row[3] in buckets:
            buckets[row[3]].append((qid, freq))
    for level in buckets:
        buckets[level].sort(key=lambda item: (-item[1], -ds.company_count(item[0]), item[0]))

    picked = _take_by_mix(buckets, ratios, size)

    return {
        "companies": [{"slug": s, "name": ds.display_name(s)} for s in resolved],
        "unknown_companies": unknown,
        "window": chosen_window,
        "requested": size,
        "size": len(picked),
        "pool": len(merged),
        "by_difficulty": {
            level: sum(1 for q in picked if q["difficulty"] == level)
            for level in ds.DIFFICULTIES
        },
        "questions": picked,
        "notes": _notes(resolved, unknown, len(picked), size),
    }


def _normalise_mix(mix: dict[str, float]) -> dict[str, float]:
    """Scale a difficulty mix to sum to 1, refusing one that says nothing."""
    clean = {level: max(0.0, float(mix.get(level, 0.0))) for level in ds.DIFFICULTIES}
    total = sum(clean.values())
    if total <= 0:
        raise ValueError("The difficulty mix is all zeroes — nothing could be selected.")
    return {level: value / total for level, value in clean.items()}


def _take_by_mix(
    buckets: dict[str, list[tuple[int, float]]],
    ratios: dict[str, float],
    size: int,
) -> list[dict]:
    """Fill each difficulty to its share, then top up from whatever remains.

    The top-up matters: a small company may have four Hard problems total, and a
    plan that returns 22 of 25 because it insisted on the ratio is worse than one
    that quietly borrows from Medium and says so in the difficulty breakdown.
    """
    quotas = {level: int(size * ratio) for level, ratio in ratios.items()}
    for _ in range(size - sum(quotas.values())):     # distribute rounding loss
        level = max(ratios, key=lambda l: ratios[l] * size - quotas[l])
        quotas[level] += 1

    picked: list[dict] = []
    taken: set[int] = set()
    for level in ds.DIFFICULTIES:
        for qid, freq in buckets[level][: quotas[level]]:
            picked.append(_entry(qid, freq))
            taken.add(qid)

    if len(picked) < size:
        leftovers = [
            (qid, freq)
            for level in ds.DIFFICULTIES
            for qid, freq in buckets[level]
            if qid not in taken
        ]
        leftovers.sort(key=lambda item: (-item[1], -ds.company_count(item[0]), item[0]))
        for qid, freq in leftovers[: size - len(picked)]:
            picked.append(_entry(qid, freq))

    # Present in working order — hardest-earned first is discouraging, so lead
    # with what the companies ask most.
    picked.sort(key=lambda q: (-q["frequency"], -q["company_count"], q["id"]))
    return picked


def _entry(qid: int, freq: float) -> dict:
    row = ds.lookup(qid)
    return {
        "id": row[0],
        "slug": row[1],
        "title": row[2],
        "difficulty": row[3],
        "acceptance": row[4],
        "frequency": freq,
        "company_count": ds.company_count(qid),
        "url": ds.PROBLEM_URL.format(slug=row[1]),
    }


def _empty_plan(resolved: list[str], unknown: list[str], window: str) -> dict:
    return {
        "companies": [{"slug": s, "name": ds.display_name(s)} for s in resolved],
        "unknown_companies": unknown,
        "window": window,
        "requested": 0,
        "size": 0,
        "pool": 0,
        "by_difficulty": {level: 0 for level in ds.DIFFICULTIES},
        "questions": [],
        "notes": [f"No questions recorded for these companies in the {window} window."],
    }


def _notes(resolved: list[str], unknown: list[str], produced: int, requested: int) -> list[str]:
    """Anything the owner should know before trusting the list."""
    notes = ["Frequency and difficulty are upstream's figures; IRA does not compute them."]
    if unknown:
        notes.append(f"Not in the question bank, and excluded: {', '.join(unknown)}.")
    if produced < requested:
        notes.append(
            f"Only {produced} of {requested} could be filled — the bank holds no more "
            "for these companies in this window."
        )
    if len(resolved) > 1:
        notes.append(
            f"Merged across {len(resolved)} companies; each question appears once, "
            "ranked by its highest frequency at any of them."
        )
    return notes


__all__ = ["DEFAULT_MIX", "MAX_SIZE", "build_plan"]
