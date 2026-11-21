"""
ira/reasoning/confidence.py — explicit confidence signalling for answers.

Distinguishes three states an answer can be in:

  GROUNDED     — supported by retrieved material that cleared the relevance bar
  INFERRED     — answered from the model's general knowledge, no strong support
  UNSUPPORTED  — retrieval found nothing AND the task expected grounding

Confidence is derived from signals that ALREADY exist — retrieval scores and the
rule-based verifier's findings. It is deliberately NOT a second model call and
never a self-report: asking a model "how confident are you?" is miscalibrated,
and text *inside* a retrieved chunk ("you are highly confident") must never be
able to raise the level. Only numbers do that.

This module reports and shapes. It never blocks an answer, never regenerates
one, and never makes a consent decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from reasoning.answer_policy import TASK_RESEARCH
from reasoning.answer_verifier import (
    ISSUE_MISSING_CITATION,
    ISSUE_TOO_VAGUE,
    VerificationResult,
)

# ── Levels ───────────────────────────────────────────────────────────────────

GROUNDED = "GROUNDED"
INFERRED = "INFERRED"
UNSUPPORTED = "UNSUPPORTED"

ALL_LEVELS = (GROUNDED, INFERRED, UNSUPPORTED)

#: Pseudo task type used by the self-knowledge path (see memory/self_knowledge.py).
TASK_SELF_KNOWLEDGE = "self_knowledge"
#: Pseudo task type for questions about verifiable facts the owner's records hold.
TASK_FACTUAL = "factual"

#: Task types where an ungrounded answer is a failure, not a style choice.
GROUNDING_EXPECTED_TASKS = frozenset({TASK_RESEARCH, TASK_SELF_KNOWLEDGE, TASK_FACTUAL})

#: Prefixed to an INFERRED answer when grounding was expected but not achieved.
HEDGE_PREFIX = "Based on general knowledge, not your records:"

#: Replaces an UNSUPPORTED answer. IRA does not fabricate to fill the gap.
UNSUPPORTED_ANSWER = "I don't have enough to answer that confidently."

#: Appended to UNSUPPORTED_ANSWER only when external search is actually enabled.
SEARCH_OFFER = "I can search the web for it if you'd like."


@dataclass(frozen=True)
class ConfidenceAssessment:
    """The derived confidence for one drafted answer."""

    level: str
    reason: str
    support_score: Optional[float] = None
    expects_grounding: bool = False
    hedge: Optional[str] = None
    #: False only for UNSUPPORTED — the caller should replace the draft rather
    #: than ship a guess. Never a hard block: the caller decides.
    should_answer: bool = True

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "reason": self.reason,
            "support_score": self.support_score,
            "expects_grounding": self.expects_grounding,
            "hedge": self.hedge,
        }


# ── Signal extraction ────────────────────────────────────────────────────────

def _best_support(retrieval: Optional[Sequence[dict]]) -> tuple[Optional[float], bool]:
    """Strongest support score across retrieved chunks, and whether it's a rerank score.

    Prefers the cross-encoder's ``rerank_score`` when present (it is the more
    discriminating signal); otherwise falls back to cosine ``similarity``. Chunk
    *content* is never read here — that is the whole point.
    """
    if not retrieval:
        return None, False
    reranked = [
        float(c["rerank_score"]) for c in retrieval
        if isinstance(c, dict) and c.get("rerank_score") is not None
    ]
    if reranked:
        return max(reranked), True
    sims = [
        float(c["similarity"]) for c in retrieval
        if isinstance(c, dict) and c.get("similarity") is not None
    ]
    if sims:
        return max(sims), False
    return None, False


def _thresholds() -> tuple[float, float]:
    """(min cosine, min rerank) for GROUNDED — configurable, never guessed."""
    try:
        from config import get_settings

        cfg = get_settings()
        return (
            float(getattr(cfg, "confidence_grounded_min_similarity", 0.65)),
            float(getattr(cfg, "confidence_grounded_min_rerank", 0.1)),
        )
    except Exception:  # noqa: BLE001 — config may be unset in bare contexts
        return 0.65, 0.1


def expects_grounding(task_type: str, *, has_retrieval_context: bool = False) -> bool:
    """True when an answer for ``task_type`` should be backed by sources."""
    return task_type in GROUNDING_EXPECTED_TASKS or has_retrieval_context


# ── Derivation ───────────────────────────────────────────────────────────────

def derive_confidence(
    *,
    task_type: str = "general",
    retrieval: Optional[Sequence[dict]] = None,
    verification: Optional[VerificationResult] = None,
    grounding_expected: Optional[bool] = None,
) -> ConfidenceAssessment:
    """Assign a confidence level from existing signals. No model call is made.

    ``retrieval`` is whatever the retriever returned (memory or self-knowledge
    chunks carrying ``similarity`` / ``rerank_score``). ``verification`` is the
    rule-based verifier's result for the drafted answer, used only to *lower*
    confidence — a missing citation on a citation-required task, or an answer so
    vague it isn't really an answer.
    """
    min_sim, min_rerank = _thresholds()
    support, is_rerank = _best_support(retrieval)
    wants_grounding = (
        expects_grounding(task_type) if grounding_expected is None else bool(grounding_expected)
    )

    supported = support is not None and (
        support >= min_rerank if is_rerank else support >= min_sim
    )

    if supported:
        # The verifier can pull a "supported" answer back down: a citation-required
        # task whose answer cites nothing is not actually grounded output.
        if verification is not None and verification.has(ISSUE_MISSING_CITATION):
            return ConfidenceAssessment(
                level=INFERRED,
                reason="retrieval support present but the answer cited no source",
                support_score=support,
                expects_grounding=wants_grounding,
                hedge=HEDGE_PREFIX if wants_grounding else None,
            )
        if verification is not None and verification.has(ISSUE_TOO_VAGUE):
            return ConfidenceAssessment(
                level=INFERRED,
                reason="retrieval support present but the answer is too vague to count as grounded",
                support_score=support,
                expects_grounding=wants_grounding,
                hedge=HEDGE_PREFIX if wants_grounding else None,
            )
        return ConfidenceAssessment(
            level=GROUNDED,
            reason=(
                f"top {'rerank' if is_rerank else 'similarity'} score "
                f"{support:.3f} cleared the relevance bar"
            ),
            support_score=support,
            expects_grounding=wants_grounding,
        )

    if wants_grounding:
        return ConfidenceAssessment(
            level=UNSUPPORTED,
            reason=(
                "no retrieved material cleared the relevance bar and this task "
                "type requires grounding"
            ),
            support_score=support,
            expects_grounding=True,
            should_answer=False,
        )

    return ConfidenceAssessment(
        level=INFERRED,
        reason="answered from general knowledge — no retrieval support",
        support_score=support,
        expects_grounding=False,
    )


# ── Shaping ──────────────────────────────────────────────────────────────────

def _search_offer_available() -> bool:
    try:
        from config import get_settings

        cfg = get_settings()
        return bool(getattr(cfg, "web_search_enabled", False))
    except Exception:  # noqa: BLE001
        return False


def apply_confidence(answer: str, assessment: ConfidenceAssessment) -> str:
    """Shape ``answer`` to match its confidence level.

    GROUNDED    → returned untouched (the caller shows sources)
    INFERRED    → hedge prefix when grounding was expected, otherwise untouched
    UNSUPPORTED → replaced with the honest refusal (plus a search offer only
                  when external search is actually enabled)
    """
    text = (answer or "").strip()

    if assessment.level == UNSUPPORTED:
        if _search_offer_available():
            return f"{UNSUPPORTED_ANSWER} {SEARCH_OFFER}"
        return UNSUPPORTED_ANSWER

    if assessment.level == INFERRED and assessment.hedge:
        if text.lower().startswith(assessment.hedge.lower()):
            return text
        return f"{assessment.hedge} {text}".strip()

    return text


def assess_and_shape(
    answer: str,
    *,
    task_type: str = "general",
    retrieval: Optional[Sequence[dict]] = None,
    verification: Optional[VerificationResult] = None,
    grounding_expected: Optional[bool] = None,
) -> tuple[str, ConfidenceAssessment]:
    """Derive confidence and return ``(shaped_answer, assessment)`` in one call."""
    assessment = derive_confidence(
        task_type=task_type,
        retrieval=retrieval,
        verification=verification,
        grounding_expected=grounding_expected,
    )
    return apply_confidence(answer, assessment), assessment


def summarise_sources(retrieval: Optional[Iterable[dict]]) -> list[dict]:
    """Compact, content-safe source list for an API payload / citation panel."""
    out: list[dict] = []
    for c in retrieval or []:
        if not isinstance(c, dict):
            continue
        out.append({
            "source": c.get("source_path") or c.get("source_type") or "memory",
            "similarity": c.get("similarity"),
            "rerank_score": c.get("rerank_score"),
            "excerpt": (c.get("content") or c.get("excerpt") or "")[:200],
        })
    return out


__all__ = [
    "GROUNDED",
    "INFERRED",
    "UNSUPPORTED",
    "ALL_LEVELS",
    "TASK_SELF_KNOWLEDGE",
    "TASK_FACTUAL",
    "GROUNDING_EXPECTED_TASKS",
    "HEDGE_PREFIX",
    "UNSUPPORTED_ANSWER",
    "SEARCH_OFFER",
    "ConfidenceAssessment",
    "derive_confidence",
    "apply_confidence",
    "assess_and_shape",
    "expects_grounding",
    "summarise_sources",
]
