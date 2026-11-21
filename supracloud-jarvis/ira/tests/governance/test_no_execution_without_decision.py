"""Phase 4 acceptance — direct tool execution without a permission decision is
impossible.

This is the criterion the transformation brief states for Phase 4, and it is a
strong claim, so it is worth being precise about what is proven and what is not.

**What is proven here.** `gateway.execute` cannot run an executor unless it is
handed an `ExecutionTicket` that is validly signed, unexpired, unconsumed, and
fingerprinted for exactly the request being performed. Only `broker.evaluate`
and `broker.ticket_after_approval` mint tickets, and only for effects that
permit execution. Every other path — no ticket, a forged one, an expired one, a
replayed one, one for a different action, one carrying REQUIRE_CONFIRMATION —
refuses and the executor is never called.

**What is not proven here.** This is not a defence against an attacker who
already has arbitrary code execution inside the process: at that point they can
import a connector and call it directly, and no in-process scheme changes that.
The companion architectural test (`tests/architecture/test_governance_chokepoint.py`)
covers that direction by asserting no module calls an executor outside the
gateway — which is the realistic failure, being new code written by someone who
has not read this file.

Every test below asserts on a call COUNTER, not just on the return value. A
refusal that returns `executed=False` after having already run the side effect
would pass a naive assertion and fail the actual requirement.
"""
from __future__ import annotations

import dataclasses
import inspect
import time
import uuid

import pytest

from core.governance import build_broker, execute
from core.governance.gateway import ExecutionOutcome
from core.governance.ticket import (
    DEFAULT_TTL_SECONDS,
    ExecutionTicket,
    InvalidTicket,
    TicketError,
    TicketMismatch,
    issue,
    reset_consumed_for_tests,
)
from core.identity.principal import (
    PRIVATE_TENANT_ID,
    Principal,
    private_user_id,
)
from core.interfaces.permission import ActionRequest, Effect, RiskLevel


@pytest.fixture(autouse=True)
def _clean_tickets():
    """Each test starts with no consumed tickets, so one cannot affect another."""
    reset_consumed_for_tests()
    yield
    reset_consumed_for_tests()


def principal(tenant=None, user="owner") -> Principal:
    return Principal(
        user_id=private_user_id(user),
        tenant_id=tenant or PRIVATE_TENANT_ID,
        session_id=uuid.uuid4(),
        edition="private",
        username=user,
    )


def request_for(domain="memory", action="read", *, resource="", purpose="the user asked", p=None):
    return ActionRequest(
        principal=p or principal(),
        domain=domain,
        action=action,
        purpose=purpose,
        resource=resource,
    )


class Spy:
    """An executor that records every invocation.

    The whole suite depends on this: `executed=False` is not the property under
    test, `calls == 0` is.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return "side effect happened"


# ── The signature itself ─────────────────────────────────────────────────────


def test_execute_requires_a_ticket_argument():
    """There is no `ticket=None` default and no overload without one.

    If this ever fails, someone has added a default and the entire chokepoint is
    optional again.
    """
    sig = inspect.signature(execute)
    ticket_param = sig.parameters.get("ticket")
    assert ticket_param is not None, "gateway.execute has no `ticket` parameter"
    assert ticket_param.default is inspect.Parameter.empty, (
        "gateway.execute's `ticket` has a default value — execution without a "
        "permission decision has become possible by omission."
    )
    assert ticket_param.kind is inspect.Parameter.KEYWORD_ONLY, (
        "`ticket` must be keyword-only so it cannot be passed positionally by "
        "accident, nor omitted by a caller that mis-orders its arguments."
    )


async def test_calling_execute_without_a_ticket_is_a_type_error():
    spy = Spy()
    with pytest.raises(TypeError):
        await execute(request_for(), executor=spy)  # type: ignore[call-arg]
    assert spy.calls == 0


# ── The happy path must work, or everything below is vacuous ─────────────────


async def test_a_permitted_action_executes_exactly_once():
    broker = build_broker()
    req = request_for("memory", "read")
    outcome = broker.evaluate(req)

    assert outcome.may_execute, f"expected an allow, got {outcome.decision.reason}"
    assert outcome.ticket is not None

    spy = Spy()
    result = await execute(req, ticket=outcome.ticket, executor=spy)

    assert result.executed is True
    assert spy.calls == 1


# ── Every way of not having a valid decision ─────────────────────────────────


async def test_a_forged_ticket_is_refused():
    """A hand-constructed ticket is the obvious bypass, and it must fail.

    This is why the ticket is HMAC-signed rather than a plain dataclass: without
    the signature this test would pass execution straight through.
    """
    spy = Spy()
    forged = ExecutionTicket(
        ticket_id=uuid.uuid4(),
        fingerprint="0" * 64,
        effect=Effect.ALLOW,
        risk=RiskLevel.LOW,
        issued_at=time.time(),
        expires_at=time.time() + 999,
        signature="not-a-real-signature",
    )
    outcome = await execute(request_for(), ticket=forged, executor=spy)

    assert outcome.executed is False
    assert outcome.stage == "ticket"
    assert "signature" in outcome.reason.lower()
    assert spy.calls == 0


async def test_a_tampered_ticket_is_refused():
    """Mutating any signed field invalidates the signature.

    `dataclasses.replace` on a frozen dataclass is the realistic form of this:
    it looks like ordinary code and produces a ticket that would otherwise be
    indistinguishable from a real one.
    """
    broker = build_broker()
    req = request_for("memory", "read")
    real = broker.evaluate(req).ticket
    assert real is not None

    for field, value in (
        ("risk", RiskLevel.CRITICAL),
        ("effect", Effect.ALLOW_FOR_SESSION),
        ("expires_at", time.time() + 10_000),
        ("fingerprint", "f" * 64),
    ):
        spy = Spy()
        tampered = dataclasses.replace(real, **{field: value})
        outcome = await execute(req, ticket=tampered, executor=spy)
        assert outcome.executed is False, f"tampering with {field} was accepted"
        assert spy.calls == 0, f"executor ran after {field} was tampered with"


async def test_an_expired_ticket_is_refused():
    spy = Spy()
    req = request_for("memory", "read")
    old = issue(
        req,
        effect=Effect.ALLOW,
        risk=RiskLevel.LOW,
        ttl_seconds=1.0,
        now=time.time() - DEFAULT_TTL_SECONDS - 10,
    )
    outcome = await execute(req, ticket=old, executor=spy)

    assert outcome.executed is False
    assert "expired" in outcome.reason.lower()
    assert spy.calls == 0


async def test_a_ticket_cannot_be_replayed():
    """An approval authorises an action once, not repeatedly.

    "Send the email" approved once and sent five times is an unglamorous but
    entirely real failure mode.
    """
    broker = build_broker()
    req = request_for("memory", "read")
    ticket = broker.evaluate(req).ticket
    assert ticket is not None

    spy = Spy()
    first = await execute(req, ticket=ticket, executor=spy)
    second = await execute(req, ticket=ticket, executor=spy)
    third = await execute(req, ticket=ticket, executor=spy)

    assert first.executed is True
    assert second.executed is False and third.executed is False
    assert "already been used" in second.reason
    assert spy.calls == 1, "the executor ran more than once from one ticket"


async def test_a_ticket_for_one_action_cannot_execute_another():
    """A decision obtained for a benign action must not be redirectable.

    This is the shape of a confused-deputy attack: get an easy approval, then
    spend it on something else.
    """
    broker = build_broker()
    benign = request_for("memory", "read")
    ticket = broker.evaluate(benign).ticket
    assert ticket is not None

    spy = Spy()
    harmful = request_for("files", "delete", resource="/etc/passwd")
    outcome = await execute(harmful, ticket=ticket, executor=spy)

    assert outcome.executed is False
    assert "different action" in outcome.reason
    assert spy.calls == 0


async def test_a_ticket_cannot_cross_a_tenant_boundary():
    """Same action, same purpose, different tenant. Must not redeem.

    The fingerprint includes the tenant precisely so that a cross-tenant
    confusion bug cannot present as a valid ticket.
    """
    tenant_a = uuid.UUID("aaaaaaaa-0000-4000-8000-00000000000a")
    tenant_b = uuid.UUID("bbbbbbbb-0000-4000-8000-00000000000b")

    broker = build_broker()
    req_a = request_for("memory", "read", p=principal(tenant=tenant_a))
    ticket = broker.evaluate(req_a).ticket
    assert ticket is not None

    spy = Spy()
    req_b = request_for("memory", "read", p=principal(tenant=tenant_b))
    outcome = await execute(req_b, ticket=ticket, executor=spy)

    assert outcome.executed is False
    assert spy.calls == 0


# ── Non-permissive effects can never mint a ticket at all ────────────────────


@pytest.mark.parametrize("effect", [Effect.DENY, Effect.REQUIRE_CONFIRMATION])
def test_a_ticket_cannot_be_issued_for_a_non_permissive_effect(effect):
    """Blocks the most tempting shortcut: minting a ticket "for later"."""
    with pytest.raises(TicketError):
        issue(request_for(), effect=effect, risk=RiskLevel.LOW)


async def test_a_denied_action_yields_no_ticket_and_cannot_execute():
    broker = build_broker()
    req = request_for("payment", "execute")
    outcome = broker.evaluate(req)

    assert outcome.decision.effect is Effect.DENY
    assert outcome.ticket is None, "a denial minted a ticket"
    assert outcome.may_execute is False


async def test_an_action_needing_confirmation_yields_no_ticket():
    """'Ask the user' is not 'go ahead'.

    A caller that treats REQUIRE_CONFIRMATION as permission is the single most
    likely way this system gets defeated, so the ticket simply does not exist.
    """
    from core.governance.rules import parse_rule

    broker = build_broker(
        rules=[parse_rule({
            "domain": "email", "action": "send",
            "effect": "require_confirmation", "label": "confirm email",
        })]
    )
    outcome = broker.evaluate(request_for("email", "send"))

    assert outcome.decision.effect is Effect.REQUIRE_CONFIRMATION
    assert outcome.decision.is_allowed is False
    assert outcome.ticket is None
    assert outcome.may_execute is False


async def test_a_confirmation_ticket_cannot_be_smuggled_past_verify():
    """Even if one were constructed, `verify` refuses it explicitly."""
    req = request_for("memory", "read")
    permissive = issue(req, effect=Effect.ALLOW, risk=RiskLevel.LOW)
    smuggled = dataclasses.replace(permissive, effect=Effect.REQUIRE_CONFIRMATION)

    spy = Spy()
    outcome = await execute(req, ticket=smuggled, executor=spy)
    assert outcome.executed is False
    assert spy.calls == 0


# ── After a real approval ────────────────────────────────────────────────────


async def test_an_approved_action_executes_and_records_the_approval():
    broker = build_broker()
    req = request_for("email", "send", resource="sarah@example.com")

    first = broker.evaluate(req)
    assert first.decision.needs_confirmation, "email.send should require confirmation"
    assert first.ticket is None

    approved = broker.ticket_after_approval(req, approval_request_id="approval-123")
    spy = Spy()
    outcome = await execute(req, ticket=approved, executor=spy, simulate=False)

    assert outcome.executed is True
    assert spy.calls == 1
    assert approved.approval_request_id == "approval-123"


async def test_an_approval_cannot_release_a_denied_policy():
    """No amount of user approval unlocks something policy forbids outright."""
    broker = build_broker()
    with pytest.raises(TicketError):
        broker.ticket_after_approval(
            request_for("payment", "execute"), approval_request_id="a-1"
        )


async def test_an_approval_cannot_override_a_constitution_deny():
    """An approval obtained before a rule was written must not outlive the rule."""
    from core.governance.rules import parse_rule

    broker = build_broker(
        rules=[parse_rule({
            "domain": "files", "action": "delete", "effect": "deny",
            "label": "Never permanently delete files",
        })]
    )
    with pytest.raises(TicketError):
        broker.ticket_after_approval(
            request_for("files", "delete", resource="/tmp/x"),
            approval_request_id="a-2",
        )


# ── The refusal path must not have side effects of its own ───────────────────


async def test_a_refused_execution_returns_a_well_formed_outcome():
    """Callers branch on this, so it has to be usable rather than an exception."""
    spy = Spy()
    forged = ExecutionTicket(
        ticket_id=uuid.uuid4(), fingerprint="0" * 64, effect=Effect.ALLOW,
        risk=RiskLevel.LOW, issued_at=time.time(), expires_at=time.time() + 99,
        signature="bad",
    )
    outcome = await execute(request_for(), ticket=forged, executor=spy)

    assert isinstance(outcome, ExecutionOutcome)
    assert outcome.executed is False
    assert outcome.reason
    assert outcome.result is None
    assert spy.calls == 0
