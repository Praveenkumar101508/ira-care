"""The audit, audited. Five plants, one per way it could report a false green.

An instrument that cannot be shown to move is not evidence, it is decoration —
and this particular instrument has already been wrong once in exactly the way
that matters: its first report listed no `worker/` module as unbound, and that
was read as "they bind" when the truth was "they never ran".

So each test below breaks something the audit is supposed to notice and asserts
that it notices. Nothing here touches production code; the plants are
constructed observations fed to the same reporting functions the real run uses.

The five:

  1. a caller that binds nothing at all;
  2. a caller that binds a STALE workspace — right shape, wrong moment;
  3. a caller bound to the WRONG workspace;
  4. an unexecuted caller being counted as safe (the original mistake);
  5. an unbound touch of a table whose predicate is NOT live, which must NOT
     be reported — a detector that fires on everything is as useless as one
     that fires on nothing.
"""
from __future__ import annotations

import uuid

import pytest

from tests import conftest_workspace_audit as audit


@pytest.fixture(autouse=True)
def isolated_audit_state(monkeypatch):
    """A private `_SEEN`, so a plant cannot pollute the real run's numbers.

    These tests run inside the same process as the audit when
    `scripts/audit/workspace_binding_audit.py` drives it, and a fabricated
    observation left behind would appear in the report as a real one.
    """
    from collections import defaultdict

    monkeypatch.setattr(audit, "_SEEN", defaultdict(
        lambda: {"bound": 0, "unbound": 0, "tables": set(),
                 "per_table": defaultdict(lambda: {"bound": 0, "unbound": 0})}))
    monkeypatch.setattr(audit, "WORKSPACE_TABLES", {"messages", "biometric_audit"})
    monkeypatch.setattr(audit, "PREDICATE_LIVE", {"messages"})


def _observe(site: str, table: str, *, bound: int = 0, unbound: int = 0) -> None:
    """Record an observation the way the asyncpg wrapper would."""
    rec = audit._SEEN[site]
    rec["tables"].add(table)
    rec["bound"] += bound
    rec["unbound"] += unbound
    rec["per_table"][table]["bound"] += bound
    rec["per_table"][table]["unbound"] += unbound


# ── Plant 1 — a caller that binds nothing ───────────────────────────────────


def test_it_reports_a_caller_that_binds_nothing():
    _observe("product/thing.py:10", "messages", unbound=3)

    found = audit.findings()
    assert [f["site"] for f in found] == ["product/thing.py:10"]
    assert found[0]["unbound"] == 3
    assert found[0]["live_tables"] == ["messages"], (
        "the finding did not name `messages` as a live-predicate table, so the "
        "report cannot distinguish a caller that reads nothing today from one "
        "that is merely unbound against a table nobody has locked down yet"
    )


# ── Plant 2 — a caller that binds, then stops ───────────────────────────────


def test_it_reports_a_caller_that_binds_most_of_the_time():
    """The shape that hides in a file-level total.

    `core/privacy/erasure.py` is the real instance: 167 bound and 27 unbound at
    the same line, because the same function is reached two ways. A report that
    only said "erasure binds" would have been true 86% of the time and useless.
    """
    _observe("product/thing.py:10", "messages", bound=167)
    _observe("product/thing.py:10", "messages", unbound=27)

    found = audit.findings()
    assert len(found) == 1
    assert found[0]["bound"] == 167 and found[0]["unbound"] == 27, (
        "a call site that is bound most of the time must still be reported; "
        "the unbound minority is the whole finding"
    )


# ── Plant 3 — bound to the WRONG workspace ──────────────────────────────────


async def test_a_wrong_workspace_binding_is_refused_by_the_connection_itself():
    """The one plant that must be run against a real database.

    `bound` versus `unbound` cannot express "bound to somebody else's
    workspace" — the GUC is set either way. That property is enforced a layer
    down, by `workspace_connection`'s check that the tenant/workspace pair
    exists, INSIDE the bound transaction and therefore under the same policies.

    This asserts that check is real, because if it were not, every observation
    the audit records as `bound` would be worth less than it looks.
    """
    import os

    dsn = os.getenv("IRA_TEST_DATABASE_DSN", "")
    if not dsn:
        pytest.skip("IRA_TEST_DATABASE_DSN not set")

    asyncpg = pytest.importorskip("asyncpg")

    import utils.db as db_module
    from core.editions import EDITION_CLOUD
    from core.identity import Principal
    from core.identity.db_context import MissingWorkspaceContext, workspace_connection

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    previous = db_module._pool
    db_module._pool = pool
    try:
        tenant_id = uuid.uuid4()
        slug = f"p1201p-{tenant_id.hex[:10]}"
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
                tenant_id, f"P12-01 plant {slug}", slug)

        # A workspace id that belongs to no tenant at all — the strongest form
        # of "wrong workspace", and the one a stale principal produces.
        impostor = Principal(
            user_id=uuid.uuid4(), tenant_id=tenant_id, session_id=uuid.uuid4(),
            edition=EDITION_CLOUD, workspace_id=uuid.uuid4())

        with pytest.raises(MissingWorkspaceContext):
            async with workspace_connection(impostor) as conn:
                await conn.fetch("SELECT 1 FROM messages LIMIT 1")
    finally:
        db_module._pool = previous
        await pool.close()


# ── Plant 4 — the original mistake, made deliberately ───────────────────────


def test_an_unexecuted_caller_is_not_counted_as_safe():
    """The failure this audit was rewritten to prevent, asserted directly.

    A file that never ran contributes no observation. The question is what the
    report does with that: `findings()` correctly does not list it, and reading
    that absence as safety is the error. So the coverage arithmetic — static
    superset minus observed — is what has to hold, and this asserts it on a
    file that is deliberately absent.
    """
    _observe("product/observed.py:10", "messages", bound=5)

    static = {"product/observed.py", "product/never_ran.py"}
    observed = {site.rsplit(":", 1)[0] for site in audit.observed()}

    assert "product/never_ran.py" not in [f["site"] for f in audit.findings()], (
        "a file that never executed appeared in the findings, which would mean "
        "the audit is inventing observations"
    )
    unaudited = static - observed
    assert unaudited == {"product/never_ran.py"}, (
        "the coverage gap must be computed as static-minus-observed and "
        "reported. If this arithmetic breaks, an unexercised caller becomes "
        "invisible rather than merely unmeasured — and invisible reads as fine."
    )


# ── Plant 5 — the detector must not fire on everything ──────────────────────


def test_an_unbound_touch_of_a_predicate_free_table_is_not_a_live_finding():
    """`biometric_audit` and `workspace_memberships` have the column, no policy.

    `voice/biometrics.py` is the real instance: entirely unbound, and correct.
    An audit that flagged it would have sent somebody to "fix" a module that
    has nothing to fix, and the next real finding would be read as more of the
    same.
    """
    _observe("voice/biometrics.py:374", "biometric_audit", unbound=1)

    found = audit.findings()
    assert len(found) == 1, "the observation should still be recorded"
    assert found[0]["live_tables"] == [], (
        "an unbound touch of a table with no workspace predicate was reported "
        "as a live finding. Nothing about that read is hidden, and calling it "
        "a defect makes the report harder to act on rather than easier."
    )
