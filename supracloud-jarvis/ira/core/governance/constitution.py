"""The Constitution Engine — evaluate a user's permanent rules against a request.

Pure and deterministic. Same rules plus same request always gives the same
answer, with no I/O, no clock beyond what is passed in, and no model anywhere
near it. That is what makes it testable exhaustively, and testable exhaustively
is what makes it trustworthy.

── How rules combine ────────────────────────────────────────────────────────

1. Every matching rule is collected.
2. They are ordered by priority (higher first), then specificity (more specific
   first), then by strictness.
3. The winner is the first rule after that ordering.
4. **Any matching DENY wins outright, regardless of priority.**

Step 4 is not redundant with the ordering, and it is the rule worth arguing
about. It means a user cannot accidentally defeat their own prohibition by
adding a higher-priority permissive rule later — if they wrote "never delete
files", a subsequent "allow file cleanup routine" does not silently overturn
it. The cost is that overriding a deny requires deleting or disabling the deny,
which is a deliberate act. That is the right trade for rules whose entire
purpose is to be hard to talk your way past.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from core.governance.effects import stricter_of
from core.governance.rules import ConstitutionRule
from core.interfaces.permission import ActionRequest, Effect, RiskLevel


@dataclass(frozen=True, slots=True)
class ConstitutionVerdict:
    """What the user's own rules say about a request.

    `effect` is None when NO rule matched. That is different from "the rules
    allow it": the Constitution simply has nothing to say, and the decision
    falls through to grants and the deployment policy. Collapsing the two into
    `ALLOW` would turn silence into permission, which is exactly the failure
    this type's shape prevents.
    """

    effect: Optional[Effect]
    reason: str
    matched: tuple[ConstitutionRule, ...] = ()
    deciding_rule: Optional[ConstitutionRule] = None

    @property
    def has_opinion(self) -> bool:
        return self.effect is not None


def _sort_key(rule: ConstitutionRule):
    # Negated so that higher priority and higher specificity sort first; final
    # term keeps the ordering total and deterministic for otherwise-equal rules.
    from core.governance.effects import strictness

    return (-rule.priority, -rule.specificity, strictness(rule.effect))


def evaluate(
    request: ActionRequest,
    rules: Iterable[ConstitutionRule],
    *,
    risk: RiskLevel = RiskLevel.MEDIUM,
) -> ConstitutionVerdict:
    """Apply the user's Constitution to a request."""
    matched: list[ConstitutionRule] = [
        rule for rule in rules if rule.matches(request, risk=risk)
    ]

    if not matched:
        return ConstitutionVerdict(
            effect=None,
            reason="No constitution rule applies to this action.",
        )

    matched.sort(key=_sort_key)

    # Any matching DENY wins outright — see the module docstring.
    denials = [r for r in matched if r.effect is Effect.DENY]
    if denials:
        rule = denials[0]
        return ConstitutionVerdict(
            effect=Effect.DENY,
            reason=(
                f"Your rule {rule.label!r} forbids this."
                if rule.label
                else f"A constitution rule forbids {request.domain}.{request.action}."
            ),
            matched=tuple(matched),
            deciding_rule=rule,
        )

    winner = matched[0]
    return ConstitutionVerdict(
        effect=winner.effect,
        reason=(
            f"Your rule {winner.label!r} applies."
            if winner.label
            else f"A constitution rule applies to {request.domain}.{request.action}."
        ),
        matched=tuple(matched),
        deciding_rule=winner,
    )


def combine_with(verdict: ConstitutionVerdict, other: Effect) -> Effect:
    """Fold a constitution verdict together with the grant/default effect.

    **The user's explicit rule takes precedence over the baseline** — in either
    direction — and the caller then clamps the result to the deployment ceiling.

    This is not the obvious "always take the stricter one", and the difference
    matters. Taking the stricter of the two would make every permissive rule
    effect (`allow`, `allow_for_session`, `allow_for_routine`) dead vocabulary:
    a rule can never be more permissive than a default that is already `deny`,
    so "let IRA read my calendar" would silently do nothing. A user would write
    a rule, see it listed in their Constitution, and find it had no effect —
    which is worse than not offering the option.

    Safety does not rest on this function being conservative. It rests on three
    things that happen around it:

      * a matching DENY is resolved by :func:`evaluate` BEFORE this is reached,
        and no permissive rule can overturn it;
      * the caller clamps to `tool_policies.max_effect`, so nothing here can
        exceed what the deployment permits at all;
      * critical-risk actions are blocked before rules are consulted.

    And the obvious objection — "could a prompt injection write itself a
    permissive rule?" — is answered structurally rather than here: there is no
    tool policy for writing constitution rules, so an agent attempting it is
    denied as an unclassified action. Rules are written only through the
    authenticated user endpoint.
    """
    if not verdict.has_opinion:
        return other
    assert verdict.effect is not None  # narrowed by has_opinion
    return verdict.effect


def summarise(rules: Sequence[ConstitutionRule]) -> list[str]:
    """Plain-language lines for the Privacy Centre and the rule list.

    Reads back the STRUCTURE, not the stored label, so what a user sees is what
    the engine will actually do. A label that has drifted from its rule — say,
    edited in the database — shows up here as a discrepancy rather than being
    faithfully echoed back as if it were the truth.
    """
    lines: list[str] = []
    for rule in sorted(rules, key=_sort_key):
        scope = f"{rule.domain}.{rule.action}".replace("*.*", "anything")
        verb = {
            Effect.DENY: "Never",
            Effect.REQUIRE_CONFIRMATION: "Always ask before",
            Effect.ALLOW: "Always allow",
            Effect.ALLOW_FOR_SESSION: "Allow for the session",
            Effect.ALLOW_FOR_ROUTINE: "Allow inside approved routines",
        }.get(rule.effect, str(rule.effect.value))
        condition = ""
        if rule.conditions:
            bits = []
            if "resource_prefix" in rule.conditions:
                bits.append(f"under {rule.conditions['resource_prefix']}")
            if "sensitivity_in" in rule.conditions:
                bits.append(f"tagged {', '.join(rule.conditions['sensitivity_in'])}")
            if "max_risk" in rule.conditions:
                bits.append(f"at {rule.conditions['max_risk']} risk or below")
            if bits:
                condition = " (" + "; ".join(bits) + ")"
        state = "" if rule.enabled else "  [disabled]"
        lines.append(f"{verb}: {scope}{condition}{state}")
    return lines


__all__ = ["ConstitutionVerdict", "combine_with", "evaluate", "summarise"]
