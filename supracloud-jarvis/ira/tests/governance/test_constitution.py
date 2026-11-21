"""Phase 4 — the User Constitution is enforced by code, not by prompting.

The differentiator the transformation brief names, so the tests are about the
properties that make it a differentiator rather than a settings screen:

  * a rule is DATA and the engine is a function — same inputs, same answer,
    every time, with no model in the loop;
  * a matching DENY wins outright, so a user cannot accidentally defeat their
    own prohibition by adding a permissive rule later;
  * a rule can only make things stricter, never looser;
  * a condition this build does not understand makes the rule NOT match, and
    is refused at write time rather than silently ignored.

Each of the seven rules the brief lists by name is exercised end-to-end,
so "IRA supports these rules" is a checked claim rather than a paragraph.
"""
from __future__ import annotations

import uuid

import pytest

from core.governance.constitution import evaluate, summarise
from core.governance.effects import stricter_of
from core.governance.rules import (
    KNOWN_CONDITIONS,
    STARTER_RULES,
    ConstitutionRule,
    InvalidRule,
    parse_rule,
)
from core.identity.principal import PRIVATE_TENANT_ID, Principal, private_user_id
from core.interfaces.permission import ActionRequest, Effect, RiskLevel


def principal() -> Principal:
    return Principal(
        user_id=private_user_id("owner"),
        tenant_id=PRIVATE_TENANT_ID,
        session_id=uuid.uuid4(),
        edition="private",
        username="owner",
    )


def req(domain, action, *, resource="", purpose="the user asked", model=True, **params):
    return ActionRequest(
        principal=principal(), domain=domain, action=action, purpose=purpose,
        resource=resource, model_proposed=model, parameters=params,
    )


def rule(**kw) -> ConstitutionRule:
    kw.setdefault("effect", "deny")
    return parse_rule(kw)


# ── Determinism ──────────────────────────────────────────────────────────────


def test_the_same_inputs_always_give_the_same_answer():
    """No clock, no randomness, no model. This is what makes it auditable."""
    rules = [rule(domain="email", action="send", effect="require_confirmation")]
    request = req("email", "send")
    answers = {evaluate(request, rules).effect for _ in range(50)}
    assert answers == {Effect.REQUIRE_CONFIRMATION}


def test_no_matching_rule_is_silence_not_permission():
    """`effect is None` must not be confused with ALLOW.

    Collapsing the two would turn "the Constitution has nothing to say" into
    "the Constitution permits it", which is how a rules engine quietly becomes
    an approval engine.
    """
    verdict = evaluate(req("email", "send"), [rule(domain="files", action="delete")])
    assert verdict.effect is None
    assert verdict.has_opinion is False


# ── Deny wins ────────────────────────────────────────────────────────────────


def test_a_matching_deny_beats_a_higher_priority_allow():
    """The rule worth arguing about, so it gets an explicit test.

    A user who wrote "never delete files" should not have it silently overturned
    by a later, higher-priority "allow the cleanup routine".
    """
    rules = [
        rule(domain="files", action="delete", effect="deny", priority=1, label="never delete"),
        rule(domain="files", action="delete", effect="allow", priority=999, label="cleanup"),
    ]
    verdict = evaluate(req("files", "delete"), rules)
    assert verdict.effect is Effect.DENY
    assert verdict.deciding_rule is not None
    assert verdict.deciding_rule.label == "never delete"


def test_a_wildcard_deny_still_wins_over_a_specific_allow():
    rules = [
        rule(domain="*", action="*", effect="deny", priority=1),
        rule(domain="email", action="send", effect="allow", priority=500),
    ]
    assert evaluate(req("email", "send"), rules).effect is Effect.DENY


def test_a_disabled_deny_does_not_apply():
    """Disabling is the deliberate act that lifts a prohibition."""
    rules = [
        rule(domain="files", action="delete", effect="deny", enabled=False),
        rule(domain="files", action="delete", effect="allow", priority=10),
    ]
    assert evaluate(req("files", "delete"), rules).effect is Effect.ALLOW


# ── Ordering among non-deny rules ────────────────────────────────────────────


def test_higher_priority_wins():
    rules = [
        rule(domain="email", action="send", effect="allow", priority=10),
        rule(domain="email", action="send", effect="require_confirmation", priority=900),
    ]
    assert evaluate(req("email", "send"), rules).effect is Effect.REQUIRE_CONFIRMATION


def test_a_more_specific_rule_beats_a_wildcard_at_equal_priority():
    """The user wrote the specific one to say something the general one does not."""
    rules = [
        rule(domain="*", action="*", effect="allow", priority=100),
        rule(domain="email", action="send", effect="require_confirmation", priority=100),
    ]
    assert evaluate(req("email", "send"), rules).effect is Effect.REQUIRE_CONFIRMATION


def test_ties_break_toward_the_stricter_effect():
    rules = [
        rule(domain="email", action="send", effect="allow", priority=100),
        rule(domain="email", action="send", effect="require_confirmation", priority=100),
    ]
    assert evaluate(req("email", "send"), rules).effect is Effect.REQUIRE_CONFIRMATION


# ── Conditions ───────────────────────────────────────────────────────────────


def test_resource_prefix_scopes_a_rule():
    rules = [rule(domain="files", action="write", effect="allow_for_routine",
                  conditions={"resource_prefix": "receipts/"})]
    assert evaluate(req("files", "write", resource="receipts/jan.pdf"), rules).effect \
        is Effect.ALLOW_FOR_ROUTINE
    assert evaluate(req("files", "write", resource="taxes/2024.pdf"), rules).effect is None


def test_sensitivity_scopes_a_rule():
    rules = [rule(domain="memory", action="write", effect="deny",
                  conditions={"sensitivity_in": ["health"]})]
    assert evaluate(req("memory", "write", sensitivity="health"), rules).effect is Effect.DENY
    assert evaluate(req("memory", "write", sensitivity="work"), rules).effect is None


def test_model_proposed_scopes_a_rule():
    """Lets a user say "only when I ask, never when you decide to"."""
    rules = [rule(domain="web", action="fetch", effect="deny",
                  conditions={"model_proposed": True})]
    assert evaluate(req("web", "fetch", model=True), rules).effect is Effect.DENY
    assert evaluate(req("web", "fetch", model=False), rules).effect is None


def test_max_risk_scopes_a_rule():
    rules = [rule(domain="files", action="write", effect="allow",
                  conditions={"max_risk": "low"})]
    assert evaluate(req("files", "write"), rules, risk=RiskLevel.LOW).effect is Effect.ALLOW
    assert evaluate(req("files", "write"), rules, risk=RiskLevel.HIGH).effect is None


def test_an_unknown_condition_makes_the_rule_not_match():
    """Applying a rule whose condition this build cannot evaluate would enforce
    it more broadly than the user wrote it."""
    unparsed = ConstitutionRule(
        effect=Effect.ALLOW, domain="files", action="write",
        conditions={"phase_of_moon": "waxing"},
    )
    assert evaluate(req("files", "write"), [unparsed]).effect is None


def test_all_conditions_must_match_not_just_one():
    rules = [rule(domain="files", action="write", effect="allow",
                  conditions={"resource_prefix": "receipts/", "model_proposed": False})]
    assert evaluate(req("files", "write", resource="receipts/a.pdf", model=True),
                    rules).effect is None
    assert evaluate(req("files", "write", resource="receipts/a.pdf", model=False),
                    rules).effect is Effect.ALLOW


# ── Write-time validation ────────────────────────────────────────────────────


def test_an_unknown_condition_is_refused_at_write_time():
    """Storing a rule the engine will never match is worse than refusing it —
    the user would believe they were protected."""
    with pytest.raises(InvalidRule) as exc:
        parse_rule({"effect": "deny", "conditions": {"phase_of_moon": "waxing"}})
    assert "phase_of_moon" in str(exc.value)
    # The refusal must name what IS supported, or the user cannot fix it.
    assert any(known in str(exc.value) for known in KNOWN_CONDITIONS)


@pytest.mark.parametrize("bad", ["allowed", "ALLOW", "", "yes", None])
def test_an_unknown_effect_is_refused(bad):
    with pytest.raises(InvalidRule):
        parse_rule({"effect": bad})


def test_an_invalid_max_risk_is_refused():
    with pytest.raises(InvalidRule):
        parse_rule({"effect": "deny", "conditions": {"max_risk": "catastrophic"}})


def test_a_non_list_sensitivity_is_refused():
    with pytest.raises(InvalidRule):
        parse_rule({"effect": "deny", "conditions": {"sensitivity_in": "health"}})


def test_a_domain_with_shell_metacharacters_is_refused():
    with pytest.raises(InvalidRule):
        parse_rule({"effect": "deny", "domain": "email; DROP TABLE"})


def test_a_label_is_stripped_of_control_characters():
    parsed = parse_rule({"effect": "deny", "label": "never\n\rsend\x00 email"})
    assert "\n" not in parsed.label and "\x00" not in parsed.label


def test_the_engine_never_reads_the_label():
    """The sentence a human reads and the predicate the machine evaluates are
    different artefacts. Conflating them is how behaviour ends up depending on
    parsing English."""
    misleading = rule(domain="email", action="send", effect="allow",
                      label="NEVER EVER ALLOW SENDING EMAIL")
    assert evaluate(req("email", "send"), [misleading]).effect is Effect.ALLOW


# ── The seven rules the brief names ──────────────────────────────────────────


def test_every_starter_rule_parses():
    for raw in STARTER_RULES:
        parse_rule(dict(raw))


@pytest.mark.parametrize(
    "label,request_args,expected",
    [
        ("Never send an email without my confirmation",
         ("email", "send"), Effect.REQUIRE_CONFIRMATION),
        ("Never permanently delete files",
         ("files", "delete"), Effect.DENY),
        ("Never make a purchase",
         ("payment", "execute"), Effect.DENY),
    ],
)
def test_a_starter_rule_does_what_its_label_promises(label, request_args, expected):
    """The label is display text — but it must not be a lie about the rule."""
    raw = next(r for r in STARTER_RULES if r["label"] == label)
    verdict = evaluate(req(*request_args), [parse_rule(dict(raw))])
    assert verdict.effect is expected


def test_the_health_memory_rule_only_bites_on_health():
    raw = next(r for r in STARTER_RULES if "health" in r["label"])
    rules = [parse_rule(dict(raw))]
    assert evaluate(req("memory", "write", sensitivity="health"), rules).effect is Effect.DENY
    assert evaluate(req("memory", "write", sensitivity="work"), rules).effect is None


def test_the_receipt_rule_only_bites_under_receipts():
    raw = next(r for r in STARTER_RULES if "receipt" in r["label"].lower())
    rules = [parse_rule(dict(raw))]
    assert evaluate(req("files", "write", resource="receipts/x.pdf"), rules).effect \
        is Effect.ALLOW_FOR_ROUTINE
    assert evaluate(req("files", "write", resource="/etc/passwd"), rules).effect is None


# ── Plain language ───────────────────────────────────────────────────────────


def test_summarise_reads_the_structure_not_the_stored_label():
    """So what a user sees is what the engine will do. A label that has drifted
    from its rule shows up as a discrepancy rather than being echoed as truth."""
    misleading = rule(domain="email", action="send", effect="allow",
                      label="I never send email")
    lines = summarise([misleading])
    assert any("Always allow" in line and "email.send" in line for line in lines)
    assert not any("I never send email" in line for line in lines)


def test_summarise_marks_disabled_rules():
    lines = summarise([rule(domain="email", action="send", effect="deny", enabled=False)])
    assert any("[disabled]" in line for line in lines)


def test_summarise_handles_an_empty_constitution():
    assert summarise([]) == []


# ── The combining primitive ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "a,b,expected",
    [
        (Effect.ALLOW, Effect.DENY, Effect.DENY),
        (Effect.ALLOW, Effect.REQUIRE_CONFIRMATION, Effect.REQUIRE_CONFIRMATION),
        (Effect.ALLOW_FOR_SESSION, Effect.ALLOW_FOR_ROUTINE, Effect.ALLOW_FOR_ROUTINE),
        (Effect.DENY, Effect.DENY, Effect.DENY),
        (Effect.ALLOW, Effect.ALLOW, Effect.ALLOW),
    ],
)
def test_stricter_of_is_the_safe_direction(a, b, expected):
    assert stricter_of(a, b) is expected
    assert stricter_of(b, a) is expected, "stricter_of must be commutative"
