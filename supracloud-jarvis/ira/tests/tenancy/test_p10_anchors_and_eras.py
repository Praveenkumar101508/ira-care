"""P10-04 — an anchor may only verify the chain model it was created for.

Owner decision A, implemented.

P9-02 made the ledger one chain **per tenant**: the head read is RLS-filtered,
so `prev_hash` links within a tenant and `seq` is globally gapped. Every anchor
taken before that anchored one global chain, and there is no global chain any
more.

There were three ways to handle those anchors and two of them destroy evidence:

* **reinterpret** them as tenant anchors — they would fail to verify, and be
  reported as tampering on records that are perfectly intact;
* **delete or rewrite** them so the new model fits — that is editing history to
  make a verifier happy;
* **preserve them, and say what they are.** An anchor from the global era is
  neither verified nor invalid against a tenant chain. It anchors something this
  deployment used to be.

The third is what this implements, and the answer is an explicit
`LEGACY_GLOBAL_ANCHOR/NOT_VALID_FOR_TENANT_CHAIN` rather than a boolean —
because a boolean has no room for it, and both of its values are a lie.

── Why the boundary is a table and not a constant ─────────────────────────

Migration `077` stamps the highest `seq` that existed when it ran. Every install
cut over at a different `seq`, at a different time, and a Python constant
changes with a deploy — so the one fact an auditor needs would be a property of
the code they are running rather than of the database they are auditing.

Nothing in `trust_ledger` is touched: no hash recomputed, no `tenant_id`
reinterpreted, no row rewritten.
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

from core.identity.principal import system_principal  # noqa: E402
from trust.ledger import (  # noqa: E402
    ANCHOR_LEGACY_GLOBAL,
    ANCHOR_MISMATCH,
    ANCHOR_SCOPE_LEGACY,
    ANCHOR_SCOPE_TENANT,
    ANCHOR_VERIFIED,
    ANCHOR_WRONG_TENANT,
    GENESIS,
    anchor_scope,
    append_record,
    chain_eras,
    export_head,
    records_for,
    verify_against_anchor,
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
                           tenant, f"p1004-{tenant.hex[:10]}")
    finally:
        await conn.close()
    return tenant


def _p(tenant: uuid.UUID):
    return system_principal(tenant_id=tenant, reason="p10-anchor-test")


async def _chain(tenant: uuid.UUID, n: int = 3) -> str:
    action = f"p1004-{uuid.uuid4().hex[:8]}"
    for _ in range(n):
        await append_record("action", user_id="u", action_id=action,
                            principal=_p(tenant))
    return action


# ─────────────────────────────────────────────────────────────────────────────
# 1. A tenant anchor verifies its own chain
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_fresh_anchor_verifies_the_chain_it_was_taken_from():
    tenant = await _tenant()
    action = await _chain(tenant, 3)
    anchor = await export_head(principal=_p(tenant))

    records = await records_for(action, principal=_p(tenant))
    assert verify_against_anchor(records, anchor, tenant_id=tenant,
                                 allow_partial=True) == ANCHOR_VERIFIED


async def test_a_fresh_anchor_says_which_model_it_anchors():
    """Absence of a scope means legacy, so a tenant anchor that forgot to stamp
    one would be treated as pre-cutover from the moment it was issued."""
    tenant = await _tenant()
    await _chain(tenant, 1)
    anchor = await export_head(principal=_p(tenant))
    assert anchor["scope"] == ANCHOR_SCOPE_TENANT
    assert anchor["tenant_id"] == str(tenant)
    assert anchor_scope(anchor) == ANCHOR_SCOPE_TENANT


async def test_an_anchor_from_one_tenant_does_not_verify_another():
    """A distinct answer from a mismatch, because the fix is different: use the
    right anchor, rather than investigate a breach."""
    a, b = await _tenant(), await _tenant()
    await _chain(a, 2)
    action_b = await _chain(b, 2)
    anchor_a = await export_head(principal=_p(a))

    records_b = await records_for(action_b, principal=_p(b))
    assert verify_against_anchor(records_b, anchor_a, tenant_id=b,
                                 allow_partial=True) == ANCHOR_WRONG_TENANT


async def test_a_tampered_chain_is_a_mismatch_and_says_so():
    """The one answer that means something is wrong. It must not be reachable
    by a migration or by using the wrong anchor, or a real intrusion gets
    dismissed as 'probably the old anchor thing'."""
    tenant = await _tenant()
    action = await _chain(tenant, 3)
    anchor = await export_head(principal=_p(tenant))

    records = await records_for(action, principal=_p(tenant))
    records[-1]["hash"] = "f" * 64
    assert verify_against_anchor(records, anchor, tenant_id=tenant,
                                 allow_partial=True) == ANCHOR_MISMATCH


# ─────────────────────────────────────────────────────────────────────────────
# 2. A legacy anchor is preserved, not reinterpreted
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_pre_cutover_anchor_is_neither_verified_nor_invalid():
    """The owner's decision, as one assertion.

    A pre-P9-02 anchor is a bare `{"seq", "hash"}` — there was one chain and
    nothing to disambiguate. It gets its own answer.
    """
    tenant = await _tenant()
    action = await _chain(tenant, 2)
    legacy = {"seq": 41, "hash": "b" * 64}  # no scope: that is what old ones look like

    records = await records_for(action, principal=_p(tenant))
    verdict = verify_against_anchor(records, legacy, tenant_id=tenant,
                                    allow_partial=True)
    assert verdict == ANCHOR_LEGACY_GLOBAL
    assert verdict != ANCHOR_MISMATCH, "a historical anchor was reported as tampering"
    assert verdict != ANCHOR_VERIFIED, "a global anchor was claimed to prove a tenant chain"


async def test_a_missing_scope_reads_as_legacy_not_as_tenant():
    """The silent-reinterpretation case, isolated. Defaulting the other way is
    a one-word change and turns every old anchor into a false tampering
    report."""
    assert anchor_scope({"seq": 1, "hash": "c" * 64}) == ANCHOR_SCOPE_LEGACY
    assert anchor_scope({}) == ANCHOR_SCOPE_LEGACY
    assert anchor_scope({"scope": ""}) == ANCHOR_SCOPE_LEGACY
    assert anchor_scope({"scope": "nonsense"}) == ANCHOR_SCOPE_LEGACY


async def test_an_explicit_legacy_scope_is_honoured_too():
    tenant = await _tenant()
    action = await _chain(tenant, 1)
    records = await records_for(action, principal=_p(tenant))
    anchor = {"seq": 1, "hash": "d" * 64, "scope": ANCHOR_SCOPE_LEGACY}
    assert verify_against_anchor(records, anchor, tenant_id=tenant,
                                 allow_partial=True) == ANCHOR_LEGACY_GLOBAL


def test_the_four_verdicts_are_distinct():
    """They are compared by callers, and two collapsing to one string restores
    exactly the ambiguity this batch removes."""
    assert len({ANCHOR_VERIFIED, ANCHOR_MISMATCH, ANCHOR_LEGACY_GLOBAL,
                ANCHOR_WRONG_TENANT}) == 4


def test_the_legacy_verdict_names_both_halves_of_what_it_means():
    """A caller reading this in a log must not have to know what it stands
    for."""
    assert "LEGACY_GLOBAL_ANCHOR" in ANCHOR_LEGACY_GLOBAL
    assert "NOT_VALID_FOR_TENANT_CHAIN" in ANCHOR_LEGACY_GLOBAL


# ─────────────────────────────────────────────────────────────────────────────
# 3. The cutover is recorded in the database, not in the code
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_chain_eras_are_recorded_and_readable():
    eras = await chain_eras(principal=_p(await _tenant()))
    assert [e["era"] for e in eras] == [1, 2]
    assert eras[0]["scope"] == ANCHOR_SCOPE_LEGACY
    assert eras[1]["scope"] == ANCHOR_SCOPE_TENANT


async def test_exactly_one_era_is_open():
    """Two open eras make "which era is this seq in" ambiguous, and an anchor
    decision cannot be made on an ambiguous answer. The constraint enforces it;
    this asserts the constraint is doing so."""
    eras = await chain_eras(principal=_p(await _tenant()))
    assert sum(1 for e in eras if e["ends_at_seq"] is None) == 1
    assert eras[-1]["ends_at_seq"] is None


async def test_the_boundary_actually_bounds_something():
    """Era 1 must end at or below the highest seq that exists. A boundary above
    the data would mark rows that do not exist yet as legacy."""
    tenant = await _tenant()
    await _chain(tenant, 1)
    eras = await chain_eras(principal=_p(tenant))
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        highest = await conn.fetchval("SELECT COALESCE(MAX(seq), 0) FROM trust_ledger")
    finally:
        await conn.close()
    assert eras[0]["ends_at_seq"] <= highest


async def test_the_runtime_role_cannot_move_the_boundary():
    """The process that writes the ledger must not be able to redefine which
    chain model its records belong to — the same reasoning as `076`."""
    conn = await asyncpg.connect(DSN)
    try:
        for privilege in ("UPDATE", "INSERT", "DELETE"):
            assert await conn.fetchval(
                "SELECT has_table_privilege('ira_app', "
                "'trust_ledger_chain_eras', $1)", privilege) is False, privilege
        assert await conn.fetchval(
            "SELECT has_table_privilege('ira_app', 'trust_ledger_chain_eras', "
            "'SELECT')") is True
    finally:
        await conn.close()


async def test_nothing_in_the_ledger_was_rewritten_to_fit_the_new_model():
    """`077` records a boundary and touches no row. Asserted on the migration
    text rather than on the data, because "the data still looks right" is also
    what a migration that rewrote it consistently would produce."""
    import pathlib
    import re

    here = pathlib.Path(__file__).resolve()
    sql = (here.parents[3] / "postgres" / "077_ledger_chain_eras.sql").read_text(
        encoding="utf-8")
    body = re.sub(r"--[^\n]*", "", sql)
    for forbidden in ("UPDATE trust_ledger ", "DELETE FROM trust_ledger",
                      "ALTER TABLE trust_ledger"):
        assert forbidden not in body, (
            f"077 contains {forbidden!r}. Historical evidence must remain "
            f"immutable; a cutover is recorded beside the chain, not written "
            f"into it.")


async def test_an_absent_era_table_is_unknown_and_not_assumed():
    """`chain_eras` returns `[]` when `077` has not been applied. That must not
    be read anywhere as "everything is tenant-era": an unknown boundary and a
    known one are different, and only one of them supports an anchor decision.
    """
    import trust.ledger as ledger

    async def boom(*_a, **_kw):
        raise RuntimeError("relation does not exist")

    import utils.db as db
    previous, db._pool = db._pool, None
    try:
        assert await ledger.chain_eras(principal=_p(await _tenant())) == []
    finally:
        db._pool = previous
