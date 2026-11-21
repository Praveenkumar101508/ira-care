"""Phase 4 — the Permission Broker fails closed, and the ceiling holds.

The properties under test, in rough order of how badly they would matter if
they broke:

  1. Nothing unclassified is ever permitted.
  2. Critical actions are blocked outright and no approval releases them.
  3. A user's rules can only tighten, never loosen — the deployment ceiling
     clamps everything.
  4. Grants are purpose-bound, expiring, revocable, and inert unless granted.
  5. Risk comes from the EXISTING engines, and is only ever raised.
  6. An exception anywhere denies rather than propagating.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.governance.broker import IRAPermissionBroker, build_broker, classify_risk
from core.governance.grants import ANY_PURPOSE, PermissionGrant, find_covering
from core.governance.policies import never_automatic, policy_for
from core.governance.rules import parse_rule
from core.identity.principal import PRIVATE_TENANT_ID, Principal, private_user_id
from core.interfaces.permission import ActionRequest, Effect, RiskLevel


def principal() -> Principal:
    return Principal(
        user_id=private_user_id("owner"), tenant_id=PRIVATE_TENANT_ID,
        session_id=uuid.uuid4(), edition="private", username="owner",
    )


def req(domain, action, *, resource="", purpose="the user asked", model=True):
    return ActionRequest(
        principal=principal(), domain=domain, action=action,
        purpose=purpose, resource=resource, model_proposed=model,
    )


def now() -> datetime:
    return datetime.now(timezone.utc)


# ── 1. Nothing unclassified is permitted ─────────────────────────────────────


@pytest.mark.parametrize(
    "domain,action",
    # An empty domain is deliberately absent: ActionRequest refuses it at
    # construction, so it never reaches the broker. That has its own test below
    # rather than a skipped case here.
    [("nonsense", "whatever"), ("email", "forward"), ("shell", "sudo")],
)
def test_an_action_with_no_policy_is_denied(domain, action):
    outcome = build_broker().evaluate(req(domain, action))
    assert outcome.decision.effect is Effect.DENY
    assert "no tool policy" in outcome.decision.reason.lower()
    assert outcome.ticket is None


def test_an_empty_domain_is_rejected_before_it_reaches_the_broker():
    """`ActionRequest.__post_init__` refuses it — a malformed request should
    never become a decision at all."""
    with pytest.raises(ValueError):
        ActionRequest(principal=principal(), domain="", action="read", purpose="x")


def test_a_request_without_a_purpose_is_rejected():
    """Purpose is mandatory: an action whose reason cannot be stated cannot be
    shown honestly on an Action Receipt."""
    with pytest.raises(ValueError):
        ActionRequest(principal=principal(), domain="email", action="read", purpose="")


# ── 2. Critical is blocked outright ──────────────────────────────────────────


@pytest.mark.parametrize("domain,action", [
    ("payment", "execute"), ("social", "publish"), ("security", "change"),
    ("email", "delete"),
])
def test_critical_actions_are_blocked_whatever_the_user_says(domain, action):
    """Even with a permissive rule at maximum priority."""
    broker = build_broker(rules=[
        parse_rule({"domain": domain, "action": action, "effect": "allow",
                    "priority": 9999, "label": "please just do it"})
    ])
    outcome = broker.evaluate(req(domain, action))
    assert outcome.decision.effect is Effect.DENY
    assert outcome.ticket is None


def test_critical_is_checked_before_rules_are_even_consulted():
    outcome = build_broker().evaluate(req("payment", "execute"))
    assert outcome.decision.source == "critical-blocked"


# ── 3. The ceiling holds ─────────────────────────────────────────────────────


def test_a_user_rule_cannot_make_an_action_more_permissive_than_policy():
    """The direction that matters: a rule may not exceed the deployment ceiling.

    `email.send` has `max_effect=require_confirmation`, so even a maximum-priority
    "always allow email" resolves to confirmation. The clamp is what does it, and
    the reason says so — a user whose rule was overridden is entitled to know
    that it was, rather than seeing an unexplained prompt.
    """
    broker = build_broker(rules=[
        parse_rule({"domain": "email", "action": "send", "effect": "allow",
                    "priority": 9999, "label": "always allow email"})
    ])
    outcome = broker.evaluate(req("email", "send"))
    assert outcome.decision.effect is Effect.REQUIRE_CONFIRMATION
    assert "clamped" in outcome.decision.reason.lower()
    assert "policy-ceiling" in outcome.decision.source


def test_a_user_rule_can_permit_up_to_the_ceiling():
    """A Constitution must be able to say "yes, you may" — within the bound.

    Regression test for a real defect found while writing this suite: the broker
    originally took the stricter of the rule and the deployment DEFAULT, which
    made every permissive rule effect dead vocabulary. A user could write "let
    IRA read my calendar", see it listed in their Constitution, and find it did
    nothing at all.

    `calendar.read` defaults to `deny` and is capped at `allow_for_session`, so
    this asserts both halves at once: the rule lifts the answer off the default,
    and the ceiling still bounds where it can get to.
    """
    request = req("calendar", "read")
    assert build_broker().evaluate(request).decision.effect is Effect.DENY

    broker = build_broker(rules=[
        parse_rule({"domain": "calendar", "action": "read", "effect": "allow",
                    "label": "let IRA read my calendar"})
    ])
    outcome = broker.evaluate(request)

    assert outcome.decision.effect is Effect.ALLOW_FOR_SESSION, (
        "the user's own rule did not take effect, or was not clamped to the ceiling"
    )
    assert outcome.may_execute is True
    assert "clamped" in outcome.decision.reason.lower()


def test_a_permissive_rule_still_cannot_overturn_a_deny_rule():
    """The permissive path must not have weakened deny-wins."""
    broker = build_broker(rules=[
        parse_rule({"domain": "calendar", "action": "read", "effect": "deny",
                    "priority": 1, "label": "no calendar access"}),
        parse_rule({"domain": "calendar", "action": "read", "effect": "allow",
                    "priority": 9999, "label": "actually yes"}),
    ])
    outcome = broker.evaluate(req("calendar", "read"))
    assert outcome.decision.effect is Effect.DENY
    assert outcome.ticket is None


def test_there_is_no_tool_policy_for_editing_the_constitution():
    """The structural answer to "could a prompt injection grant itself rights?"

    Rules are written through the authenticated user endpoint. An agent that
    tries to reach the Constitution as a tool has no policy covering it, so the
    broker denies it as unclassified — no separate guard to remember.
    """
    for action in ("write", "create", "update", "delete", "grant"):
        assert policy_for("constitution", action) is None
        assert policy_for("governance", action) is None
        outcome = build_broker().evaluate(req("constitution", action))
        assert outcome.decision.effect is Effect.DENY


def test_a_grant_cannot_exceed_the_ceiling_either():
    """A user setting "never ask me" for email does not get silent sending."""
    grant = PermissionGrant(
        connector="email", scope="send", purpose="the user asked",
        granted_at=now(), confirmation_policy="never",
    )
    outcome = build_broker(grants=[grant]).evaluate(req("email", "send"))
    assert outcome.decision.effect is Effect.REQUIRE_CONFIRMATION
    assert outcome.ticket is None


def test_a_user_rule_can_always_make_things_stricter():
    broker = build_broker(rules=[
        parse_rule({"domain": "memory", "action": "read", "effect": "deny",
                    "label": "no memory reads"})
    ])
    outcome = broker.evaluate(req("memory", "read"))
    assert outcome.decision.effect is Effect.DENY


def test_never_automatic_is_derived_from_the_policies_it_describes():
    """A second hand-maintained list would be one edit from disagreeing."""
    listed = set(never_automatic())
    assert ("email", "send") in listed
    assert ("payment", "execute") in listed
    assert ("memory", "read") not in listed
    for domain, action in listed:
        policy = policy_for(domain, action)
        assert policy is not None
        assert policy.max_effect in (Effect.DENY, Effect.REQUIRE_CONFIRMATION)


# ── 4. Grants ────────────────────────────────────────────────────────────────


def test_a_grant_is_inert_until_granted():
    grant = PermissionGrant(connector="calendar", scope="read", purpose="the user asked")
    assert grant.status() == "never_granted"
    assert not grant.covers(req("calendar", "read"))


def test_an_expired_grant_does_not_cover():
    grant = PermissionGrant(
        connector="calendar", scope="read", purpose="the user asked",
        granted_at=now() - timedelta(days=2), expires_at=now() - timedelta(days=1),
    )
    assert grant.status() == "expired"
    assert not grant.covers(req("calendar", "read"))


def test_a_revoked_grant_reads_as_revoked_even_if_also_expired():
    """Revocation is the fact the user acted on, and the one they will look for."""
    grant = PermissionGrant(
        connector="calendar", scope="read", purpose="the user asked",
        granted_at=now() - timedelta(days=3),
        expires_at=now() - timedelta(days=1),
        revoked_at=now() - timedelta(days=2),
    )
    assert grant.status() == "revoked"


def test_a_grant_is_bound_to_its_purpose():
    """Without this, "read my calendar to suggest meeting times" becomes
    authority to read the calendar for anything, forever."""
    grant = PermissionGrant(
        connector="calendar", scope="read", purpose="suggest meeting times",
        granted_at=now(),
    )
    assert grant.covers(req("calendar", "read", purpose="suggest meeting times"))
    assert not grant.covers(req("calendar", "read", purpose="build an advertising profile"))


def test_purpose_matching_normalises_whitespace_and_case_but_nothing_more():
    grant = PermissionGrant(
        connector="calendar", scope="read", purpose="Suggest  Meeting Times",
        granted_at=now(),
    )
    assert grant.covers(req("calendar", "read", purpose="suggest meeting times"))
    # Not fuzzy: "close enough" is how purpose binding erodes into none at all.
    assert not grant.covers(req("calendar", "read", purpose="suggest meetings"))


def test_the_any_purpose_sentinel_covers_everything():
    grant = PermissionGrant(
        connector="calendar", scope="read", purpose=ANY_PURPOSE, granted_at=now(),
    )
    assert grant.covers(req("calendar", "read", purpose="literally anything"))


def test_a_grant_is_bound_to_its_resource():
    grant = PermissionGrant(
        connector="files", scope="read", purpose="the user asked",
        resource="receipts/*", granted_at=now(),
    )
    assert grant.covers(req("files", "read", resource="receipts/jan.pdf"))
    assert not grant.covers(req("files", "read", resource="/etc/passwd"))


def test_the_strictest_covering_grant_wins():
    """Otherwise the outcome depends on row ordering."""
    loose = PermissionGrant(connector="calendar", scope="read", purpose=ANY_PURPOSE,
                            granted_at=now(), confirmation_policy="never")
    tight = PermissionGrant(connector="calendar", scope="read", purpose=ANY_PURPOSE,
                            granted_at=now(), confirmation_policy="always")
    chosen = find_covering(req("calendar", "read"), [loose, tight])
    assert chosen is tight
    assert find_covering(req("calendar", "read"), [tight, loose]) is tight


def test_with_no_grant_the_policy_default_applies():
    outcome = build_broker().evaluate(req("calendar", "read"))
    assert outcome.decision.effect is Effect.DENY
    assert outcome.decision.source == "policy-default"


def test_a_covering_grant_permits_within_the_ceiling():
    grant = PermissionGrant(
        connector="calendar", scope="read", purpose="the user asked",
        granted_at=now(), confirmation_policy="once_per_session",
    )
    outcome = build_broker(grants=[grant]).evaluate(req("calendar", "read"))
    assert outcome.decision.effect is Effect.ALLOW_FOR_SESSION
    assert outcome.may_execute is True


# ── 5. Risk comes from the existing engines ──────────────────────────────────


def test_risk_is_never_lowered_below_the_policy_baseline():
    policy = policy_for("email", "send")
    assert policy is not None
    risk = classify_risk(req("email", "send"), policy)
    from core.governance.effects import risk_rank

    assert risk_rank(risk) >= risk_rank(policy.risk)


def test_a_model_proposed_side_effecting_action_is_never_merely_low_risk():
    """Low risk + side-effecting + model-proposed is the combination prompt
    injection aims for."""
    policy = policy_for("memory", "delete")
    assert policy is not None
    risk = classify_risk(req("memory", "delete", model=True), policy)
    assert risk is not RiskLevel.LOW


def test_file_risk_uses_the_action_guards_classifier():
    """Not a second opinion about what a dangerous path looks like."""
    policy = policy_for("files", "delete")
    assert policy is not None
    protected = classify_risk(req("files", "delete", resource="/etc/passwd"), policy)
    from core.governance.effects import risk_rank

    assert risk_rank(protected) >= risk_rank(RiskLevel.HIGH)


# ── 6. Fail closed on the unexpected ─────────────────────────────────────────


async def test_the_protocol_method_returns_a_decision():
    broker = build_broker()
    decision = await broker.decide(req("memory", "read"))
    assert decision.effect is Effect.ALLOW


def test_an_exception_inside_evaluation_denies_rather_than_raising(monkeypatch):
    """A broker that raised would let a caller with a bare `except` treat the
    failure as "carry on"."""
    broker = build_broker()

    def boom(*_a, **_kw):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr("core.governance.broker.policy_for", boom)
    outcome = broker.evaluate(req("memory", "read"))

    assert outcome.decision.effect is Effect.DENY
    assert outcome.decision.source == "broker-exception"
    assert outcome.ticket is None


def test_is_allowed_never_treats_confirmation_as_permission():
    from core.interfaces.permission import PermissionDecision

    decision = PermissionDecision(
        effect=Effect.REQUIRE_CONFIRMATION, reason="ask first"
    )
    assert decision.is_allowed is False
    assert decision.needs_confirmation is True


# ── The broker is stateless with respect to rules ────────────────────────────


def test_two_brokers_with_different_rules_do_not_interfere():
    """Rules are passed in, not global — which is what keeps a decision for one
    tenant from being shaped by another's Constitution."""
    strict = IRAPermissionBroker(rules=[
        parse_rule({"domain": "memory", "action": "read", "effect": "deny"})
    ])
    permissive = IRAPermissionBroker()

    assert strict.evaluate(req("memory", "read")).decision.effect is Effect.DENY
    assert permissive.evaluate(req("memory", "read")).decision.effect is Effect.ALLOW
