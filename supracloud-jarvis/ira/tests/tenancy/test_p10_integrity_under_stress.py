"""P10-06 — the chain holds under concurrency, restart and tampering.

Everything before this batch proved a property once, in a quiet moment. This
proves the properties that only fail when something else is happening at the
same time, or after the process that wrote them has gone away.

The list is the owner's, and each item is here because a plausible
implementation passes every other test in Phase 10 and fails this one:

  * concurrent writes within one tenant — the chain forks if the head read and
    the append are not serialised;
  * concurrent writes across tenants — they must NOT serialise against each
    other, or one busy tenant stalls every other;
  * no cross-tenant `prev_hash` linkage — the head read is RLS-filtered, so a
    lost policy shows up as one tenant's chain linking into another's;
  * sequence allocation consistent with the hashed body — the P9-02 defect,
    which produced records that could never verify;
  * restart — the chain is in the database, not in a process, and a new pool
    must continue it rather than start a second one;
  * replay, deletion, body mutation, `prev_hash` mutation, `tenant_id`
    mutation, anchor mutation — each detected, and detected as the RIGHT thing;
  * a stale head — the case a retry loop produces;
  * rollback — a failed transaction must leave no seq claimed in the chain;
  * no superuser dependency and no bare connection path.

── Why the sequence case gets its own section ─────────────────────────────

P9-02 found that `head.seq + 1` is not what the column default assigns once
tenants share the sequence, and `seq` is inside the hashed body. Records landed
and could never verify:

    predicted seq=864   stored seq=867   ->   body digest mismatch at seq=867

That is the single defect this phase most needs never to recur, so it is
asserted three ways: the returned seq equals the stored seq, the stored body
still digests to its stored digest, and a chain built under concurrent load
verifies end to end.
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
from core.identity.principal import system_principal  # noqa: E402
from trust.ledger import (  # noqa: E402
    ANCHOR_MISMATCH,
    ANCHOR_VERIFIED,
    GENESIS,
    append_record,
    body_digest,
    export_head,
    records_for,
    verify_against_anchor,
    verify_chain,
)

READ = ("SELECT seq, user_id, action_id, task_id, kind, record, prev_hash, "
        "hash, body_digest FROM trust_ledger ORDER BY seq ASC")


@pytest.fixture(autouse=True)
async def app_pool():
    import utils.db as db

    pool = await asyncpg.create_pool(dsn=DSN, min_size=2, max_size=10)
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
                           tenant, f"p1006-{tenant.hex[:10]}")
    finally:
        await conn.close()
    return tenant


def _p(tenant: uuid.UUID):
    return system_principal(tenant_id=tenant, reason="p10-integrity")


async def _chain_of(tenant: uuid.UUID) -> list[dict]:
    """Everything this tenant can see, shaped for `verify_chain`."""
    async with tenant_connection(_p(tenant)) as conn:
        rows = await conn.fetch(READ)
    out = []
    for row in rows:
        item = dict(row)
        if isinstance(item["record"], str):
            item["record"] = json.loads(item["record"])
        out.append(item)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 1. Concurrency
# ─────────────────────────────────────────────────────────────────────────────


async def test_concurrent_writes_in_one_tenant_do_not_fork_the_chain():
    tenant = await _tenant()
    results = await asyncio.gather(*[
        append_record("action", user_id="u", action_id=f"c-{i}",
                      principal=_p(tenant))
        for i in range(16)
    ])
    prevs = [r["prev_hash"] for r in results]
    assert len(set(prevs)) == 16, "two appends claimed the same predecessor"
    assert prevs.count(GENESIS) == 1, "more than one record claimed to be first"

    chain = await _chain_of(tenant)
    assert len(chain) == 16
    verify_chain(chain)


async def test_concurrent_writes_across_tenants_do_not_block_each_other():
    """The lock is per tenant. If it were global, one busy tenant would stall
    every other — and a correctness fix that serialises the whole deployment is
    a different defect, not a smaller one."""
    a, b, c = await _tenant(), await _tenant(), await _tenant()
    await asyncio.gather(*[
        append_record("action", user_id="u", action_id=f"{name}-{i}",
                      principal=_p(tenant))
        for i in range(6)
        for name, tenant in (("a", a), ("b", b), ("c", c))
    ])
    for tenant in (a, b, c):
        chain = await _chain_of(tenant)
        assert len(chain) == 6
        assert chain[0]["prev_hash"] == GENESIS
        verify_chain(chain)


async def test_no_tenants_chain_links_into_another():
    """Every `prev_hash` in a tenant's chain must be GENESIS or a hash belonging
    to that same tenant. A dropped policy shows up here as a link out."""
    a, b = await _tenant(), await _tenant()
    await asyncio.gather(*[
        append_record("action", user_id="u", action_id=f"x-{i}",
                      principal=_p(t))
        for i in range(8) for t in (a, b)
    ])
    for tenant in (a, b):
        chain = await _chain_of(tenant)
        mine = {r["hash"] for r in chain}
        for record in chain:
            assert record["prev_hash"] == GENESIS or record["prev_hash"] in mine, (
                f"seq={record['seq']} links to a hash this tenant cannot see")


async def test_a_stale_head_does_not_produce_a_duplicate_link():
    """What a retry loop produces: read the head, wait, then append. The append
    must re-read under the lock rather than trusting what the caller saw."""
    tenant = await _tenant()
    await append_record("action", user_id="u", action_id="first",
                        principal=_p(tenant))
    stale = await export_head(principal=_p(tenant))

    await append_record("action", user_id="u", action_id="second",
                        principal=_p(tenant))
    third = await append_record("action", user_id="u", action_id="third",
                                principal=_p(tenant))

    assert third["prev_hash"] != stale["hash"], (
        "the append linked to a head that was already superseded")
    verify_chain(await _chain_of(tenant))


# ─────────────────────────────────────────────────────────────────────────────
# 2. Sequence allocation — the P9-02 defect, three ways
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_returned_seq_is_the_stored_seq_under_concurrency():
    tenant = await _tenant()
    results = await asyncio.gather(*[
        append_record("action", user_id="u", action_id=f"s-{i}",
                      principal=_p(tenant))
        for i in range(12)
    ])
    stored = {r["seq"]: r for r in await _chain_of(tenant)}
    for result in results:
        assert result["seq"] in stored, (
            f"seq {result['seq']} was returned to the caller and is not in the "
            f"table — the hashed seq and the stored seq have come apart again")
        assert stored[result["seq"]]["action_id"] == result["action_id"]


async def test_every_stored_body_still_digests_to_its_stored_digest():
    """The exact comparison that failed before P9-02, run over a whole chain."""
    tenant = await _tenant()
    for i in range(6):
        await append_record("action", user_id="u", action_id=f"d-{i}",
                            record={"n": i}, principal=_p(tenant))
    for record in await _chain_of(tenant):
        body = {k: record[k] for k in
                ("seq", "user_id", "action_id", "task_id", "kind", "record",
                 "prev_hash")}
        assert body_digest(body) == record["body_digest"], (
            f"seq={record['seq']} was hashed over a different body than the one "
            f"stored")


async def test_a_rolled_back_transaction_claims_no_place_in_the_chain():
    """A failed append burns a sequence value — that is what a sequence does —
    but it must not leave a gap the chain cannot cross. `verify_chain` checks
    linkage rather than contiguity, and this is where that matters."""
    tenant = await _tenant()
    await append_record("action", user_id="u", action_id="before",
                        principal=_p(tenant))

    with pytest.raises(ValueError):
        await append_record("not_a_real_kind", user_id="u", action_id="doomed",
                            principal=_p(tenant))

    after = await append_record("action", user_id="u", action_id="after",
                                principal=_p(tenant))
    chain = await _chain_of(tenant)
    assert [r["action_id"] for r in chain] == ["before", "after"]
    assert chain[-1]["hash"] == after["hash"]
    verify_chain(chain)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Restart
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_chain_continues_across_a_pool_restart():
    """The chain lives in the database, not in a process. A new pool must
    CONTINUE it — a fresh process that started a second chain from GENESIS
    would verify perfectly in isolation and be a fork."""
    import utils.db as db

    tenant = await _tenant()
    before = await append_record("action", user_id="u", action_id="pre-restart",
                                 principal=_p(tenant))

    old, db._pool = db._pool, await asyncpg.create_pool(dsn=DSN, min_size=1,
                                                        max_size=4)
    try:
        after = await append_record("action", user_id="u",
                                    action_id="post-restart",
                                    principal=_p(tenant))
    finally:
        await db._pool.close()
        db._pool = old

    assert after["prev_hash"] == before["hash"], (
        "the chain restarted from GENESIS after a pool restart")
    verify_chain(await _chain_of(tenant))


async def test_an_anchor_survives_a_restart():
    """Anchors are held outside the database precisely so a restart cannot
    rewrite them. The anchor taken before must still verify after."""
    import utils.db as db

    tenant = await _tenant()
    for i in range(3):
        await append_record("action", user_id="u", action_id=f"a-{i}",
                            principal=_p(tenant))
    anchor = await export_head(principal=_p(tenant))

    old, db._pool = db._pool, await asyncpg.create_pool(dsn=DSN, min_size=1,
                                                        max_size=4)
    try:
        chain = await _chain_of(tenant)
        assert verify_against_anchor(chain, anchor, tenant_id=tenant) \
            == ANCHOR_VERIFIED
    finally:
        await db._pool.close()
        db._pool = old


# ─────────────────────────────────────────────────────────────────────────────
# 4. Tampering — detected, and detected as the right thing
# ─────────────────────────────────────────────────────────────────────────────


async def _tampered(tenant: uuid.UUID, mutate) -> list[dict]:
    chain = await _chain_of(tenant)
    mutate(chain)
    return chain


async def test_a_mutated_body_is_detected():
    tenant = await _tenant()
    for i in range(4):
        await append_record("action", user_id="u", action_id=f"t-{i}",
                            record={"n": i}, principal=_p(tenant))

    def mutate(chain):
        chain[2]["record"] = {"n": 999}

    with pytest.raises(ValueError, match="body digest mismatch"):
        verify_chain(await _tampered(tenant, mutate))


async def test_a_mutated_prev_hash_is_detected_as_a_broken_link():
    """A different failure from a mutated body, and it must say so: one means
    a record was edited, the other that the ORDER was."""
    tenant = await _tenant()
    for i in range(4):
        await append_record("action", user_id="u", action_id=f"p-{i}",
                            principal=_p(tenant))

    def mutate(chain):
        chain[2]["prev_hash"] = "f" * 64

    with pytest.raises(ValueError, match="broken prev_hash"):
        verify_chain(await _tampered(tenant, mutate))


async def test_a_deleted_record_is_detected():
    """Removal breaks the linkage of the record that followed it. The append-only
    trigger stops this in the database; this proves the CHAIN would notice even
    if the trigger did not."""
    tenant = await _tenant()
    for i in range(5):
        await append_record("action", user_id="u", action_id=f"del-{i}",
                            principal=_p(tenant))

    def mutate(chain):
        del chain[2]

    with pytest.raises(ValueError, match="broken prev_hash"):
        verify_chain(await _tampered(tenant, mutate))


async def test_a_reordered_chain_is_detected():
    tenant = await _tenant()
    for i in range(4):
        await append_record("action", user_id="u", action_id=f"r-{i}",
                            principal=_p(tenant))

    def mutate(chain):
        chain[1], chain[2] = chain[2], chain[1]

    with pytest.raises(ValueError):
        verify_chain(await _tampered(tenant, mutate))


async def test_a_mutated_hash_is_detected():
    tenant = await _tenant()
    for i in range(4):
        await append_record("action", user_id="u", action_id=f"h-{i}",
                            principal=_p(tenant))

    def mutate(chain):
        chain[1]["hash"] = "e" * 64

    with pytest.raises(ValueError):
        verify_chain(await _tampered(tenant, mutate))


async def test_a_mutated_anchor_is_a_mismatch_not_a_pass():
    """The anchor is the one part held outside the database, so it is the one
    part an attacker with database access cannot change — and the one a
    careless operator can. A wrong anchor must not verify."""
    tenant = await _tenant()
    for i in range(3):
        await append_record("action", user_id="u", action_id=f"an-{i}",
                            principal=_p(tenant))
    anchor = await export_head(principal=_p(tenant))
    anchor["hash"] = "c" * 64

    assert verify_against_anchor(await _chain_of(tenant), anchor,
                                 tenant_id=tenant) == ANCHOR_MISMATCH


async def test_a_mutated_tenant_id_moves_a_record_out_of_its_own_chain():
    """The database refuses this outright — `trust_ledger` is append-only, so
    UPDATE cannot reach `tenant_id` at all. Proved rather than assumed, because
    a row whose tenant changed would appear in another tenant's chain and break
    its linkage."""
    tenant, other = await _tenant(), await _tenant()
    await append_record("action", user_id="u", action_id="fixed-tenant",
                        principal=_p(tenant))

    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        with pytest.raises(asyncpg.exceptions.RaiseError):
            await conn.execute(
                "UPDATE trust_ledger SET tenant_id = $1 WHERE action_id = $2",
                other, "fixed-tenant")
    finally:
        await conn.close()

    assert len(await records_for("fixed-tenant", principal=_p(tenant))) == 1
    assert await records_for("fixed-tenant", principal=_p(other)) == []


async def test_a_replayed_record_cannot_be_inserted_over_an_existing_seq():
    """Replay: take a real record and write it again. `seq` is the primary key,
    so the database refuses — asserted because "the chain would notice" and "the
    row cannot exist" are different strengths and this one is the stronger."""
    tenant = await _tenant()
    original = await append_record("action", user_id="u", action_id="replayed",
                                   principal=_p(tenant))

    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                "INSERT INTO trust_ledger (seq, user_id, action_id, kind, "
                "record, prev_hash, hash, body_digest, tenant_id) "
                "VALUES ($1,'u','replayed','action','{}'::jsonb,$2,$3,$4,$5)",
                original["seq"], original["prev_hash"], original["hash"],
                original["body_digest"], tenant)
    finally:
        await conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 5. No hidden authority
# ─────────────────────────────────────────────────────────────────────────────


async def test_none_of_this_needed_a_superuser():
    """Every append above went through the application pool. If that role could
    bypass RLS, every isolation assertion in this file would pass while
    protecting nothing."""
    conn = await asyncpg.connect(DSN)
    try:
        assert await conn.fetchval(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user") is False
        assert await conn.fetchval(
            "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user") is False
    finally:
        await conn.close()


async def test_rls_is_still_enabled_and_forced_after_all_of_this():
    """Asserted at the END of the stress, not the start. A test that checked the
    policy first and then hammered the table would not notice a code path that
    turned it off along the way."""
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        row = await conn.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'trust_ledger'")
    finally:
        await conn.close()
    assert row["relrowsecurity"] is True
    assert row["relforcerowsecurity"] is True
