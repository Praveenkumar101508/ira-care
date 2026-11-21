"""The User Constitution — structured rules, evaluated by ordinary code.

The transformation brief is explicit about this and it is worth restating,
because it is the whole differentiator:

    These rules must be enforced by deterministic application code. Do not rely
    only on system prompts or ask the language model to enforce the rules
    itself.

So a rule is data — `(domain, action, effect, conditions, priority)` — and the
thing that reads it is a function. There is deliberately no field anywhere in
this module holding natural-language instructions for the model. A "rule" the
model is asked to obey is not a rule, it is a request, and a request can be
argued out of.

`label` exists, but it is display text: what the user typed so they can
recognise their own rule in a list. The engine never reads it. That separation
is the point — the sentence a human reads and the predicate the machine
evaluates are different artefacts, and conflating them is how you end up with a
system whose behaviour depends on parsing English.

── Conditions ───────────────────────────────────────────────────────────────

Conditions are a small, closed vocabulary rather than an expression language.
An expression language here would be a sandbox-escape surface reachable from
user-editable data, and the rules people actually write ("never for anything
tagged health", "only during working hours") need very little.

Supported keys, all optional, ALL of which must match for the rule to apply:

    resource_prefix   str        resource starts with this
    resource_equals   str        exact resource match
    sensitivity_in    [str]      request sensitivity is one of these
    model_proposed    bool       matches how the action was originated
    max_risk          str        rule applies only at or below this risk

Anything unrecognised makes the rule NOT match, and says so. A condition the
engine does not understand must never be silently ignored: ignoring it would
apply a rule more broadly than the user wrote it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Optional

from core.interfaces.permission import ActionRequest, Effect, RiskLevel
from core.governance.effects import risk_rank

#: Matches any domain or action. A literal rather than None so the matching
#: rule is visible in the row and in the audit record.
WILDCARD: Final[str] = "*"

#: Condition keys the engine understands. See the module docstring.
KNOWN_CONDITIONS: Final[frozenset[str]] = frozenset({
    "resource_prefix",
    "resource_equals",
    "sensitivity_in",
    "model_proposed",
    "max_risk",
})

RULE_ORIGINS: Final[tuple[str, ...]] = ("user", "onboarding", "template", "system")


class InvalidRule(ValueError):
    """A constitution rule could not be understood.

    Raised at WRITE time, never at evaluation time. A rule that cannot be
    parsed must be rejected when the user saves it — discovering it at
    evaluation time would mean either failing the action or ignoring the rule,
    and both are wrong answers to "your rule was malformed".
    """


@dataclass(frozen=True, slots=True)
class ConstitutionRule:
    """One permanent rule.

    Frozen: a rule that could be mutated between matching and being recorded in
    the audit log would make the log a description of something that no longer
    exists.
    """

    effect: Effect
    domain: str = WILDCARD
    action: str = WILDCARD
    conditions: Mapping[str, Any] = field(default_factory=dict)
    priority: int = 100
    enabled: bool = True
    label: str = ""
    origin: str = "user"
    rule_id: Optional[uuid.UUID] = None

    @property
    def specificity(self) -> int:
        """How narrowly this rule is scoped. Higher is more specific.

        Used to break priority ties: a rule about `email.send` should beat a
        rule about `*.*` at the same priority, because the user wrote the
        specific one to say something the general one does not.
        """
        score = 0
        if self.domain != WILDCARD:
            score += 2
        if self.action != WILDCARD:
            score += 2
        score += len(self.conditions)
        return score

    def matches(self, request: ActionRequest, *, risk: RiskLevel) -> bool:
        """Whether this rule applies to `request`.

        Returns False for a disabled rule and for any rule carrying a condition
        this build does not understand.
        """
        if not self.enabled:
            return False
        if self.domain != WILDCARD and self.domain != request.domain:
            return False
        if self.action != WILDCARD and self.action != request.action:
            return False
        return self._conditions_match(request, risk=risk)

    def _conditions_match(self, request: ActionRequest, *, risk: RiskLevel) -> bool:
        for key, expected in self.conditions.items():
            if key not in KNOWN_CONDITIONS:
                # Fail to MATCH rather than fail open or fail closed globally.
                # An unknown condition means this build cannot tell whether the
                # user's rule applies; applying it anyway would enforce a rule
                # more broadly than it was written.
                return False

            if key == "resource_prefix":
                if not str(request.resource).startswith(str(expected)):
                    return False
            elif key == "resource_equals":
                if str(request.resource) != str(expected):
                    return False
            elif key == "sensitivity_in":
                sensitivity = str(request.parameters.get("sensitivity", ""))
                if sensitivity not in {str(v) for v in expected or ()}:
                    return False
            elif key == "model_proposed":
                if bool(request.model_proposed) is not bool(expected):
                    return False
            elif key == "max_risk":
                try:
                    ceiling = RiskLevel(str(expected))
                except ValueError:
                    return False
                if risk_rank(risk) > risk_rank(ceiling):
                    return False

        return True


def parse_rule(raw: Mapping[str, Any], *, rule_id: Optional[uuid.UUID] = None) -> ConstitutionRule:
    """Build a rule from stored/posted JSON, refusing anything malformed.

    Every refusal names what was wrong. A user editing their own Constitution
    and getting "invalid rule" back has learned nothing.
    """
    effect_raw = raw.get("effect")
    try:
        effect = Effect(str(effect_raw))
    except ValueError:
        raise InvalidRule(
            f"effect must be one of {', '.join(e.value for e in Effect)}, "
            f"got {effect_raw!r}"
        ) from None

    domain = str(raw.get("domain", WILDCARD) or WILDCARD).strip().lower()
    action = str(raw.get("action", WILDCARD) or WILDCARD).strip().lower()
    for name, value in (("domain", domain), ("action", action)):
        if value != WILDCARD and not all(ch.isalnum() or ch in "-_." for ch in value):
            raise InvalidRule(
                f"{name} must be '*' or a simple identifier, got {value!r}"
            )

    conditions = raw.get("conditions") or {}
    if not isinstance(conditions, Mapping):
        raise InvalidRule("conditions must be an object")
    unknown = set(conditions) - KNOWN_CONDITIONS
    if unknown:
        # Rejected at write time, deliberately. Storing a rule the engine will
        # silently never match is worse than refusing it: the user would
        # believe they were protected.
        raise InvalidRule(
            f"unknown condition(s): {', '.join(sorted(unknown))}. "
            f"Supported: {', '.join(sorted(KNOWN_CONDITIONS))}"
        )
    if "max_risk" in conditions:
        try:
            RiskLevel(str(conditions["max_risk"]))
        except ValueError:
            raise InvalidRule(
                f"max_risk must be one of {', '.join(r.value for r in RiskLevel)}"
            ) from None
    if "sensitivity_in" in conditions and not isinstance(
        conditions["sensitivity_in"], (list, tuple)
    ):
        raise InvalidRule("sensitivity_in must be a list")

    try:
        priority = int(raw.get("priority", 100))
    except (TypeError, ValueError):
        raise InvalidRule("priority must be an integer") from None

    origin = str(raw.get("origin", "user"))
    if origin not in RULE_ORIGINS:
        raise InvalidRule(f"origin must be one of {', '.join(RULE_ORIGINS)}")

    label = "".join(
        ch for ch in str(raw.get("label", "")) if ch.isprintable()
    ).strip()[:200]

    return ConstitutionRule(
        effect=effect,
        domain=domain,
        action=action,
        conditions=dict(conditions),
        priority=priority,
        enabled=bool(raw.get("enabled", True)),
        label=label,
        origin=origin,
        rule_id=rule_id,
    )


# ── The rules the brief names ────────────────────────────────────────────────
#
# These are offered during onboarding, not imposed. They are here as data so
# that "IRA supports the rules the brief describes" is a testable claim rather
# than a paragraph in a README — tests/governance/test_constitution.py asserts
# each one actually produces the behaviour its label promises.

STARTER_RULES: Final[tuple[Mapping[str, Any], ...]] = (
    {
        "label": "Never send an email without my confirmation",
        "domain": "email", "action": "send",
        "effect": Effect.REQUIRE_CONFIRMATION.value, "priority": 200,
        "origin": "onboarding",
    },
    {
        "label": "Never remember health conversations",
        "domain": "memory", "action": "write",
        "effect": Effect.DENY.value, "priority": 300,
        "conditions": {"sensitivity_in": ["health"]},
        "origin": "onboarding",
    },
    {
        "label": "Never use an external model for family information",
        "domain": "model", "action": "external_call",
        "effect": Effect.DENY.value, "priority": 300,
        "conditions": {"sensitivity_in": ["family"]},
        "origin": "onboarding",
    },
    {
        "label": "Never permanently delete files",
        "domain": "files", "action": "delete",
        "effect": Effect.DENY.value, "priority": 250,
        "origin": "onboarding",
    },
    {
        "label": "Allow automatic receipt organisation",
        "domain": "files", "action": "write",
        "effect": Effect.ALLOW_FOR_ROUTINE.value, "priority": 150,
        "conditions": {"resource_prefix": "receipts/"},
        "origin": "onboarding",
    },
    {
        "label": "Never make a purchase",
        "domain": "payment", "action": "execute",
        "effect": Effect.DENY.value, "priority": 400,
        "origin": "onboarding",
    },
    {
        "label": "Delete temporary voice data after transcription",
        "domain": "voice", "action": "retain",
        "effect": Effect.DENY.value, "priority": 200,
        "origin": "onboarding",
    },
)


__all__ = [
    "KNOWN_CONDITIONS",
    "RULE_ORIGINS",
    "STARTER_RULES",
    "WILDCARD",
    "ConstitutionRule",
    "InvalidRule",
    "parse_rule",
]
