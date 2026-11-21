"""Phase 4 — the gateway drives the EXISTING safety stack, not a parallel one.

The transformation brief is explicit: *"Do not build a second disconnected
safety system."* That is easy to violate accidentally — a governance layer that
does its own simulation, its own risk grading and its own audit log would look
complete and would quietly diverge from the one that already protects IRA.

So these tests assert the wiring:

  * the ledger kinds the gateway passes are ones `trust.ledger` accepts;
  * simulation goes through `actions.shadow_world.simulate`, and a failed or
    missing simulation REFUSES the action rather than proceeding;
  * the Proof Card comes from `trust.proof_card.build_proof_card`;
  * intent is recorded BEFORE the side effect;
  * an unreachable ledger is reported honestly rather than claimed.

The ledger-kind test in particular exists because of a real bug caught during
development: the gateway originally passed `"intent"` and `"result"`, which are
not in `LEDGER_KINDS`. `append_record` raises `ValueError` on those, and because
the ledger write is deliberately fail-soft, every audit write was being
swallowed and reported as "ledger unavailable". Nothing failed; the audit trail
was simply always empty.
"""
from __future__ import annotations

import uuid

import pytest

from core.governance import build_broker, execute
from core.governance.gateway import _build_spec, _record_ledger, _summarise
from core.governance.ticket import reset_consumed_for_tests
from core.identity.principal import PRIVATE_TENANT_ID, Principal, private_user_id
from core.interfaces.permission import ActionRequest, Effect, RiskLevel


@pytest.fixture(autouse=True)
def _clean_tickets():
    reset_consumed_for_tests()
    yield
    reset_consumed_for_tests()


def principal() -> Principal:
    return Principal(
        user_id=private_user_id("owner"), tenant_id=PRIVATE_TENANT_ID,
        session_id=uuid.uuid4(), edition="private", username="owner",
    )


def req(domain="memory", action="read", *, resource="", purpose="the user asked"):
    return ActionRequest(
        principal=principal(), domain=domain, action=action,
        purpose=purpose, resource=resource,
    )


# ── The ledger ───────────────────────────────────────────────────────────────


def test_gateway_uses_real_ledger_kinds():
    """Regression test for a bug that made the audit trail silently always empty.

    `_record_ledger` is fail-soft by design, so an invalid kind does not raise —
    it is swallowed and reported as "ledger unavailable" forever. The kinds the
    gateway passes must therefore be pinned against the ledger's own set.
    """
    from trust.ledger import LEDGER_KINDS

    import inspect as _inspect

    from core.governance import gateway

    source = _inspect.getsource(gateway)
    # The kinds this module passes to _record_ledger, as written in the source.
    used = set()
    for kind in ('"action"', '"execution"', '"security_event"', '"approval"',
                 '"verification"', '"rollback"', '"intent"', '"result"'):
        if f"_record_ledger(\n        {kind}" in source or f"_record_ledger({kind}" in source:
            used.add(kind.strip('"'))

    assert used, "no ledger kinds found in gateway source — the scan is wrong"
    invalid = used - set(LEDGER_KINDS)
    assert not invalid, (
        f"gateway passes ledger kind(s) {sorted(invalid)} which trust.ledger "
        f"rejects. Because the ledger write is fail-soft, this would be "
        f"swallowed and the audit trail would be silently empty. "
        f"Valid kinds: {sorted(LEDGER_KINDS)}"
    )


async def test_an_unreachable_ledger_is_reported_not_claimed(ledger_down):
    """`_record_ledger` returns a value rather than raising, and the caller acts
    on it.

    Its contract did not change in P10-05 — what changed is `execute`, which now
    REFUSES on False instead of continuing and reporting the gap. Both halves
    are needed: a function that raised here would bypass the caller's decision,
    and a caller that ignored the return value is what the phase found in cloud.

    The failure is now injected explicitly (`ledger_down`) rather than obtained
    by having no database. Relying on absence made this test pass for a reason
    unrelated to what it claims, and would have quietly stopped testing anything
    the moment the suite gained a ledger.
    """
    assert await _record_ledger(
        "action", user_id="owner", action_id="a-1", record={"x": 1}) is False


async def test_a_working_ledger_reports_true(working_ledger):
    """The other half. A check that only ever sees False cannot tell a fail-soft
    writer from one that always fails."""
    assert await _record_ledger(
        "action", user_id="owner", action_id="a-2", record={"x": 1}) is True
    assert working_ledger[-1]["action_id"] == "a-2"


async def test_execution_reports_ledger_state_honestly():
    broker = build_broker()
    request = req()
    ticket = broker.evaluate(request).ticket
    assert ticket is not None

    outcome = await execute(request, ticket=ticket, executor=lambda: "done")
    assert outcome.executed is True
    assert outcome.ledger_recorded is True
    assert outcome.governed is True


async def test_nothing_executes_when_the_ledger_will_not_record_it(ledger_down):
    """P10-05. This test used to assert the opposite.

    It ran with no database, watched the action execute anyway, and checked that
    the outcome ADMITTED the audit entry was missing. That was honest reporting
    of an ungoverned action — and nothing treated the admission as an error, so
    the shape it blessed is precisely what Phase 9 found in cloud: every
    governed action running with `ledger_recorded: False`, for a whole phase.

    The intent write is the last point at which refusing costs nothing. One
    statement later the side effect has run and the choice is gone.
    """
    broker = build_broker()
    request = req()
    ticket = broker.evaluate(request).ticket
    assert ticket is not None

    ran = []
    outcome = await execute(request, ticket=ticket,
                            executor=lambda: ran.append(1))

    assert ran == [], "the executor ran despite the ledger refusing the intent"
    assert outcome.executed is False
    assert outcome.governed is False
    assert outcome.stage == "ledger"
    assert "cannot be governed" in outcome.reason


async def test_an_action_whose_outcome_was_not_recorded_is_not_called_governed(
        monkeypatch):
    """The half that cannot be refused.

    The intent lands, the side effect runs, and the RESULT record fails. The
    action is not undoable, so `executed` stays True — reporting otherwise would
    be a second lie to fix the first. What must not survive is the claim that it
    was governed.
    """
    import trust.ledger as ledger

    calls = {"n": 0}

    async def _first_only(kind, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"seq": 1, "hash": "0" * 64}
        raise RuntimeError("ledger died after the side effect")

    monkeypatch.setattr(ledger, "append_record", _first_only)

    broker = build_broker()
    request = req()
    ticket = broker.evaluate(request).ticket
    outcome = await execute(request, ticket=ticket, executor=lambda: "done")

    assert outcome.executed is True, "the side effect happened; saying otherwise is a lie"
    assert outcome.governed is False
    assert outcome.ledger_recorded is False
    assert outcome.stage == "evidence_incomplete"
    assert "NOT" in outcome.reason and "governed" in outcome.reason


async def test_a_refusal_is_never_reported_as_governed():
    """`governed` defaults False, and a refused action has nothing to govern.

    Asserted because the field is new: every path that returns an outcome must
    be checked once, or the default is doing work nobody verified.
    """
    from core.governance.ticket import ExecutionTicket

    broker = build_broker()
    request = req()
    ticket = broker.evaluate(request).ticket
    assert isinstance(ticket, ExecutionTicket)

    await execute(request, ticket=ticket, executor=lambda: "once")
    replayed = await execute(request, ticket=ticket, executor=lambda: "twice")
    assert replayed.executed is False
    assert replayed.governed is False


# ── Simulation ───────────────────────────────────────────────────────────────


async def test_a_side_effecting_action_is_simulated_before_it_runs(monkeypatch):
    calls: list = []

    async def fake_simulate(spec):
        calls.append(spec)
        return {"ok": True, "detail": ""}

    monkeypatch.setattr("actions.shadow_world.simulate", fake_simulate)

    broker = build_broker()
    request = req("files", "write", resource="notes.txt")
    ticket = broker.ticket_after_approval(request, approval_request_id="a-1")

    ran: list = []
    outcome = await execute(
        request, ticket=ticket, executor=lambda: ran.append(1)
    )

    assert calls, "the action was not simulated"
    assert outcome.simulated is True
    assert outcome.executed is True
    assert len(ran) == 1
    # The spec handed to Shadow World must carry the real action kind.
    assert calls[0].kind == "file_change"


async def test_a_failed_simulation_refuses_the_action(monkeypatch):
    async def refusing_simulate(spec):
        return {"ok": False, "detail": "would clobber a protected path"}

    monkeypatch.setattr("actions.shadow_world.simulate", refusing_simulate)

    broker = build_broker()
    request = req("files", "write", resource="/etc/hosts")
    ticket = broker.ticket_after_approval(request, approval_request_id="a-2")

    ran: list = []
    outcome = await execute(request, ticket=ticket, executor=lambda: ran.append(1))

    assert outcome.executed is False
    assert outcome.stage == "simulation"
    assert "protected path" in outcome.reason
    assert ran == [], "the executor ran despite a failed simulation"


async def test_a_missing_simulator_refuses_rather_than_skipping(monkeypatch):
    """Shadow World's own contract: with no registered simulator it returns
    ok=False. The gateway must preserve that, not read it as success."""
    async def unsimulated(spec):
        return {"ok": False, "detail": f"no simulator registered for {spec.kind}"}

    monkeypatch.setattr("actions.shadow_world.simulate", unsimulated)

    broker = build_broker()
    request = req("files", "write", resource="notes.txt")
    ticket = broker.ticket_after_approval(request, approval_request_id="a-3")

    ran: list = []
    outcome = await execute(request, ticket=ticket, executor=lambda: ran.append(1))

    assert outcome.executed is False
    assert ran == []


async def test_a_crashing_simulator_refuses_the_action(monkeypatch):
    """Fail closed (brief §3.5): a simulator that cannot run is not permission
    to skip simulation."""
    async def exploding(spec):
        raise RuntimeError("simulator is broken")

    monkeypatch.setattr("actions.shadow_world.simulate", exploding)

    broker = build_broker()
    request = req("files", "write", resource="notes.txt")
    ticket = broker.ticket_after_approval(request, approval_request_id="a-4")

    ran: list = []
    outcome = await execute(request, ticket=ticket, executor=lambda: ran.append(1))

    assert outcome.executed is False
    assert ran == []


async def test_a_non_side_effecting_action_is_not_simulated():
    """Reading memory has nothing to simulate; requiring it would be ceremony."""
    broker = build_broker()
    request = req("memory", "read")
    ticket = broker.evaluate(request).ticket
    assert ticket is not None

    outcome = await execute(request, ticket=ticket, executor=lambda: "x")
    assert outcome.executed is True
    assert outcome.simulated is False


# ── The ActionSpec handed to the existing components ─────────────────────────


def test_the_spec_is_the_shape_shadow_world_expects():
    from actions.shadow_world import ACTION_KINDS, ActionSpec

    broker = build_broker()
    request = req("files", "write", resource="notes.txt")
    ticket = broker.ticket_after_approval(request, approval_request_id="a-5")

    spec = _build_spec(request, ticket, "file_change")
    assert isinstance(spec, ActionSpec)
    assert spec.kind in ACTION_KINDS, "the gateway invented an action kind"
    assert spec.requested_by == "owner"
    assert "notes.txt" in spec.objects_affected


def test_every_policy_shadow_kind_is_one_shadow_world_knows():
    """A policy naming a kind Shadow World has never heard of would simulate as
    'no simulator registered' and block the action — silently, and only for that
    one action."""
    from actions.shadow_world import ACTION_KINDS

    from core.governance.policies import SEED_POLICIES

    for policy in SEED_POLICIES:
        if policy.shadow_kind is None:
            continue
        assert policy.shadow_kind in ACTION_KINDS, (
            f"{policy.domain}.{policy.action} names shadow kind "
            f"{policy.shadow_kind!r}, which Shadow World does not know"
        )


# ── Result summarisation ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected_fragment",
    [
        (None, "no result"),
        (True, "True"),
        (42, "42"),
        ([1, 2, 3], "3 item"),
        ({"a": 1, "b": 2}, "2 field"),
    ],
)
def test_result_summaries_describe_shape_not_content(value, expected_fragment):
    """023 documents `tool_calls.result_summary` as "never credentials, never
    raw connector payloads". A truncated repr is not redaction, so only the
    shape is recorded."""
    assert expected_fragment in _summarise(value)


def test_a_summary_never_contains_the_payload():
    secret = {"access_token": "ya29.SUPER-SECRET", "body": "private message text"}
    summary = _summarise(secret)
    assert "SUPER-SECRET" not in summary
    assert "private message text" not in summary


def test_a_summary_of_an_object_names_only_its_type():
    class GmailResponse:
        def __init__(self):
            self.token = "secret-token"

        def __repr__(self):
            return "GmailResponse(token='secret-token')"

    summary = _summarise(GmailResponse())
    assert summary == "GmailResponse"
    assert "secret-token" not in summary


# ── Proof Card ───────────────────────────────────────────────────────────────


async def test_the_proof_card_carries_the_governance_facts(monkeypatch):
    async def fake_simulate(spec):
        return {"ok": True, "detail": ""}

    monkeypatch.setattr("actions.shadow_world.simulate", fake_simulate)

    broker = build_broker()
    request = req("files", "write", resource="notes.txt", purpose="save the note")
    ticket = broker.ticket_after_approval(request, approval_request_id="approval-9")

    outcome = await execute(request, ticket=ticket, executor=lambda: "saved")

    assert outcome.executed is True
    card = outcome.proof_card
    assert card is not None
    governance = card.get("governance", card)
    assert governance["action"] == "files.write"
    assert governance["purpose"] == "save the note"
    assert governance["approval_request_id"] == "approval-9"
    assert governance["ticket_id"] == outcome.ticket_id


async def test_an_executor_failure_is_reported_not_swallowed():
    broker = build_broker()
    request = req("memory", "read")
    ticket = broker.evaluate(request).ticket
    assert ticket is not None

    def boom():
        raise ValueError("connector exploded")

    outcome = await execute(request, ticket=ticket, executor=boom)

    assert outcome.executed is False
    assert outcome.stage == "executor"
    assert "ValueError" in outcome.reason
    # The error type is reported; the message is not, since a connector
    # exception can carry request contents.
    assert "connector exploded" not in outcome.reason
