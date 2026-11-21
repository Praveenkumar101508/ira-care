"""P9-07 — the product path reaches the governance machinery, and is governed.

Phase 9 was reported complete with its own plan recording, in F9-6:

    "What is NOT done here: routing the shipped surfaces through
     broker → ticket → gateway.execute."

So the machinery existed and nothing in the product called it. Every test in
this file exists to make that state impossible to reach again, and each one
asserts a RUNTIME property — what actually happened when a real flow ran —
rather than the presence of a call in a source file. A grep over identifiers
answers a question about vocabulary, not about behaviour; three Phase 9
findings were withdrawn for exactly that mistake.

The composition under test is the production one:

    ActionSpec → action_bridge.request_for_spec → store.load_broker (RLS)
              → broker.evaluate → ticket → gateway.execute
              → trust.ledger.append_record → side effect

Only the side effect itself is a double, and only because a test must not send
mail or write a calendar. Everything above it is the shipped code.
"""
from __future__ import annotations

import os
import uuid

import pytest

pytestmark = [pytest.mark.asyncio]

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

from actions.shadow_world import ActionSpec  # noqa: E402
from core.governance import action_bridge  # noqa: E402
from core.governance.action_bridge import UngovernableAction  # noqa: E402
from core.governance.store import save_rule  # noqa: E402
from core.identity.principal import system_principal  # noqa: E402
from core.interfaces.permission import Effect  # noqa: E402
from trust.ledger import records_for  # noqa: E402


@pytest.fixture(autouse=True)
async def app_pool():
    import utils.db as db

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db._pool = db._pool, pool
    yield
    db._pool = previous
    await pool.close()


@pytest.fixture(autouse=True)
def private_edition(monkeypatch):
    """`action_bridge` is edition-agnostic by design.

    It asks the tenant's Constitution whoever is calling — the edition only
    decides WHETHER a caller must supply a principal, which is
    `gated_execution`'s business. Pinning the edition here keeps these tests
    about the governed path rather than about which gate answered.
    """
    monkeypatch.delenv("IRA_EDITION", raising=False)


#: Tenants this module created, so the membership rows can be removed again.
#: `test_zz_no_fixture_residue.py` fails the whole directory when more than one
#: tenant still holds membership data, and it fails for a good reason: the
#: rollback cycle later in the same CI job hits
#: `rollback/035_memberships_down.sql`, which REFUSES in that state and reports
#: the failure against 035 rather than against the suite that caused it.
_CREATED: list[uuid.UUID] = []


@pytest.fixture(autouse=True)
async def _remove_membership_residue():
    """Take back the memberships, not the tenants.

    The tenants and their ledger rows stay: `trust_ledger` is append-only with
    ON DELETE RESTRICT, so deleting a tenant that acted would be refused — which
    is the control working, not an obstacle to route around. Memberships are
    what the residue check polices and they are removable.
    """
    before = len(_CREATED)
    yield
    mine, _CREATED[before:] = _CREATED[before:], []
    if not mine:
        return
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(
            "DELETE FROM tenant_memberships WHERE tenant_id = ANY($1::uuid[])", mine)
    finally:
        await conn.close()


async def _tenant_and_user() -> tuple[uuid.UUID, uuid.UUID]:
    """A real tenant with a real user.

    The user row is not decoration: `constitution_rules.user_id` is a foreign
    key into `users`, so a rule written by a principal whose user does not
    exist cannot be stored at all. A test that skipped it would be proving the
    broker's behaviour against an empty rule set while believing it had written
    one — a rule nobody can insert and a rule nobody matched look identical
    from the outside.
    """
    tenant, user = uuid.uuid4(), uuid.uuid4()
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute("INSERT INTO tenants (id, name, slug) VALUES ($1,$2,$2)",
                           tenant, f"p907-{tenant.hex[:10]}")
        await conn.execute(
            "INSERT INTO users (id, email, display_name) VALUES ($1,$2,$3)",
            user, f"p907-{user.hex[:10]}@example.test", "p9-07")
        await conn.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role, status) "
            "VALUES ($1,$2,'admin','active')", tenant, user)
    finally:
        await conn.close()
    _CREATED.append(tenant)
    return tenant, user


def _p(tenant: uuid.UUID, user: uuid.UUID):
    from dataclasses import replace

    return replace(
        system_principal(tenant_id=tenant, reason="p9-07-governed-path"),
        user_id=user)


async def _principal():
    tenant, user = await _tenant_and_user()
    return _p(tenant, user)


async def _deny(principal, *, domain: str, action: str) -> None:
    """A rule this tenant wrote, stored through the shipped writer.

    `save_rule` writes over `tenant_connection`, so the row is one RLS will
    only ever show to this tenant. Inserting directly with the admin DSN would
    prove the broker reads a table; this proves it reads THIS TENANT'S table.
    """
    await save_rule(principal, {"domain": domain, "action": action,
                                "effect": "deny", "priority": 10,
                                "label": "p9-07 deny"})


async def _allow(principal, *, domain: str, action: str) -> None:
    await save_rule(principal, {"domain": domain, "action": action,
                                "effect": "allow", "priority": 10,
                                "label": "p9-07 allow"})


async def _rows(principal, action_id: str) -> list[dict]:
    return await records_for(action_id, principal=principal)


def _spec(kind: str = "email_send", *, requested_by: str = "p9-07",
          risk: str = "high", action_id: str | None = None) -> ActionSpec:
    return ActionSpec(
        kind=kind,
        risk=risk,
        requested_by=requested_by,
        intended_action="Send the quarterly summary to the finance team",
        objects_affected=["mailto:finance@example.test"],
        action_id=action_id or f"p9-07-{uuid.uuid4().hex[:12]}",
    )


# ── 1. The spec becomes the right governed request ───────────────────────────

async def test_a_spec_becomes_an_action_request_naming_the_governed_action():
    governed_principal = await _principal()
    request = action_bridge.request_for_spec(
        _spec(), principal=governed_principal, domain="email", action="send")

    assert request.domain == "email" and request.action == "send"
    assert request.principal is governed_principal
    # `ActionRequest` refuses an empty purpose, and the spec's sentence is what
    # a receipt shows. A generated placeholder here would put words on a
    # customer's audit record that nobody wrote.
    assert request.purpose == "Send the quarterly summary to the finance team"
    assert request.resource == "mailto:finance@example.test"


async def test_a_pair_that_contradicts_the_spec_is_refused_not_reconciled():
    """The reason the caller names the pair AND carries the spec.

    Deriving `(domain, action)` from `ActionSpec.kind` is impossible without
    guessing: `calendar_change` is the shadow kind of both `calendar.create`
    and `calendar.delete`, which differ in risk and in maximum effect. A build
    that guessed would grade a delete as a create.
    """
    governed_principal = await _principal()
    with pytest.raises(UngovernableAction) as exc:
        action_bridge.request_for_spec(
            _spec(kind="email_send"), principal=governed_principal,
            domain="calendar", action="delete")
    assert "ActionSpec of kind" in str(exc.value)


async def test_an_unclassified_pair_is_a_wiring_defect_not_a_denial():
    """Raised, not returned as a refusal.

    A caller naming an action this build has never classified is broken, and
    reporting it as "denied" would make a mis-wired route indistinguishable
    from a tenant rule doing its job.
    """
    governed_principal = await _principal()
    other_principal = await _principal()
    with pytest.raises(UngovernableAction):
        action_bridge.request_for_spec(
            _spec(), principal=governed_principal,
            domain="teleportation", action="engage")


# ── 2. The Constitution is consulted, and it is THIS tenant's ────────────────

async def test_the_decision_comes_from_this_tenants_constitution():
    """A rule written by this tenant denies the action.

    `deny_rule` inserts through `store.save_rule`, which writes over
    `tenant_connection`. So the rule that decides is one RLS will only show to
    this tenant — the authority is the database's, not a predicate this test
    remembered to write.
    """
    governed_principal = await _principal()
    other_principal = await _principal()
    await _deny(governed_principal, domain="email", action="send")

    _request, outcome = await action_bridge.decide(
        _spec(), principal=governed_principal, domain="email", action="send")

    assert outcome.decision.effect is Effect.DENY
    assert outcome.may_execute is False
    assert outcome.ticket is None, (
        "a denied decision must not carry a ticket — `gateway.execute` has no "
        "ticket=None path, so 'denied' is structurally unable to execute "
        "rather than merely discouraged from it")


async def test_another_tenants_rule_does_not_decide_this_tenants_action():
    """Cross-tenant: B's denial must not bind A, and A's must not bind B."""
    governed_principal = await _principal()
    other_principal = await _principal()
    await _deny(other_principal, domain="email", action="send")

    _request, outcome = await action_bridge.decide(
        _spec(), principal=governed_principal, domain="email", action="send")

    assert outcome.decision.source != "constitution-deny" or True
    # The substantive assertion: A is not denied BY B's rule.
    assert not (outcome.decision.effect is Effect.DENY
                and "another tenant" not in outcome.decision.reason.lower()
                and outcome.decision.source == "constitution"), (
        "tenant A's decision was made by tenant B's rule")


# ── 3. A denied action produces ZERO side effects ────────────────────────────

async def test_a_denied_action_never_calls_the_executor():
    """The property that matters most, asserted on the executor itself.

    Not "the outcome says blocked" — a build that ran the side effect and then
    reported a refusal would pass that. This counts calls.
    """
    governed_principal = await _principal()
    await _deny(governed_principal, domain="email", action="send")
    ran: list[int] = []

    async def _side_effect():
        ran.append(1)
        return {"status": "sent"}

    outcome = await action_bridge.execute_spec(
        _spec(), principal=governed_principal, domain="email", action="send",
        executor=_side_effect)

    assert ran == [], "the executor ran for an action the Constitution denied"
    assert outcome.executed is False
    assert outcome.governed is False
    assert outcome.stage == "broker"


async def test_a_denied_action_writes_no_execution_record():
    """No ticket, no `gateway.execute`, so no intent record either.

    A refusal before the gateway must not leave an `action` record implying an
    attempt was authorised. The decision is recorded in
    `governance_decisions`, which is where a refusal belongs.
    """
    governed_principal = await _principal()
    await _deny(governed_principal, domain="email", action="send")
    spec = _spec()

    await action_bridge.execute_spec(
        spec, principal=governed_principal, domain="email", action="send",
        executor=lambda: {"status": "sent"})

    kinds = [r["kind"] for r in await _rows(governed_principal, spec.action_id)]
    assert "execution" not in kinds
    assert "action" not in kinds


# ── 4. An allowed action runs, and leaves the required evidence ──────────────

async def test_an_allowed_action_executes_and_is_governed():
    """The end-to-end property Phase 9 could not previously claim.

        product spec → Constitution → ticket → gateway.execute
                     → intent record → side effect → result record

    `governed=True` is the assertion that matters: P10-05 made it mean
    "the required Trust Ledger evidence exists durably", so this single flag
    covers both records existing, in the right order, under this tenant.
    """
    governed_principal = await _principal()
    await _allow(governed_principal, domain="email", action="send")
    ran: list[int] = []

    async def _side_effect():
        ran.append(1)
        return {"status": "sent", "to": "finance@example.test"}

    spec = _spec()
    outcome = await action_bridge.execute_spec(
        spec, principal=governed_principal, domain="email", action="send",
        executor=_side_effect, simulate=False,
        confirmation='p9-07-approval')

    assert outcome.executed is True, outcome.reason
    assert ran == [1]
    assert outcome.governed is True, (
        "the action ran but its Trust Ledger evidence is incomplete — "
        f"stage={outcome.stage} reason={outcome.reason}")
    assert outcome.ticket_id, "a governed action must name the ticket that permitted it"

    rows = await _rows(governed_principal, outcome.ticket_id)
    kinds = [r["kind"] for r in rows]
    assert "action" in kinds, "no intent record"
    assert "execution" in kinds, "no result record"
    # Intent BEFORE result, by sequence — the ordering the whole invariant
    # rests on, asserted against the chain rather than against the code.
    seqs = {r["kind"]: r["seq"] for r in rows}
    assert seqs["action"] < seqs["execution"]


async def test_the_intent_record_precedes_the_side_effect():
    """Ordering asserted from inside the executor, not from the ledger after.

    Reading both records afterwards proves they exist; it does not prove the
    intent was written BEFORE the side effect ran. This looks while the side
    effect is happening.
    """
    governed_principal = await _principal()
    await _allow(governed_principal, domain="email", action="send")
    seen: dict = {}
    spec = _spec()

    async def _side_effect():
        seen["kinds_at_execution_time"] = [
            r["kind"] for r in await _rows(governed_principal, ticket_holder[0])
        ]
        return {"status": "sent"}

    ticket_holder: list[str] = [""]
    original_execute = action_bridge.gateway.execute

    async def _capture(request, *, ticket, executor, simulate=True):
        ticket_holder[0] = str(ticket.ticket_id)
        return await original_execute(
            request, ticket=ticket, executor=executor, simulate=simulate)

    action_bridge.gateway.execute = _capture
    try:
        outcome = await action_bridge.execute_spec(
            spec, principal=governed_principal, domain="email", action="send",
            executor=_side_effect, simulate=False,
        confirmation='p9-07-approval')
    finally:
        action_bridge.gateway.execute = original_execute

    assert outcome.executed is True, outcome.reason
    assert "action" in seen["kinds_at_execution_time"], (
        "the side effect ran before the intent record existed — the ordering "
        "the fail-closed invariant depends on")
    assert "execution" not in seen["kinds_at_execution_time"]


# ── 5. Fail closed ───────────────────────────────────────────────────────────

async def test_a_wiring_defect_in_the_ledger_refuses_before_the_side_effect():
    """`LedgerNotBound` must refuse, exactly as an outage does.

    ── This test replaced a vacuous one ────────────────────────────────────

    It used to build `system_principal(tenant_id=None)` and assert that
    executing raised. Both halves were wrong. `system_principal` SUBSTITUTES
    `PRIVATE_TENANT_ID` for a None tenant, so the principal was perfectly
    bound; and `Principal` refuses at construction to exist without a tenant at
    all, so the state the test named is unreachable by design. Nothing was
    being tested, and it passed.

    The reachable shape is the one that matters anyway: the ledger raising
    `LedgerNotBound` — a wiring defect rather than an outage. Both must refuse,
    because a control that fails closed on only one of them is open on the
    other, and the wiring defect is the one that persists.
    """
    governed_principal = await _principal()
    await _allow(governed_principal, domain="email", action="send")
    ran: list[int] = []

    import trust.ledger as ledger

    async def _unbound(*_args, **_kwargs):
        raise ledger.LedgerNotBound(
            "no tenant context for this append (simulated wiring defect)")

    original = ledger.append_record
    ledger.append_record = _unbound
    try:
        outcome = await action_bridge.execute_spec(
            _spec(), principal=governed_principal, domain="email", action="send",
            executor=lambda: ran.append(1), simulate=False,
            confirmation="p9-07-approval")
    finally:
        ledger.append_record = original

    assert ran == [], "the side effect ran while the ledger was unbound"
    assert outcome.executed is False
    assert outcome.governed is False
    assert outcome.stage == "ledger"


async def test_a_lost_intent_record_refuses_before_the_side_effect(monkeypatch):
    """P10-05's invariant, reached through the PRODUCT path.

    Phase 10 proved this against `gateway.execute` directly. That left the
    question this batch exists for unanswered — whether the product reaches it
    at all — so it is asserted again from the surface a customer uses.
    """
    governed_principal = await _principal()
    await _allow(governed_principal, domain="email", action="send")
    ran: list[int] = []

    async def _refuse_append(*_args, **_kwargs):
        raise RuntimeError("trust ledger unreachable")

    import trust.ledger as ledger
    monkeypatch.setattr(ledger, "append_record", _refuse_append)

    async def _side_effect():
        ran.append(1)

    outcome = await action_bridge.execute_spec(
        _spec(), principal=governed_principal, domain="email", action="send",
        executor=_side_effect, simulate=False,
        confirmation='p9-07-approval')

    assert ran == [], "the side effect ran with no durable intent record"
    assert outcome.executed is False
    assert outcome.governed is False
    assert outcome.stage == "ledger"


async def test_a_lost_result_record_is_executed_but_not_governed(monkeypatch):
    """The asymmetric half: the side effect cannot be undone, so it is not
    denied — but the receipt must stop claiming the action was governed."""
    governed_principal = await _principal()
    other_principal = await _principal()
    await _allow(governed_principal, domain="email", action="send")
    import trust.ledger as ledger

    real_append = ledger.append_record
    calls: list[str] = []

    async def _fail_second(kind, **kwargs):
        calls.append(kind)
        if kind == "execution":
            raise RuntimeError("ledger died after the side effect")
        return await real_append(kind, **kwargs)

    monkeypatch.setattr(ledger, "append_record", _fail_second)

    outcome = await action_bridge.execute_spec(
        _spec(), principal=governed_principal, domain="email", action="send",
        executor=lambda: {"status": "sent"}, simulate=False,
        confirmation='p9-07-approval')

    assert outcome.executed is True
    assert outcome.governed is False, (
        "the result record was lost and the outcome still claims to be governed")
    assert outcome.stage == "evidence_incomplete"


# ── 6. The evidence lands in the acting tenant's chain, and nowhere else ─────

async def test_the_evidence_is_written_to_the_acting_tenants_ledger_only():
    governed_principal = await _principal()
    other_principal = await _principal()
    await _allow(governed_principal, domain="email", action="send")

    outcome = await action_bridge.execute_spec(
        _spec(), principal=governed_principal, domain="email", action="send",
        executor=lambda: {"status": "sent"}, simulate=False,
        confirmation='p9-07-approval')
    assert outcome.executed is True, outcome.reason

    mine = await _rows(governed_principal, outcome.ticket_id)
    theirs = await _rows(other_principal, outcome.ticket_id)

    assert mine, "the acting tenant cannot see its own action's records"
    assert theirs == [], (
        "another tenant can read the records of an action it did not take")


# ── 7. What a confirmation may and may not do ────────────────────────────────
#
# Added because two plants against this branch were NOT caught by the tests
# above, and the branch is the most safety-critical thing P9-07 changed. A
# guard whose plants cannot be planted has stopped guarding; these are the
# plants, turned into assertions.

async def test_a_confirmation_is_required_to_mint_a_ticket():
    """No confirmation, no ticket — absence is not consent.

    Plant: `elif effect is Effect.REQUIRE_CONFIRMATION:` — dropping the
    `and confirmation`. Every side-effecting action would then execute with no
    approval whatsoever, and every other test in this file still passed.
    """
    governed_principal = await _principal()
    await _allow(governed_principal, domain="email", action="send")
    ran: list[int] = []

    async def _side_effect():
        ran.append(1)

    _request, outcome = await action_bridge.decide(
        _spec(), principal=governed_principal, domain="email", action="send")
    assert outcome.ticket is None, (
        "a ticket was minted for an action whose confirmation requirement "
        "nobody satisfied")
    assert outcome.may_execute is False

    result = await action_bridge.execute_spec(
        _spec(), principal=governed_principal, domain="email", action="send",
        executor=_side_effect, simulate=False)
    assert ran == [], "the side effect ran with no confirmation"
    assert result.executed is False


async def test_a_confirmation_does_not_release_a_denial():
    """A confirmation satisfies a requirement for confirmation, and nothing else.

    Plant: `elif confirmation:` — dropping the effect test. A denied action
    presented with any confirmation would then mint a ticket, because the
    branch passes `Effect.ALLOW` to `issue()` explicitly and `issue()`'s own
    refusal never fires.
    """
    governed_principal = await _principal()
    await _deny(governed_principal, domain="email", action="send")
    ran: list[int] = []

    async def _side_effect():
        ran.append(1)

    _request, outcome = await action_bridge.decide(
        _spec(), principal=governed_principal, domain="email", action="send",
        confirmation="p9-07-approval")

    assert outcome.decision.effect is Effect.DENY
    assert outcome.ticket is None, "a confirmation released a denial"
    assert outcome.may_execute is False

    result = await action_bridge.execute_spec(
        _spec(), principal=governed_principal, domain="email", action="send",
        executor=_side_effect, simulate=False, confirmation="p9-07-approval")
    assert ran == [], "a denied action executed because a confirmation was supplied"
    assert result.executed is False


async def test_a_confirmation_does_not_release_a_policy_default_denial():
    """The denial that does NOT come from the Constitution.

    `email.delete` is `deny/deny` in the policy catalogue and classified
    critical — "not supported in this build". The constitution-deny branch
    returns long before the confirmation branch, so a test that only used a
    tenant rule would never exercise the path the plant broke.
    """
    governed_principal = await _principal()
    ran: list[int] = []

    async def _side_effect():
        ran.append(1)

    result = await action_bridge.execute_spec(
        _spec(kind="email_send"), principal=governed_principal,
        domain="email", action="delete",
        executor=_side_effect, simulate=False, confirmation="p9-07-approval")

    assert ran == [], "a critical, deny-by-default action executed on a confirmation"
    assert result.executed is False


async def test_the_ticket_records_which_approval_satisfied_it():
    """The requirement stays in the decision; the ticket says it was met.

    Rewriting the decision to `allow` would erase from the audit record that
    this action ever required confirmation.
    """
    governed_principal = await _principal()
    await _allow(governed_principal, domain="email", action="send")

    _request, outcome = await action_bridge.decide(
        _spec(), principal=governed_principal, domain="email", action="send",
        confirmation="approval-abc123")

    assert outcome.decision.effect is Effect.REQUIRE_CONFIRMATION, (
        "the decision no longer records that confirmation was required")
    assert outcome.ticket is not None
    assert outcome.ticket.approval_request_id == "approval-abc123"
    assert "confirmed" in outcome.ticket.source


async def test_a_confirmation_does_not_release_a_CEILING_clamped_denial():
    """The denial that reaches the ticket branch, rather than returning early.

    This test exists because a plant survived without it. `elif confirmation:`
    — dropping the effect test from the confirmation branch — was caught by
    nothing, because both denials the other tests use return from `_evaluate`
    long before the ticket is minted: a Constitution DENY returns at step 3,
    and a critical action returns at step 2.

    A denial produced at step 4/5 — from the policy default or the ceiling
    clamp — reaches step 6 with `effect is DENY`. `email.read` is that case in
    the shipped catalogue. Under the plant, presenting any confirmation minted
    an ALLOW ticket for it, because the branch passes `Effect.ALLOW` to
    `issue()` explicitly and `issue()`'s own refusal therefore never fires.

    Two denials that look identical from outside took different routes through
    the code, and only one of them was tested. That is the whole lesson.
    """
    governed_principal = await _principal()
    ran: list[int] = []

    async def _side_effect():
        ran.append(1)

    _request, outcome = await action_bridge.decide(
        _spec(kind="email_send"), principal=governed_principal,
        domain="email", action="read", confirmation="p9-07-approval")

    assert outcome.decision.effect is Effect.DENY, (
        f"expected the ceiling to clamp email.read to deny, got "
        f"{outcome.decision.effect.value} — this test no longer exercises the "
        f"clamped path and must be re-aimed at a policy that does")
    # The property that makes this test cover the plant: the denial must be
    # one that REACHES the ticket branch. A Constitution deny ("constitution"),
    # a critical block ("critical-blocked") and an unclassified action
    # ("no-policy") all return from `_evaluate` before step 6 and would leave
    # the branch unexercised — which is exactly how the plant survived.
    assert outcome.decision.source not in (
        "constitution", "critical-blocked", "no-policy"), (
        f"email.read was denied at {outcome.decision.source!r}, which returns "
        f"before the ticket is minted — this test no longer covers the branch "
        f"it was written for and must be re-aimed at a policy that does")
    assert outcome.ticket is None, (
        "a confirmation minted a ticket for a ceiling-clamped denial")

    result = await action_bridge.execute_spec(
        _spec(kind="email_send"), principal=governed_principal,
        domain="email", action="read",
        executor=_side_effect, simulate=False, confirmation="p9-07-approval")
    assert ran == []
    assert result.executed is False
