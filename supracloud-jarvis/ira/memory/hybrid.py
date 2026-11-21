"""
Reciprocal Rank Fusion — combining ranked lists from different retrievers.

Vector search and full-text search score on scales that cannot be compared: a
cosine similarity and a `ts_rank_cd` value have no common unit. RRF ignores the
scores and uses only each item's RANK in each list:

    score(d) = Σ over lists containing d of  1 / (k + rank(d))

with ranks starting at 1. An item near the top of both lists beats an item at
the top of one, and a larger `k` flattens the advantage of being first. `k = 60`
is the value from Cormack, Clarke & Büttcher (2009) and the default here.

Pure and synchronous, so the ordering rules are testable without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Fused:
    """One item after fusion, with where it came from."""

    key: object
    score: float
    ranks: dict[str, int] = field(default_factory=dict)   # retriever -> 1-based rank

    @property
    def retrievers(self) -> list[str]:
        return sorted(self.ranks)


def rrf_fuse(ranked: dict[str, list], *, k: int = 60) -> list[Fused]:
    """Fuse ``{retriever_name: [key, ...]}`` (best first) into one ranked list.

    A key repeated within one list counts once, at its best rank. Ties are
    broken by the best single rank, then by retriever name and first
    appearance, so the output is deterministic for identical input.
    """
    if k < 0:
        raise ValueError("RRF k must be non-negative")

    fused: dict[object, Fused] = {}
    order: dict[object, int] = {}
    for name in sorted(ranked):
        for rank, key in enumerate(ranked[name], start=1):
            item = fused.get(key)
            if item is None:
                item = fused[key] = Fused(key=key, score=0.0)
                order[key] = len(order)
            if name in item.ranks:
                continue
            item.ranks[name] = rank
            item.score += 1.0 / (k + rank)

    return sorted(
        fused.values(),
        key=lambda f: (-f.score, min(f.ranks.values()), order[f.key]),
    )


__all__ = ["Fused", "rrf_fuse"]
