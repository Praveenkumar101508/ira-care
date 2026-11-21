"""P10-01 — a ledger read returns one tenant's records, or refuses.

P9-02 bound the ledger's WRITE. Its reads stayed on a bare `utils.db.acquire()`,
and under `trust_ledger`'s FORCE RLS every one of them matched no rows:

    records_for        -> []
    recent_records     -> []
    export_head        -> {"seq": 0, "hash": GENESIS}

Every one of those is a **plausible, benign-looking answer**. That is what makes
this worse than the write defect rather than a smaller version of it: the write
had a refusal underneath it — an INSERT that violated a policy — and the reads
have nothing at all. *Empty* and *nothing happened* are the same value, and the
surfaces that consume them (the Proof Card, the Action Studio, the Today
briefing) present them as facts.

── The two failures that must not look alike ──────────────────────────────

These functions are fail-soft on purpose and stay that way: a momentarily
unreachable database yields `[]` or `None`, because a Proof Card is worth more
without its records than not at all.

What they may no longer do is answer `[]` because **nobody said whose records
these are**. `LedgerNotBound` propagates through the `except Exception` rather
than being flattened, so a caller can tell "no records" from "we never asked
properly". A caller that cannot tell those apart shows an empty audit trail and
calls it clean.

── What this batch does NOT do ────────────────────────────────────────────

`redact_records` and `unredactable_count` are still unbound: they are P10-03,
and they are a different problem — a redaction that silently redacts nothing
reports a completed erasure. Splitting them out is deliberate, and
`test_the_unfixed_reads_are_still_named` keeps that honest rather than letting
the batch look finished.
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

from core.identity.principal import PRIVATE_TENANT_ID, system_principal  # noqa: E402
from trust.ledger import (  # noqa: E402
    GENESIS,
    LedgerNotBound,
    append_record,
    export_head,
    recent_records,
    records_for,
    verify_chain,
)


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
    monkeypatch.delenv("IRA_EDITION", raising=False)


async def _tenant() -> uuid.UUID:
    tenant = uuid.uuid4()
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute("INSERT INTO tenants (id, name, slug) VALUES ($1,$2,$2)",
                           tenant, f"p10-{tenant.hex[:10]}")
    finally:
        await conn.close()
    return tenant


def _p(tenant: uuid.UUID):
    return system_principal(tenant_id=tenant, reason="p10-read-test")


# ─────────────────────────────────────────────────────────────────────────────
# 1. A bound read returns what was written
# ─────────────────────────────────────────────────────────────────────────────


async def test_records_for_returns_this_tenants_records():
    tenant = await _tenant()
    action = f"p10-{uuid.uuid4().hex[:8]}"
    await append_record("action", user_id="u", action_id=action, principal=_p(tenant))
    await append_record("execution", user_id="u", action_id=action, principal=_p(tenant))

    rows = await records_for(action, principal=_p(tenant))
    assert len(rows) == 2
    assert [r["kind"] for r in rows] == ["action", "execution"]


async def test_records_for_hides_another_tenants_records():
    """The assertion the old bare read could never make, because it returned
    nothing for everybody."""
    a, b = await _tenant(), await _tenant()
    action = f"p10-{uuid.uuid4().hex[:8]}"
    await append_record("action", user_id="u", action_id=action, principal=_p(a))

    assert len(await records_for(action, principal=_p(a))) == 1
    assert await records_for(action, principal=_p(b)) == []


async def test_recent_records_is_scoped_to_the_tenant():
    a, b = await _tenant(), await _tenant()
    for i in range(3):
        await append_record("action", user_id="u", action_id=f"a-{i}-{a.hex[:6]}",
                            principal=_p(a))
    await append_record("action", user_id="u", action_id=f"b-{b.hex[:6]}",
                        principal=_p(b))

    mine = await recent_records(limit=100, principal=_p(a))
    assert len(mine) == 3
    assert all(str(r["action_id"]).startswith("a-") for r in mine)


async def test_recent_records_still_honours_its_bound():
    tenant = await _tenant()
    for i in range(5):
        await append_record("action", user_id="u", action_id=f"n-{i}",
                            principal=_p(tenant))
    assert len(await recent_records(limit=2, principal=_p(tenant))) == 2


async def test_a_bound_read_produces_a_chain_that_verifies():
    """The read and the write have to agree about what a chain is.

    `verify_chain` over a tenant's own rows must succeed from GENESIS. If the
    read ever stopped being scoped — or the write stopped being — this is where
    the two would disagree, and it would disagree loudly rather than by
    returning fewer rows.
    """
    tenant = await _tenant()
    action = f"p10-{uuid.uuid4().hex[:8]}"
    for _ in range(4):
        await append_record("action", user_id="u", action_id=action,
                            principal=_p(tenant))
    rows = await records_for(action, principal=_p(tenant))
    import json as _json
    for r in rows:
        if isinstance(r["record"], str):
            r["record"] = _json.loads(r["record"])
    assert rows[0]["prev_hash"] == GENESIS
    verify_chain(rows)


# ─────────────────────────────────────────────────────────────────────────────
# 2. The head is this tenant's head
# ─────────────────────────────────────────────────────────────────────────────


async def test_export_head_is_this_tenants_head():
    a, b = await _tenant(), await _tenant()
    written = await append_record("action", user_id="u", action_id="head-a",
                                  principal=_p(a))
    await append_record("action", user_id="u", action_id="head-b", principal=_p(b))

    head = await export_head(principal=_p(a))
    assert head["hash"] == written["hash"]
    assert head["seq"] == written["seq"]


async def test_the_head_says_which_tenant_it_is_the_head_of():
    """An anchor is worthless if it does not record what it anchors. Since the
    chain is per tenant, a bare hash cannot be checked against anything later."""
    tenant = await _tenant()
    await append_record("action", user_id="u", action_id="anchored",
                        principal=_p(tenant))
    assert (await export_head(principal=_p(tenant)))["tenant_id"] == str(tenant)


async def test_an_empty_tenant_reports_genesis_not_a_missing_head():
    """A tenant with no records has a real, correct answer, and it is not the
    same as the database being unreachable (which is None)."""
    head = await export_head(principal=_p(await _tenant()))
    assert head["seq"] == 0
    assert head["hash"] == GENESIS


# ─────────────────────────────────────────────────────────────────────────────
# 3. "Nothing to show" and "nobody said whose" are different answers
# ─────────────────────────────────────────────────────────────────────────────


async def test_reads_refuse_in_cloud_with_no_tenant_context(monkeypatch):
    """The defect, stated as three refusals.

    Before P10-01 each of these returned an empty answer that a caller renders
    as a clean, empty audit trail.
    """
    monkeypatch.setenv("IRA_EDITION", "cloud")
    with pytest.raises(LedgerNotBound):
        await records_for("anything")
    with pytest.raises(LedgerNotBound):
        await recent_records()
    with pytest.raises(LedgerNotBound):
        await export_head()


async def test_the_refusal_is_not_flattened_into_an_empty_result(monkeypatch):
    """These functions catch `Exception` so a down database cannot lose a Proof
    Card. `LedgerNotBound` has to escape that catch or the fix is invisible —
    the caller would see `[]` exactly as before, and the only difference would
    be a log line."""
    monkeypatch.setenv("IRA_EDITION", "cloud")
    try:
        result = await records_for("anything")
    except LedgerNotBound:
        return
    pytest.fail(f"the refusal was swallowed and returned {result!r}")


async def test_a_principal_with_no_tenant_is_a_refusal_not_an_empty_list():
    """The second door into the same defect, found by a plant.

    `_binding_for` runs before the try, so its refusal was never in danger of
    being flattened — which made the re-raise clause look decorative, and a
    plant that deleted it broke nothing. What IS inside the try is
    `tenant_connection`, which raises `MissingTenantContext` for a principal
    carrying no `tenant_id`. Without the clause that lands in the fail-soft
    handler and comes back as `[]`: a clean, empty audit trail for a caller who
    could not say whose it was.
    """
    from core.identity.db_context import MissingTenantContext

    class NoTenant:
        tenant_id = None
        user_id = uuid.uuid4()

    with pytest.raises(MissingTenantContext):
        await records_for("anything", principal=NoTenant())
    with pytest.raises(MissingTenantContext):
        await recent_records(principal=NoTenant())
    with pytest.raises(MissingTenantContext):
        await export_head(principal=NoTenant())


async def test_an_unreachable_database_is_still_soft():
    """The other half. If this started raising, an outage would take the Proof
    Card and the Today briefing with it — which is the behaviour the fail-soft
    catch exists to prevent, and it must survive this batch."""
    import utils.db as db

    previous, db._pool = db._pool, None
    try:
        assert await records_for("anything", principal=_p(PRIVATE_TENANT_ID)) == []
        assert await recent_records(principal=_p(PRIVATE_TENANT_ID)) == []
        assert await export_head(principal=_p(PRIVATE_TENANT_ID)) is None
    finally:
        db._pool = previous


async def test_private_still_reads_without_a_principal():
    """The private edition is the shipped product. It has one tenant, and a
    read there must keep working with no caller change — otherwise this batch
    is a regression wearing a fix's clothes."""
    action = f"p10-priv-{uuid.uuid4().hex[:8]}"
    await append_record("action", user_id="owner", action_id=action)
    assert len(await records_for(action)) == 1
    assert (await export_head())["tenant_id"] == str(PRIVATE_TENANT_ID)


# ─────────────────────────────────────────────────────────────────────────────
# 4. What is still unfixed, kept visible
# ─────────────────────────────────────────────────────────────────────────────


def test_no_ledger_function_reads_unbound():
    """P10-01's placeholder, replaced by its successor rather than widened.

    This file used to assert that `redact_records` and `unredactable_count`
    were STILL unbound, so P10-01 could not read as "the reads are fixed". It
    was written to fail when P10-03 landed, and it did.

    The instruction it carried was to delete it at that point, not to widen it —
    so what replaces it is the general property rather than a re-pointed version
    of the old one: **no function in `trust/ledger.py` reads or writes through a
    bare `acquire()`**. That stays true as the module grows, which the old form
    could not.
    """
    import pathlib
    import re

    import trust.ledger as ledger

    source = pathlib.Path(ledger.__file__).read_text(encoding="utf-8")
    # Strip docstrings and comments: the module DISCUSSES `acquire()` at length,
    # and a guard that reads prose fires on its own documentation.
    code = re.sub(r'"""[\s\S]*?"""', "", source)
    code = "\n".join(ln for ln in code.splitlines()
                     if not ln.lstrip().startswith("#"))
    offenders = [ln.strip() for ln in code.splitlines() if "acquire()" in ln]
    assert offenders == [], (
        f"trust/ledger.py still reaches the database unbound: {offenders}. "
        f"Under FORCE RLS an unbound statement does not error — it matches "
        f"nothing, and every caller renders that as an answer.")
