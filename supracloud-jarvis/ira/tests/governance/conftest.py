"""A working Trust Ledger for the governance unit tests.

Until P10-05 these tests ran with no database and `gateway.execute` continued
anyway, reporting `ledger_recorded: False`. That was honest and it was not
enough — nothing treated False as an error, so a governed action executed with
no audit entry and the receipt said so in a field nobody read. Phase 9 found
exactly that state in cloud, for a whole phase.

`execute` now refuses when the intent record does not land, so a suite with no
ledger is a suite where nothing executes. Two ways to fix that, and only one is
honest:

  * relax the gateway so a missing ledger is tolerated in tests — which is the
    control removing itself to keep its tests green;
  * give the tests a ledger.

This is the second. `append_record` is stubbed at the seam the gateway actually
uses, so the fail-closed path stays real and the tests that want to exercise a
BROKEN ledger switch it off explicitly (`ledger_down`) rather than inheriting a
broken one by accident.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def working_ledger(monkeypatch):
    """A ledger that accepts every append. Overridable per test.

    Records nothing and returns the shape `_record_ledger` expects. It does not
    pretend to chain or verify — the chain's own behaviour is proved against
    real PostgreSQL in `tests/tenancy/`, and a fake that tried to imitate it
    here would be a second implementation to keep in step.
    """
    appended: list[dict] = []

    async def _append(kind, *, user_id, action_id, task_id=None, record=None,
                      tenant_id=None, principal=None):
        entry = {"kind": kind, "user_id": user_id, "action_id": action_id,
                 "record": record or {}}
        appended.append(entry)
        return {**entry, "seq": len(appended), "hash": "0" * 64}

    import trust.ledger as ledger

    monkeypatch.setattr(ledger, "append_record", _append)
    return appended


@pytest.fixture
def ledger_down(monkeypatch):
    """A ledger that refuses every append, for the fail-closed tests.

    Raises rather than returning falsy: that is what an unreachable database
    and an unbound connection both do, and `_record_ledger` catching it is the
    behaviour under test.
    """
    async def _refuse(*_args, **_kwargs):
        raise RuntimeError("trust ledger unreachable")

    import trust.ledger as ledger

    monkeypatch.setattr(ledger, "append_record", _refuse)
