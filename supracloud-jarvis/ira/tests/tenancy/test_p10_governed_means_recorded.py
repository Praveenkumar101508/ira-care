"""P10-05 — a governed action implies durable evidence, against a real database.

The invariant:

    GOVERNED SUCCESS  implies  REQUIRED TRUST LEDGER EVIDENCE EXISTS DURABLY

`gateway.execute` used to continue when the ledger refused and report
`ledger_recorded: False`. That was honest, and it was not enough: **nothing
treated False as an error**. Phase 9 found the resulting state — every cloud
governed action running with `ledger_recorded: False`, for a whole phase,
because the ledger write was refused by RLS and every caller was fail-soft.

── Where the refusal goes, and why there ──────────────────────────────────

The intent record is written BEFORE the side effect. That write is the last
point at which refusing costs nothing: nothing has happened, so a refusal leaves
no unrecorded action behind. One statement later the side effect has run and the
choice is gone.

So there are two halves and they are not symmetrical:

  * **intent record fails** → refuse. Nothing executes.
  * **result record fails** → the side effect already happened and cannot be
    undone. `executed` stays True, because saying otherwise would be a second
    lie to fix the first. What must not survive is the CLAIM: `governed` is
    False and the stage says `evidence_incomplete`.

The unit suite drives this with a stubbed ledger. This file drives it against
real PostgreSQL with the non-superuser role, so the refusal comes from the
policy rather than from a mock — which is the whole difference between "we
handle a failure" and "we handle THE failure".

── The cost, stated ───────────────────────────────────────────────────────

While the ledger is unreachable, no governed action runs. That is deliberate:
an audit trail that yields under load is not an audit trail. It is asserted here
rather than left as a surprise for whoever first sees a governed action refused
during a database blip.
"""
from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

from core.governance import build_broker, execute  # noqa: E402
from core.identity.principal import system_principal  # noqa: E402
from core.interfaces.permission import ActionRequest  # noqa: E402
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
    """The ledger binds from the request principal or, in private, the one
    tenant. These tests drive `execute` directly, so private is the edition
    that exercises the real binding without standing up a request."""
    monkeypatch.delenv("IRA_EDITION", raising=False)


def _request() -> ActionRequest:
    from core.identity.principal import PRIVATE_TENANT_ID

    return ActionRequest(
        principal=system_principal(tenant_id=PRIVATE_TENANT_ID, reason="p1005"),
        # `memory.read` is what the deployment baseline permits outright, so a
        # refusal in this suite is always about the LEDGER rather than about the
        # broker declining. A domain the policy gates on confirmation would make
        # every assertion below ambiguous.
        domain="memory", action="read",
        resource=f"p1005-{uuid.uuid4().hex[:8]}",
        purpose="p10-05 acceptance", model_proposed=False,
    )


def _ticket(request):
    ticket = build_broker().evaluate(request).ticket
    assert ticket is not None, "the broker refused; this suite needs a permitted action"
    return ticket


# ─────────────────────────────────────────────────────────────────────────────
# 1. The happy path leaves evidence that can be read back
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_governed_action_leaves_readable_evidence():
    """Not "the write returned True" — the record is read back through the
    bound reader. A write that reported success and a record that exists are
    different claims, and this phase has now found two places where they came
    apart."""
    request = _request()
    ticket = _ticket(request)

    outcome = await execute(request, ticket=ticket, executor=lambda: "done",
                            simulate=False)
    assert outcome.executed is True
    assert outcome.governed is True

    records = await records_for(outcome.ticket_id,
                                principal=request.principal)
    kinds = [r["kind"] for r in records]
    assert "action" in kinds, "the intent was not recorded"
    assert "execution" in kinds, "the outcome was not recorded"


async def test_the_evidence_names_the_action_it_governs():
    request = _request()
    outcome = await execute(request, ticket=_ticket(request),
                            executor=lambda: "done", simulate=False)

    records = await records_for(outcome.ticket_id, principal=request.principal)
    intent = next(r for r in records if r["kind"] == "action")
    assert intent["record"]["domain"] == "memory"
    assert intent["record"]["action"] == "read"
    assert intent["record"]["purpose"] == "p10-05 acceptance"


# ─────────────────────────────────────────────────────────────────────────────
# 2. No durable intent, no side effect
# ─────────────────────────────────────────────────────────────────────────────


async def test_an_unreachable_ledger_stops_the_action_entirely():
    """The database really down, not a mock refusing.

    The executor must not run. Anything else leaves a side effect with no
    record of having been attempted, which is the state this batch exists to
    make unreachable.
    """
    import utils.db as db

    request = _request()
    ticket = _ticket(request)

    ran = []
    previous, db._pool = db._pool, None
    try:
        outcome = await execute(request, ticket=ticket,
                                executor=lambda: ran.append(1), simulate=False)
    finally:
        db._pool = previous

    assert ran == [], "the side effect happened with no audit record"
    assert outcome.executed is False
    assert outcome.governed is False
    assert outcome.stage == "ledger"


async def test_an_unbound_ledger_stops_the_action_too(monkeypatch):
    """`LedgerNotBound` is a wiring defect rather than an outage, and both must
    refuse. A control that only fails closed on one of them is open on the
    other, and the wiring defect is the one that persists.

    ── How this test induces the unbound state, and why it changed (P9-07) ──

    It used to clear the request ContextVar and rely on cloud having no other
    source of a tenant. That worked because `gateway._record_ledger` passed NO
    principal at all — so in cloud EVERY governed action raised
    `LedgerNotBound` and refused. This test was therefore green because the
    governed path did not work, which is the same reason Phase 9 was reported
    closed.

    P9-07 threads `request.principal` into the ledger writes, so clearing the
    ContextVar no longer unbinds anything: the principal on the request is the
    binding, and it is the correct one.

    The property under test is unchanged and still worth pinning — an unbound
    ledger must refuse — so the test now induces the state that is still
    reachable: a principal that names no tenant. That is a real shape
    (`resolve_principal` can produce one) and it is the shape the refusal
    exists for.
    """
    monkeypatch.setenv("IRA_EDITION", "cloud")

    request = _request()
    ticket = _ticket(request)
    ran = []

    # Induced at the seam that raises it, because no principal can carry the
    # unbound state any more: `Principal` refuses to exist without a tenant,
    # and `system_principal` substitutes the private one. Clearing the
    # ContextVar — what this test used to do — no longer unbinds anything now
    # that the principal on the request IS the binding.
    import trust.ledger as ledger

    async def _unbound(*_args, **_kwargs):
        raise ledger.LedgerNotBound("simulated wiring defect")

    monkeypatch.setattr(ledger, "append_record", _unbound)

    outcome = await execute(request, ticket=ticket,
                            executor=lambda: ran.append(1), simulate=False)

    assert ran == [], "the side effect happened with the ledger unbound"
    assert outcome.executed is False
    assert outcome.governed is False


async def test_the_refusal_says_why_rather_than_reporting_a_generic_error():
    """A caller told "the action failed" retries. A caller told the ledger would
    not record it fixes the ledger."""
    import utils.db as db

    request = _request()
    ticket = _ticket(request)
    previous, db._pool = db._pool, None
    try:
        outcome = await execute(request, ticket=ticket, executor=lambda: "x",
                                simulate=False)
    finally:
        db._pool = previous

    assert "Trust Ledger" in outcome.reason
    assert "Nothing was executed" in outcome.reason


# ─────────────────────────────────────────────────────────────────────────────
# 3. The half that cannot be refused
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_lost_result_record_is_reported_as_ungoverned_not_as_success(
        monkeypatch):
    """Intent lands, side effect runs, result record fails.

    The action is real and cannot be undone, so `executed` is True. `governed`
    is False, and the intent record still exists — which is the point of writing
    it first: there is a trace of the attempt even when the outcome is lost.
    """
    import trust.ledger as ledger

    real_append = ledger.append_record
    calls = {"n": 0}

    async def _dies_after_the_first(kind, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return await real_append(kind, **kwargs)
        raise RuntimeError("ledger died after the side effect")

    monkeypatch.setattr(ledger, "append_record", _dies_after_the_first)

    request = _request()
    ran = []
    outcome = await execute(request, ticket=_ticket(request),
                            executor=lambda: ran.append(1), simulate=False)

    assert ran == [1], "the side effect should have happened"
    assert outcome.executed is True
    assert outcome.governed is False
    assert outcome.stage == "evidence_incomplete"

    monkeypatch.setattr(ledger, "append_record", real_append)
    records = await records_for(outcome.ticket_id, principal=request.principal)
    assert [r["kind"] for r in records] == ["action"], (
        "the intent record is what survives a lost outcome — it is written "
        "first precisely so an unrecorded action still leaves a trace")


async def test_governed_is_never_true_without_both_records():
    """The invariant, asserted directly over every outcome this suite can
    produce. A field that is only ever checked alongside `executed` is a field
    that can drift from it."""
    request = _request()
    outcome = await execute(request, ticket=_ticket(request),
                            executor=lambda: "done", simulate=False)
    records = await records_for(outcome.ticket_id, principal=request.principal)
    if outcome.governed:
        assert {"action", "execution"} <= {r["kind"] for r in records}


# ─────────────────────────────────────────────────────────────────────────────
# 4. The structural guarantee
# ─────────────────────────────────────────────────────────────────────────────


def test_no_path_returns_governed_true_without_the_ledger_flag():
    """Read as structure. Every `governed=` in the gateway must be either a
    literal False or derived from the ledger flags — a future path that set it
    True on its own would restore the exact claim this batch removed.
    """
    import inspect
    import re

    from core.governance import gateway

    source = inspect.getsource(gateway)
    for match in re.findall(r"governed=([^,\n)]+)", source):
        value = match.strip()
        assert value == "False" or "recorded" in value, (
            f"`governed={value}` is set without reference to whether the ledger "
            f"recorded anything. GOVERNED SUCCESS must imply durable evidence.")


def test_the_gateway_still_refuses_before_the_executor():
    """The ordering is the control. If the refusal ever moved below the
    executor call, every test above would still pass on the happy path and the
    side effect would run before the check.
    """
    import inspect

    from core.governance import gateway

    source = inspect.getsource(gateway.execute)
    refusal = source.index("if not intent_recorded:")
    executor = source.index("await _maybe_await(executor())")
    assert refusal < executor, (
        "the ledger refusal moved below the executor call — the side effect "
        "now happens before the evidence is confirmed")
