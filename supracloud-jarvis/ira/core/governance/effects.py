"""How effects compare, and which way "safer" is.

Almost every bug class this package exists to prevent is really one mistake:
combining two answers and getting the more permissive one. So the comparison is
defined once, here, with an explicit ordering — not open-coded at each call site
with an `if` that looked right at the time.

The ordering, strictest first:

    deny
    require_confirmation
    allow_for_routine       (narrow: only inside a routine the user pre-approved)
    allow_for_session       (broader: the rest of this session)
    allow                   (broadest)

`allow_for_routine` sits BELOW `allow_for_session` in permissiveness because a
routine is a specific, previously-approved sequence, whereas a session is
everything the user does until they stop. "Only when running the backup routine
I already approved" grants less than "anything for the next hour".
"""
from __future__ import annotations

from typing import Final

from core.interfaces.permission import Effect, RiskLevel

#: Strictest (0) to most permissive. Used for both clamping and combining.
_STRICTNESS: Final[dict[Effect, int]] = {
    Effect.DENY: 0,
    Effect.REQUIRE_CONFIRMATION: 1,
    Effect.ALLOW_FOR_ROUTINE: 2,
    Effect.ALLOW_FOR_SESSION: 3,
    Effect.ALLOW: 4,
}

#: Risk, least to most severe. Mirrors actions/shadow_world.RISK_LEVELS — the
#: same vocabulary the existing Shadow World and Action Guard already use, not a
#: second scale that would have to be translated.
_RISK_ORDER: Final[dict[RiskLevel, int]] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def strictness(effect: Effect) -> int:
    """Lower is stricter. Unknown effects are treated as the strictest."""
    return _STRICTNESS.get(effect, 0)


def stricter_of(a: Effect, b: Effect) -> Effect:
    """The more restrictive of two effects.

    This is the function that makes "deny wins" true. Two rules that disagree
    resolve to the safer answer, never the more convenient one.
    """
    return a if strictness(a) <= strictness(b) else b


def clamp_to_ceiling(effect: Effect, ceiling: Effect) -> Effect:
    """Never let `effect` be more permissive than `ceiling`.

    The deployment's tool policy sets the ceiling. A user rule saying "always
    allow email sending" is clamped back to `require_confirmation` because the
    policy says that action can never be automatic.

    That direction is deliberate and is the opposite of most permission systems:
    a user's Constitution exists to CONSTRAIN their assistant. A rule engine
    that could also remove safety rails would be one prompt injection away from
    writing itself a permissive rule.
    """
    return stricter_of(effect, ceiling)


def risk_rank(risk: RiskLevel) -> int:
    return _RISK_ORDER.get(risk, _RISK_ORDER[RiskLevel.CRITICAL])


def higher_risk(a: RiskLevel, b: RiskLevel) -> RiskLevel:
    """The more severe of two risks.

    Risk is only ever raised, never lowered — the same rule
    `trust/action_guard.py` already enforces for its derived risk, so the two
    layers cannot disagree about which direction is safe.
    """
    return a if risk_rank(a) >= risk_rank(b) else b


def is_permissive(effect: Effect) -> bool:
    """True for effects that permit execution WITHOUT further approval.

    `require_confirmation` is NOT permissive. A caller that treats "ask the
    user" as "go ahead" is the single most likely way this system gets
    defeated, so the question has exactly one answer in exactly one place.
    """
    return effect in (Effect.ALLOW, Effect.ALLOW_FOR_SESSION, Effect.ALLOW_FOR_ROUTINE)


__all__ = [
    "clamp_to_ceiling",
    "higher_risk",
    "is_permissive",
    "risk_rank",
    "stricter_of",
    "strictness",
]
