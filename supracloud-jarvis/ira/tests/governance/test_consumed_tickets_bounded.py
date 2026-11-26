"""The consumed-ticket record is bounded, and bounding it never reopens a replay.

Found while investigating the API's memory growth in the wall-clock soak: the
record of redeemed execution tickets was a set that only ever grew, one id per
governed action for the life of the process. An id only matters until its
ticket expires (an expired ticket is refused by `verify()`), so the record now
forgets the ids of expired tickets, and keeps the latest expiry it forgot so a
forgotten ticket is still refused.

Negative control: against the previous `ticket.py` (an append-only set),
`test_the_record_stays_bounded_over_many_actions` fails with 5000 entries and
`test_a_forgotten_ticket_is_still_refused_on_replay` fails because nothing is
ever forgotten; the two tests about live tickets pass on both versions, since
they guard what the change must not break.
"""
from __future__ import annotations

import time
import uuid

import pytest

from core.governance import ticket as ticket_mod
from core.governance.ticket import InvalidTicket, issue, reset_consumed_for_tests
from core.identity.principal import PRIVATE_TENANT_ID, Principal, private_user_id
from core.interfaces.permission import ActionRequest, Effect, RiskLevel


@pytest.fixture(autouse=True)
def _clean_tickets():
    reset_consumed_for_tests()
    yield
    reset_consumed_for_tests()


def _request() -> ActionRequest:
    p = Principal(user_id=private_user_id("owner"), tenant_id=PRIVATE_TENANT_ID, session_id=uuid.uuid4(),
                  edition="private", username="owner")
    return ActionRequest(principal=p, domain="memory", action="read", purpose="the user asked", resource="")


def _redeem_expired_ticket(req: ActionRequest, at: float):
    """A ticket issued and redeemed at `at`, a time already past on the wall clock
    (verify() is given the same `at`, so it accepts it); its expiry is past too."""
    t = issue(req, effect=Effect.ALLOW, risk=RiskLevel.LOW, ttl_seconds=1.0, now=at)
    t.consume(req, now=at)
    return t


#: The pruning threshold; 1024 when the module has none (the negative control).
BOUND = getattr(ticket_mod, "_PRUNE_AT", 1024)


def _size() -> int:
    return len(ticket_mod._consumed)


def test_the_record_stays_bounded_over_many_actions():
    req = _request()
    past = time.time() - 3600.0
    for i in range(5000):
        _redeem_expired_ticket(req, past + i * 0.001)
    assert _size() <= BOUND, f"consumed-ticket record holds {_size()} entries after 5000 actions"


def test_a_forgotten_ticket_is_still_refused_on_replay():
    req = _request()
    past = time.time() - 3600.0
    first = _redeem_expired_ticket(req, past)
    for i in range(1, BOUND + 10):
        _redeem_expired_ticket(req, past + i * 0.001)
    assert str(first.ticket_id) not in ticket_mod._consumed, "the first id should have been forgotten by now"
    with pytest.raises(InvalidTicket, match="already been used"):
        first.consume(req, now=past)   # the caller even claims it is still unexpired


def test_live_tickets_are_never_forgotten():
    """Pruning drops only tickets expired by the wall clock: a live ticket's replay
    is caught by its id, however many expired ones are dropped around it."""
    req = _request()
    live = issue(req, effect=Effect.ALLOW, risk=RiskLevel.LOW)
    live.consume(req)
    past = time.time() - 3600.0
    for i in range(BOUND + 10):
        _redeem_expired_ticket(req, past + i * 0.001)
    assert str(live.ticket_id) in ticket_mod._consumed
    with pytest.raises(InvalidTicket, match="already been used"):
        live.consume(req)


def test_many_live_tickets_are_all_kept():
    """With more live tickets than the pruning threshold, nothing live is dropped
    (the record grows with what is live, not with what was ever used)."""
    req = _request()
    tickets = [issue(req, effect=Effect.ALLOW, risk=RiskLevel.LOW) for _ in range(BOUND + 50)]
    for t in tickets:
        t.consume(req)
    assert _size() == len(tickets)
    for t in tickets[:: 97]:
        with pytest.raises(InvalidTicket):
            t.consume(req)
