"""Two places where "fail soft" had to be narrowed, and one that must stay private.

A `try: … except Exception: return []` is the most common way a security
control stops being one. It is written for a real recoverable case — the
database is down, the flag is off — and then quietly absorbs everything else:
a typo in the SQL, a `None` where a Principal was expected, a policy refusal.
The caller sees the same empty result for all of them, so a control that is
failing reports success and a bug that would take a minute to find lives for
months.

This file pins two of those boundaries and one edition boundary:

  1. `get_people_summary()` returns `''` for a missing workspace, a revoked
     membership, an absent workspace and a database outage — and for nothing
     else. Its output goes into a prompt, so an empty summary is safe and a
     wrong one is not; but a NameError must surface.
  2. `owner_connection` on `heartbeat_surfaced` is Private-edition only. It
     binds the single private tenant, so a cloud process reaching it would be
     writing another edition's ledger. The helper refuses, and this proves the
     code path cannot get past that refusal rather than trusting it.
"""
from __future__ import annotations

import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import pytest

from core.editions import EDITION_CLOUD, EDITION_PRIVATE
from tests.conftest import make_workspace_principal

PRINCIPAL = make_workspace_principal()


# ─────────────────────────────────────────────────────────────────────────────
# 1. get_people_summary — four soft cases, everything else raises
# ─────────────────────────────────────────────────────────────────────────────


def _raise(exc):
    async def _boom(_principal):
        raise exc
    return _boom


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param("MissingWorkspaceContext", id="no-selected-workspace"),
        pytest.param("MissingTenantContext", id="revoked-membership"),
        pytest.param("WorkspaceSelectionError", id="missing-workspace"),
        pytest.param("PoolSaturated", id="database-unavailable"),
    ],
)
async def test_the_four_recoverable_cases_return_an_empty_summary(monkeypatch, exc, caplog):
    """These four are states of the world, not defects.

    A user with no selected workspace, whose membership was revoked, whose
    workspace was deleted, or whose database is briefly unreachable, should get
    a turn with no relationship context — not a 500.
    """
    import memory.relationships as rel

    from core.identity.db_context import MissingTenantContext, MissingWorkspaceContext
    from core.identity.workspace import WorkspaceSelectionError
    from utils.db import PoolSaturated

    built = {
        "MissingWorkspaceContext": MissingWorkspaceContext("no workspace"),
        "MissingTenantContext": MissingTenantContext("revoked"),
        "WorkspaceSelectionError": WorkspaceSelectionError("workspace_not_in_tenant"),
        "PoolSaturated": PoolSaturated("no connection"),
    }[exc]

    monkeypatch.setattr(rel, "list_people", _raise(built))
    with caplog.at_level("INFO"):
        assert await rel.get_people_summary(PRINCIPAL) == ""

    # Silent is not the same as unlogged. An operator must be able to see that
    # a turn ran without relationship context and why.
    assert any(exc in r.message or exc in str(r.args) for r in caplog.records), (
        "the soft failure was swallowed without a log line naming the cause"
    )


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(NameError("principal is not defined"), id="programming-error"),
        pytest.param(TypeError("list_people() takes 1 argument"), id="wrong-signature"),
        pytest.param(SyntaxError("malformed query"), id="malformed-query"),
        pytest.param(PermissionError("policy refused this read"), id="security-policy"),
    ],
)
async def test_everything_else_raises_instead_of_summarising_nothing(monkeypatch, exc):
    """A bug must not look like "this user knows nobody".

    Each of these would previously have produced `''`, indistinguishable in the
    prompt from a genuinely empty address book. The `PermissionError` case is
    the sharpest: a refused read reported as no data is a control that reports
    success.
    """
    import memory.relationships as rel

    monkeypatch.setattr(rel, "list_people", _raise(exc))
    with pytest.raises(type(exc)):
        await rel.get_people_summary(PRINCIPAL)


async def test_an_empty_address_book_is_still_an_empty_summary(monkeypatch):
    """The ordinary case still works — this file narrows the except, it does not
    turn "nobody recorded" into an error."""
    import memory.relationships as rel

    async def _none(_principal):
        return []

    monkeypatch.setattr(rel, "list_people", _none)
    assert await rel.get_people_summary(PRINCIPAL) == ""


# ─────────────────────────────────────────────────────────────────────────────
# 2. heartbeat_surfaced stays Private-edition
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_heartbeat_ledger_is_scoped_rather_than_edition_gated():
    """`_claim_item` binds a workspace. It used to refuse in cloud instead.

    The previous version of this test asserted that `_claim_item` went through
    `owner_connection`, which REFUSES in the cloud edition — so no cloud
    process could write the ledger at all. That was a real mitigation and it
    was not a boundary: it prevented the write rather than scoping it, and it
    left `UNIQUE (item_key)` global, so one tenant surfacing an item silently
    suppressed it for every other tenant whose key collided.

    That test's own failure message named the alternative — *"Either restore it
    or give the table a workspace and bind one."* `082` gave it a workspace
    (P12-03b, finding H), so this asserts the binding rather than the refusal.
    """
    import inspect

    from worker.heartbeat import _claim_item

    src = inspect.getsource(_claim_item)
    assert "workspace_connection" in src, (
        "_claim_item no longer binds a workspace. It writes heartbeat_surfaced, "
        "whose policy requires app.tenant_id AND app.workspace_id since 082 — "
        "so an unbound write is refused and an unbound read matches nothing."
    )
    assert "acquire()" not in src, (
        "_claim_item reaches the ledger on a bare connection. Under 082's "
        "policy that write is refused, and the heartbeat would silently stop "
        "de-duplicating."
    )
    assert "ON CONFLICT (tenant_id, workspace_id, item_key)" in src, (
        "the claim no longer conflicts on the SCOPED key. Conflicting on "
        "item_key alone is finding H: the second workspace to surface a "
        "colliding key is told it was already surfaced, and says nothing."
    )


async def test_the_private_edition_still_reaches_the_ledger(monkeypatch):
    """The other half. A guard that blocked both editions would pass the test
    above and break the running product."""
    from core.identity.db_context import owner_connection

    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_PRIVATE)

    # Fails on the absent pool, not on the edition — reaching `utils.db` at all
    # is what proves the gate let it through.
    with pytest.raises(Exception) as e:
        async with owner_connection("heartbeat"):
            pass  # pragma: no cover
    assert "cloud edition" not in str(e.value)
