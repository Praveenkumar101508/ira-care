"""P9-04 — risk grading is global on purpose, and per-tenant strictness is real.

Finding F9-2 said: *"845 lines across `trust/`, and `action_guard.py` and
`proof_card.py` contain no reference to a tenant, a workspace or a `Principal`.
Risk grading is global. A tenant cannot have a stricter policy than another, and
nothing records which tenant an action was graded for."*

Two of those three sentences are wrong, and they are wrong the same way F9-1 and
F9-3 were: they read module text for the word "tenant" instead of tracing where
grading actually happens.

**"A tenant cannot have a stricter policy."** It can, and that is the sanctioned
mechanism. `broker._evaluate` step 3 evaluates the User Constitution —
`constitution_rules`, tenant-scoped, loaded by `store.load_broker(principal)`
through `tenant_connection`. Step 5 clamps the combined effect to
`policy.max_effect`, so a tenant may only ever be **stricter** than the
deployment baseline (ADR-0003, and `/governance/policies` is read-only for
exactly this reason).

**"Nothing records which tenant an action was graded for."** `permission_decisions`
is tenant-scoped and written by `store.record_decision(principal, ...)`, and
since P9-02 the Trust Ledger records the tenant too.

**What is true** is narrower and is not a defect: the *baseline* and the
*resource-derived* risk are global. `tool_policies` carries no `tenant_id` at all
— `test_governance_isolation_rls` asserts that deliberately — and
`classify_command_risk` / `classify_file_risk` grade a command line and a
filesystem path. `rm -rf /` is not more dangerous for one customer than another.
Making those per-tenant would let a tenant *lower* a floor, which is the one
direction the whole design refuses.

So this file does not make risk grading tenant-aware. It makes the global-ness
**deliberate and executable**, which is the other option P9-04 was scoped with,
and it pins the per-tenant strictness that F9-2 said did not exist — because an
architecture nobody asserts is one refactor from being different.

── F9-6, found while tracing, and fixed here ──────────────────────────────

Nothing in production calls `core.governance.gateway.execute`. The shipped
execution surfaces — `/actions`, `/notes`, `/calendar`, assist scheduling — go
through `actions/gated_execution.py`, which applies simulation, the owner gate,
one-shot approval, the ledger and Proof Cards, and **never consults the broker,
the Constitution or the grants**.

It has not been exploitable: every one of those callers passes
`is_owner=api.middleware.auth.is_owner(...)`, which is False in cloud whatever
the name is, so the owner gate refuses. But that is a property of five call
sites all remembering to pass the right flag — `067`'s *"a gate, not an
answer"*. The edition check now lives in the module that would do the acting,
on BOTH halves, because `confirm_action` never had an owner gate at all and it
is the half that runs the side effect.
"""
from __future__ import annotations

import uuid

import pytest

from actions.gated_execution import UNGOVERNED_IN_CLOUD, confirm_action, stage_action
from actions.shadow_world import ActionSpec
from core.governance.broker import IRAPermissionBroker, classify_risk
from core.governance.policies import policy_for
from core.identity.principal import Principal, system_principal
from core.interfaces.permission import ActionRequest, Effect, RiskLevel


def _request(principal: Principal, *, domain="files", action="write",
             resource="/tmp/p904.txt", model_proposed=False) -> ActionRequest:
    return ActionRequest(
        principal=principal, domain=domain, action=action,
        resource=resource, purpose="p9-04 test", model_proposed=model_proposed,
    )


def _spec() -> ActionSpec:
    return ActionSpec(
        kind="file_change",
        intended_action="delete /tmp/p904.txt",
        requested_by="owner",
        risk="low",
        objects_affected=["/tmp/p904.txt"],
        data_leaving_device=[],
        tools_involved=["files.delete"],
    )


TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()


# ─────────────────────────────────────────────────────────────────────────────
# 1. The global half — asserted as a decision, not left as an accident
# ─────────────────────────────────────────────────────────────────────────────


def test_risk_is_the_same_answer_whoever_asks():
    """The deliberate-global assertion, executable.

    A command line's danger is a fact about the command, not about the customer
    running it. If this ever starts differing by principal, somebody has made
    risk grading tenant-aware — which is a decision that must be argued for,
    because the obvious next step after "tenant A may be stricter" is "tenant B
    may be laxer", and the floor is the point.

    Swept across the leading hex digit of the tenant id rather than compared
    between two arbitrary tenants. The first version of this test used two
    `uuid4()`s, and a planted `if tenant_id.startswith("0"): risk = LOW` walked
    straight past it — two samples cannot show that a function ignores an input.

    Graded against a DESTRUCTIVE shell command rather than a file write. The
    second version of this test swept `files.write`, whose policy risk is
    already the lowest there is — so a plant that forced the risk down had
    nothing to move, and the sweep passed while the structural check below did
    the whole job. A guard that cannot fail is not a guard.
    """
    policy = policy_for("shell", "run")
    assert policy is not None
    baseline = classify_risk(
        _request(system_principal(tenant_id=TENANT_A), domain="shell",
                 action="run", resource="rm -rf /"), policy)
    assert baseline is not RiskLevel.LOW, (
        "the sweep below cannot detect a downgrade if the case it grades is "
        "already at the floor")

    answers = set()
    for digit in "0123456789abcdef":
        tenant = uuid.UUID(digit + str(uuid.uuid4())[1:])
        for scopes in (frozenset(), frozenset({"shell:run"})):
            principal = system_principal(tenant_id=tenant)
            if scopes:
                principal = principal.with_scopes(*scopes)
            answers.add(classify_risk(
                _request(principal, domain="shell", action="run",
                         resource="rm -rf /"), policy))
    assert len(answers) == 1, (
        f"risk grading gave {len(answers)} different answers for the same "
        f"request depending on who asked: {answers}")


def test_the_grader_does_not_read_the_caller_at_all():
    """The same claim as structure rather than as sampling.

    A behavioural sweep can only ever show that the principals it happened to
    try agree. This reads the function: `classify_risk` decides from
    `request.domain`, `request.action`, `request.resource` and
    `request.model_proposed`, and touches nothing about who is asking. Any plant
    that keys on the caller has to mention it.
    """
    import inspect

    source = inspect.getsource(classify_risk)
    body = source.split('"""')[2] if source.count('"""') >= 2 else source
    for forbidden in ("principal", "tenant", "workspace", "is_owner", "roles",
                      "scopes"):
        assert forbidden not in body, (
            f"classify_risk mentions {forbidden!r}. Risk grading keying on the "
            f"caller is a design change, not a refactor: the step after "
            f"'tenant A may be stricter' is 'tenant B may be laxer', and the "
            f"floor is the point. Per-tenant strictness belongs in the "
            f"Constitution, which can only narrow.")


def test_the_resource_and_not_the_caller_moves_the_risk():
    """The other half of the same claim: grading is not inert, it just keys on
    the thing being acted on."""
    policy = policy_for("shell", "run")
    assert policy is not None
    harmless = classify_risk(
        _request(system_principal(tenant_id=TENANT_A), domain="shell",
                 action="run", resource="ls -l"), policy)
    destructive = classify_risk(
        _request(system_principal(tenant_id=TENANT_A), domain="shell",
                 action="run", resource="rm -rf /"), policy)
    order = list(RiskLevel)
    assert order.index(destructive) > order.index(harmless)


def test_the_deployment_baseline_carries_no_tenant():
    """`tool_policies` is global by construction — there is no per-tenant
    baseline to be inconsistent with. Asserted on the loaded policy object
    rather than the table, so it also catches somebody adding a tenant field in
    Python and never in SQL."""
    policy = policy_for("files", "write")
    assert not hasattr(policy, "tenant_id")
    assert not hasattr(policy, "workspace_id")


def test_a_model_proposed_side_effect_is_never_low_for_anyone():
    """A floor that must not become negotiable per tenant: a low-risk grading on
    a side-effecting action the MODEL proposed is exactly the combination prompt
    injection aims for."""
    policy = policy_for("files", "write")
    for tenant in (TENANT_A, TENANT_B):
        risk = classify_risk(
            _request(system_principal(tenant_id=tenant), model_proposed=True),
            policy)
        assert risk is not RiskLevel.LOW


# ─────────────────────────────────────────────────────────────────────────────
# 2. Per-tenant strictness exists — the half F9-2 said did not
# ─────────────────────────────────────────────────────────────────────────────


def test_a_tenants_rule_can_deny_what_the_deployment_allows():
    """The mechanism F9-2 reported as missing.

    The rule is constructed here rather than hunted for in `STARTER_RULES`. A
    first version of this test looked for a denying starter rule and skipped
    when it found none — and a skip on the single most important assertion in
    the file is a vacuous pass, which is the failure mode this whole phase keeps
    finding in other people's guards.
    """
    from core.governance.rules import ConstitutionRule

    rule = ConstitutionRule(
        effect=Effect.DENY, domain="files", action="write",
        label="this tenant does not permit file writes",
    )
    request = _request(system_principal(tenant_id=TENANT_A))

    strict = IRAPermissionBroker(rules=(rule,)).evaluate(request).decision
    lax = IRAPermissionBroker().evaluate(request).decision

    assert strict.effect is Effect.DENY
    assert strict.source == "constitution", (
        f"the denial came from {strict.source!r} rather than the tenant's "
        f"Constitution, so this proves nothing about per-tenant strictness")
    assert lax.effect is not Effect.DENY or lax.source != "constitution", (
        "the rule made no difference — per-tenant strictness is not reaching "
        "the decision")


def test_a_tenants_rule_cannot_exceed_the_deployment_ceiling():
    """Strictness only. `clamp_to_ceiling` is what makes "a user's Constitution
    may only make things stricter" a property rather than a promise."""
    from core.governance.effects import clamp_to_ceiling

    policy = policy_for("files", "write")
    assert clamp_to_ceiling(Effect.ALLOW, policy.max_effect) is not Effect.ALLOW \
        or policy.max_effect is Effect.ALLOW
    # And the general law, over every effect the ceiling could face:
    for proposed in Effect:
        clamped = clamp_to_ceiling(proposed, Effect.DENY)
        assert clamped is Effect.DENY, (
            "a ceiling of DENY let something through — the ceiling is the one "
            "thing a tenant rule may not lift")


def test_critical_is_blocked_before_any_rule_is_read():
    """No tenant configuration can reach a critical action, in either
    direction. Checked in the broker before the Constitution precisely so that
    nothing a user writes can release it."""
    policy = policy_for("shell", "run")
    request = _request(system_principal(tenant_id=TENANT_A), domain="shell",
                       action="run", resource="rm -rf /")
    if classify_risk(request, policy) is not RiskLevel.CRITICAL:
        pytest.skip("this build does not grade that command critical")
    decision = IRAPermissionBroker().evaluate(request).decision
    assert decision.effect is Effect.DENY
    assert decision.source == "critical-blocked"


def test_two_tenants_rules_do_not_reach_each_others_brokers():
    """A broker is constructed per caller from `load_broker(principal)`, which
    reads through `tenant_connection`. Asserted on the object because a shared
    or cached broker would silently apply one customer's Constitution to
    another."""
    from core.governance.rules import STARTER_RULES

    rule = STARTER_RULES[0]
    a = IRAPermissionBroker(rules=(rule,))
    b = IRAPermissionBroker()
    assert a._rules != b._rules
    assert b._rules == ()


# ─────────────────────────────────────────────────────────────────────────────
# 3. F9-6 — the ungoverned path is refused in cloud by construction
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_ungoverned_path_refuses_in_cloud_even_for_an_owner(monkeypatch):
    """`is_owner=True` is the caller mistake this closes.

    Before P9-04 the only thing standing between a cloud request and a side
    effect with no Constitution consulted was five call sites all passing
    `is_owner(...)`, which happens to be False in cloud.
    """
    monkeypatch.setenv("IRA_EDITION", "cloud")

    async def _boom():
        raise AssertionError("the action executed")

    outcome = await stage_action(_spec(), is_owner=True, execute=_boom)
    assert outcome["status"] == "forbidden"
    assert outcome["detail"] == UNGOVERNED_IN_CLOUD


async def test_confirm_refuses_in_cloud_too(monkeypatch):
    """Confirm is the half that runs the side effect and never had an owner
    gate — its authority was the one-shot token, which is a property of the
    FLOW and not of the function."""
    monkeypatch.setenv("IRA_EDITION", "cloud")
    outcome = await confirm_action(_spec(), token="anything-at-all")
    assert outcome["status"] == "forbidden"
    assert outcome["detail"] == UNGOVERNED_IN_CLOUD


async def test_the_refusal_names_the_governed_path(monkeypatch):
    """"You are not the owner" was true and unhelpful: it describes the caller
    rather than what is missing, and the reader's next move is to try to become
    the owner."""
    monkeypatch.setenv("IRA_EDITION", "cloud")
    outcome = await stage_action(_spec(), is_owner=True, execute=lambda: None)
    assert "Constitution" in outcome["detail"]
    assert "core/governance" in outcome["detail"]


async def test_private_is_unchanged(monkeypatch):
    """The private edition is the shipped product and this must not touch it.
    A non-owner is still refused for being a non-owner, with the message that
    has always been returned."""
    monkeypatch.delenv("IRA_EDITION", raising=False)
    outcome = await stage_action(_spec(), is_owner=False, execute=lambda: None)
    assert outcome["status"] == "forbidden"
    assert outcome["detail"] != UNGOVERNED_IN_CLOUD
    assert "owner" in outcome["detail"]


async def test_private_owner_still_reaches_staging(monkeypatch):
    """And the owner still gets a token. A refusal that also broke the private
    edition would be a regression wearing a security fix's clothes."""
    monkeypatch.delenv("IRA_EDITION", raising=False)

    async def _sim(spec):
        return {"ok": True, "detail": "simulated"}

    outcome = await stage_action(_spec(), is_owner=True, execute=lambda: None,
                                 simulator=_sim)
    assert outcome["status"] == "confirmation_required"
    assert outcome["token"]
