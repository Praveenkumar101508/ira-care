"""P9-02 — the Trust Ledger writes under a tenant, or it refuses out loud.

`trust_ledger` has carried `ENABLE`/`FORCE ROW LEVEL SECURITY` with a policy
keyed on `app.tenant_id` since `023_tenancy.sql`. `trust/ledger.py:append_record`
wrote through a bare `utils.db.acquire()`, which never sets that GUC, so every
INSERT was refused by the policy's `WITH CHECK` — and all three callers wrap the
append in `except Exception`, log "trust ledger unavailable" and continue. The
tamper-evident audit record was simply always empty, and the only trace was a
line that reads like a flaky database.

That is the SECOND time this table lost every write in silence. `026_privacy.sql`
records the first: 023 made `tenant_id` NOT NULL with no default, so appends had
been failing with a not-null violation for three migrations. The pattern is the
one this phase keeps finding — **a fail-soft writer in front of a tamper-evident
record turns every schema tightening into silent data loss** — and it is why the
tests below assert the refusals as hard as they assert the writes.

── The three properties, and why each is here ─────────────────────────────

1. **A bound append lands and the stored record verifies.** Binding alone was
   not enough. `seq` is a global `BIGSERIAL` but the head read is RLS-filtered
   and therefore per tenant, so `head.seq + 1` is not the value the column
   default assigns once more than one tenant shares the sequence — and `seq` is
   inside the hashed body. Probed before the fix, on a three-tenant database:
   predicted 864, stored 867, `verify_chain` → "body digest mismatch at
   seq=867". A record that cannot verify is not evidence, so a binding that
   produced them would have replaced silent loss with silent corruption.
   `test_the_stored_record_is_the_record_that_was_hashed` is that plant made
   permanent.

2. **No tenant context refuses, loudly and by a distinguishable type.** In
   `cloud` there is no such thing as "the tenant", so `LedgerNotBound` is raised
   rather than a tenant guessed. `LedgerNotBound` is not a subclass of anything
   the callers special-case, which is the point: they can tell "our wiring is
   broken" from "the database blinked".

3. **A tenant cannot reach, extend or be attributed another tenant's chain.**
   Including the case the old `FOR UPDATE` never covered: a tenant with no rows
   yet locks no row, so two concurrent first appends would both read GENESIS and
   fork the chain. Single-tenant that was a once-per-install race. Per tenant it
   is once per new customer.

Running them: the same harness as every other file in this directory —
`IRA_TEST_DATABASE_DSN` (the `ira_app` role, non-superuser, subject to RLS) and
`IRA_TEST_ADMIN_DSN` (for creating the tenant rows a test needs). Without them
the module skips rather than passing vacuously.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

from core.identity.db_context import tenant_connection  # noqa: E402
from core.identity.principal import (  # noqa: E402
    PRIVATE_TENANT_ID,
    system_principal,
)
from trust.ledger import (  # noqa: E402
    GENESIS,
    LedgerNotBound,
    LedgerTenantMismatch,
    append_record,
    body_digest,
    verify_chain,
)

READ_COLUMNS = ("seq, user_id, action_id, task_id, kind, record, prev_hash, "
                "hash, body_digest")


@pytest.fixture(autouse=True)
async def app_pool():
    """`append_record` writes through the application pool, as it does in
    production. Supplying it here rather than stubbing the connection is what
    makes these tests run against the real policy."""
    import utils.db as db

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=6)
    previous, db._pool = db._pool, pool
    yield
    db._pool = previous
    await pool.close()


@pytest.fixture(autouse=True)
def private_edition(monkeypatch):
    """Default the edition explicitly. `current_edition()` reads the process
    environment, so a test that changes it must not leak into the next one."""
    monkeypatch.delenv("IRA_EDITION", raising=False)


async def _new_tenant() -> uuid.UUID:
    tenant = uuid.uuid4()
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute("INSERT INTO tenants (id, name, slug) VALUES ($1,$2,$2)",
                           tenant, f"led-{tenant.hex[:10]}")
    finally:
        await conn.close()
    return tenant


@pytest.fixture
async def tenant_a() -> uuid.UUID:
    return await _new_tenant()


@pytest.fixture
async def tenant_b() -> uuid.UUID:
    return await _new_tenant()


def _principal(tenant: uuid.UUID):
    return system_principal(tenant_id=tenant, reason="ledger-test")


async def _rows_for(tenant: uuid.UUID) -> list[dict]:
    """Every ledger row VISIBLE to `tenant`, oldest first, shaped for
    verify_chain. Read through `tenant_connection` on purpose: a read that
    bypassed RLS would not be reading what the tenant can read."""
    async with tenant_connection(_principal(tenant)) as conn:
        rows = await conn.fetch(
            f"SELECT {READ_COLUMNS} FROM trust_ledger ORDER BY seq ASC")
    out = []
    for row in rows:
        item = dict(row)
        item["record"] = json.loads(item["record"])
        out.append(item)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 1. A bound append lands, and what is stored is what was hashed
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_bound_append_actually_lands(tenant_a):
    """The whole defect in one assertion: before the binding this wrote nothing
    and reported success to nobody, because the caller swallowed the refusal."""
    result = await append_record("action", user_id="u", action_id="a-1",
                                 principal=_principal(tenant_a))
    assert result["tenant_id"] == str(tenant_a)
    assert len(await _rows_for(tenant_a)) == 1


async def test_the_stored_record_is_the_record_that_was_hashed(tenant_a,
                                                               tenant_b):
    """`seq` is inside the hashed body AND assigned by the database.

    Interleaving a second tenant is not decoration — it is the condition that
    made the old `head.seq + 1` wrong. With one tenant the prediction happened
    to be right, which is exactly why nothing caught it.
    """
    first = await append_record("action", user_id="u", action_id="a-1",
                                principal=_principal(tenant_a))
    await append_record("action", user_id="u", action_id="b-1",
                        principal=_principal(tenant_b))
    second = await append_record("action", user_id="u", action_id="a-2",
                                 principal=_principal(tenant_a))

    stored = await _rows_for(tenant_a)
    assert [r["seq"] for r in stored] == [first["seq"], second["seq"]], (
        "the seq the row was stored under is not the seq that was hashed — a "
        "record hashed over one seq and stored under another can never verify")
    for row, returned in zip(stored, (first, second)):
        assert body_digest({k: row[k] for k in
                            ("seq", "user_id", "action_id", "task_id", "kind",
                             "record", "prev_hash")}) == returned["body_digest"]
    verify_chain(stored)


async def test_a_tenants_chain_starts_at_genesis_and_links_to_itself(tenant_a,
                                                                     tenant_b):
    """Another tenant's appends in between must not appear in the linkage.

    The head read is RLS-filtered, so `prev_hash` chains within a tenant. If the
    policy were ever dropped, this test breaks — the chain would link across
    tenants and `verify_chain` over one tenant's visible rows would report a
    broken link.
    """
    for i in range(3):
        await append_record("action", user_id="u", action_id=f"a-{i}",
                            principal=_principal(tenant_a))
        await append_record("action", user_id="u", action_id=f"b-{i}",
                            principal=_principal(tenant_b))

    rows = await _rows_for(tenant_a)
    assert len(rows) == 3
    assert rows[0]["prev_hash"] == GENESIS
    verify_chain(rows)
    assert all(r["action_id"].startswith("a-") for r in rows)


async def test_the_returned_seq_is_the_one_a_caller_can_look_up(tenant_a):
    """`actions/gated_execution._record` returns `int(row["seq"])` and the
    Proof Card quotes it. A predicted seq would name a row that is somebody
    else's, or none at all."""
    result = await append_record("verification", user_id="u", action_id="a-1",
                                 principal=_principal(tenant_a))
    async with tenant_connection(_principal(tenant_a)) as conn:
        found = await conn.fetchval(
            "SELECT action_id FROM trust_ledger WHERE seq = $1", result["seq"])
    assert found == "a-1"


# ─────────────────────────────────────────────────────────────────────────────
# 2. No tenant context refuses — by a type a caller can tell apart
# ─────────────────────────────────────────────────────────────────────────────


async def test_cloud_with_no_tenant_context_refuses(monkeypatch):
    monkeypatch.setenv("IRA_EDITION", "cloud")
    with pytest.raises(LedgerNotBound):
        await append_record("action", user_id="u", action_id="orphan")


async def test_the_refusal_names_how_to_fix_it(monkeypatch):
    """A refusal that does not say what to pass gets worked around rather than
    fixed — and the workaround is a bare connection."""
    monkeypatch.setenv("IRA_EDITION", "cloud")
    with pytest.raises(LedgerNotBound) as exc:
        await append_record("action", user_id="u", action_id="orphan")
    message = str(exc.value)
    assert "principal" in message
    assert "default_workspace_job_principal" in message


async def test_the_refusal_is_not_an_availability_error(monkeypatch):
    """`LedgerNotBound` must not be confusable with the database being down.

    `utils.db.PoolSaturated` exists for the same reason one layer lower: a
    caller that cannot tell "we were never wired up" from "the database was
    busy" tunes the wrong thing, forever.
    """
    from utils.db import PoolSaturated

    assert not issubclass(LedgerNotBound, PoolSaturated)
    assert not issubclass(LedgerNotBound, asyncpg.PostgresError)


async def test_private_with_no_tenant_context_uses_the_one_tenant_it_has():
    """The fallback exists, is edition-gated, and lands where it claims.

    `private` has exactly one tenant and `PRIVATE_TENANT_ID` is a constant, so
    binding it is not a guess. `test_cloud_with_no_tenant_context_refuses` is
    the other half: the same code path must NOT produce a default in a process
    that serves several tenants.
    """
    result = await append_record("action", user_id="owner",
                                 action_id=f"priv-{uuid.uuid4().hex[:8]}")
    assert result["tenant_id"] == str(PRIVATE_TENANT_ID)


# ─────────────────────────────────────────────────────────────────────────────
# 3. One tenant cannot reach, extend or be attributed another's chain
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_tenant_cannot_read_another_tenants_records(tenant_a, tenant_b):
    await append_record("action", user_id="u", action_id="secret-a",
                        principal=_principal(tenant_a))
    async with tenant_connection(_principal(tenant_b)) as conn:
        seen = await conn.fetchval(
            "SELECT count(*) FROM trust_ledger WHERE action_id = 'secret-a'")
    assert seen == 0


async def test_naming_another_tenant_is_refused_not_obeyed(tenant_a, tenant_b):
    """`tenant_id=` is an assertion checked against the bound tenant.

    Obeying it would let an argument override a verified membership, which is
    the one thing `core.identity.db_context` exists to prevent. Ignoring it
    would silently discard what the caller asked for. Refusing is the only
    answer that is not a lie.
    """
    with pytest.raises(LedgerTenantMismatch):
        await append_record("action", user_id="u", action_id="crossed",
                            principal=_principal(tenant_a),
                            tenant_id=str(tenant_b))
    assert await _rows_for(tenant_b) == []


async def test_a_matching_tenant_assertion_is_allowed(tenant_a):
    """The assertion must be usable, or callers will drop it rather than keep
    it correct."""
    result = await append_record("action", user_id="u", action_id="agreed",
                                 principal=_principal(tenant_a),
                                 tenant_id=str(tenant_a))
    assert result["seq"] > 0


async def test_concurrent_first_appends_do_not_fork_a_new_tenants_chain(tenant_a):
    """The case `FOR UPDATE` alone cannot cover.

    With no rows yet there is no head row to lock, so every concurrent appender
    reads GENESIS and writes a record claiming to be first. The chain forks and
    `verify_chain` reports a broken link at the second one. Multi-tenancy turns
    this from a once-per-install race into a once-per-customer one, which is why
    it is fixed here and not left to Phase 10's concurrency work.
    """
    results = await asyncio.gather(*[
        append_record("action", user_id="u", action_id=f"race-{i}",
                      principal=_principal(tenant_a))
        for i in range(8)
    ])
    prev_hashes = [r["prev_hash"] for r in results]
    assert len(set(prev_hashes)) == len(prev_hashes), (
        "two appends claimed the same predecessor — the chain forked")
    assert prev_hashes.count(GENESIS) == 1

    rows = await _rows_for(tenant_a)
    assert len(rows) == 8
    verify_chain(rows)


async def test_concurrent_appends_by_different_tenants_stay_separate(tenant_a,
                                                                     tenant_b):
    """The lock is per tenant, so it must not merge two chains under load."""
    await asyncio.gather(*[
        append_record("action", user_id="u", action_id=f"{name}-{i}",
                      principal=_principal(tenant))
        for i in range(4)
        for name, tenant in (("a", tenant_a), ("b", tenant_b))
    ])
    for tenant in (tenant_a, tenant_b):
        rows = await _rows_for(tenant)
        assert len(rows) == 4
        assert rows[0]["prev_hash"] == GENESIS
        verify_chain(rows)


# ─────────────────────────────────────────────────────────────────────────────
# 4. The policy this all rests on is still there
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_binding_did_not_come_from_weakening_the_policy():
    """The instruction was explicit: fix it without bypassing RLS.

    A binding and a dropped policy produce identical green tests above, so the
    property is asserted directly. FORCE matters as much as ENABLE — without it
    the table owner bypasses the policy silently, and `042` gives `ira_app` its
    tables through `ira_schema_owner`.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        row = await conn.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'trust_ledger'")
        policies = await conn.fetchval(
            "SELECT count(*) FROM pg_policies WHERE tablename = 'trust_ledger' "
            "AND policyname = 'tenant_isolation'")
    finally:
        await conn.close()
    assert row["relrowsecurity"] is True
    assert row["relforcerowsecurity"] is True
    assert policies == 1


async def test_the_writing_role_is_not_a_superuser():
    """A superuser bypasses RLS entirely, so every isolation test above would
    pass while protecting nothing."""
    conn = await asyncpg.connect(DSN)
    try:
        assert await conn.fetchval(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user") is False
    finally:
        await conn.close()


async def test_an_unbound_connection_still_cannot_write(tenant_a):
    """The refusal the fix is built on. If this ever passes an INSERT, the
    binding stopped being load-bearing and `LedgerNotBound` became decoration.
    """
    import utils.db as db

    async with db.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO trust_ledger (user_id, action_id, kind, record, "
                "prev_hash, hash, tenant_id) "
                "VALUES ('u','bare','action','{}'::jsonb,$1,$2,$3)",
                GENESIS, "0" * 64, tenant_a)
