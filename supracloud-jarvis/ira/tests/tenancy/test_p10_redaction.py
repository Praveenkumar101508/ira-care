"""P10-03 — a redaction erases what it says it erased, in one tenant.

`redact_records` ran on a bare `acquire()`. Under `trust_ledger`'s FORCE RLS the
SELECT matched nothing, so it returned `0` and the UPDATE never ran — and `0` is
what the Privacy Centre prints as **"nothing to erase"**. Somebody exercising a
right to erasure was told their audit content had been dealt with, by a code
path that had touched no row at all.

Every other unbound read in that module returned a misleading *emptiness*. This
one returned a misleading *completion*, which is why it was split out of P10-01
rather than fixed alongside it.

`unredactable_count` is the same shape one step further on: unbound it returned
`0` — *"no residue"* — the number printed when an erasure is being reported as
total. `-1` for "could not determine" already existed; the defect was that an
unbound read never reached it, because RLS does not error, it matches nothing.

── The count was a query, not a change ────────────────────────────────────

It used to `SELECT` the matching rows, then `UPDATE` on a **wider** predicate —
every row with a `body_digest`, including ones already tombstoned — and return
the length of the SELECT. Two predicates and one number. `UPDATE … RETURNING
seq` makes the returned count the set of rows the database actually modified,
which is what "redaction cannot silently succeed when zero rows were changed"
requires.

── Why the tenant binding is load-bearing here specifically ───────────────

`trust_ledger.user_id` is TEXT — a username or an id string, not a
tenant-unique key. Two tenants can hold the same value, and
`test_redacting_one_tenant_leaves_the_others_records_alone` builds exactly that
collision. Without the binding, a redaction for `alice` in tenant A is a
redaction for every `alice` in the deployment.
"""
from __future__ import annotations

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
    REDACTION_TOMBSTONE,
    LedgerNotBound,
    append_record,
    is_redacted,
    records_for,
    redact_records,
    unredactable_count,
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
                           tenant, f"p1003-{tenant.hex[:10]}")
    finally:
        await conn.close()
    return tenant


def _p(tenant: uuid.UUID):
    return system_principal(tenant_id=tenant, reason="p10-redaction-test")


async def _seed(tenant: uuid.UUID, user: str, n: int = 3) -> str:
    action = f"p1003-{uuid.uuid4().hex[:8]}"
    for _ in range(n):
        await append_record("action", user_id=user, action_id=action,
                            record={"resource": "secret@example.test"},
                            principal=_p(tenant))
    return action


# ─────────────────────────────────────────────────────────────────────────────
# 1. It erases, and the count is the mutation
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_bound_redaction_actually_erases_content():
    tenant = await _tenant()
    action = await _seed(tenant, "alice", 3)

    assert await redact_records(user_id="alice", principal=_p(tenant)) == 3

    rows = await records_for(action, principal=_p(tenant))
    assert len(rows) == 3
    assert all(is_redacted(r["record"]) for r in rows), (
        "the redaction reported success and left the content in place")


async def test_the_count_is_what_changed_not_what_matched():
    """Redacting twice must report 3 then 0.

    The old implementation counted a SELECT and updated on a wider predicate, so
    the second call would report rows it had not changed. A number that
    describes a query rather than a change cannot be used to tell somebody what
    happened to their data.
    """
    tenant = await _tenant()
    await _seed(tenant, "bob", 3)

    assert await redact_records(user_id="bob", principal=_p(tenant)) == 3
    assert await redact_records(user_id="bob", principal=_p(tenant)) == 0


async def test_redacting_a_user_with_no_records_reports_zero_honestly():
    """Zero is a legitimate answer — when it is true. The defect was that it was
    also the answer when the read could not see anything."""
    tenant = await _tenant()
    assert await redact_records(user_id="nobody", principal=_p(tenant)) == 0


async def test_only_the_named_user_is_redacted():
    tenant = await _tenant()
    action_a = await _seed(tenant, "carol", 2)
    action_b = await _seed(tenant, "dave", 2)

    assert await redact_records(user_id="carol", principal=_p(tenant)) == 2

    assert all(is_redacted(r["record"])
               for r in await records_for(action_a, principal=_p(tenant)))
    assert not any(is_redacted(r["record"])
                   for r in await records_for(action_b, principal=_p(tenant)))


# ─────────────────────────────────────────────────────────────────────────────
# 2. One tenant's erasure is one tenant's
# ─────────────────────────────────────────────────────────────────────────────


async def test_redacting_one_tenant_leaves_the_others_records_alone():
    """The collision that makes the binding load-bearing.

    `user_id` is TEXT, so two tenants can both hold `alice`. Unbound, an erasure
    for one is an erasure for every one of them in the deployment.
    """
    a, b = await _tenant(), await _tenant()
    action_a = await _seed(a, "alice", 2)
    action_b = await _seed(b, "alice", 2)

    assert await redact_records(user_id="alice", principal=_p(a)) == 2

    assert all(is_redacted(r["record"])
               for r in await records_for(action_a, principal=_p(a)))
    assert not any(is_redacted(r["record"])
                   for r in await records_for(action_b, principal=_p(b))), (
        "redacting tenant A's 'alice' erased tenant B's 'alice' as well")


async def test_a_tenant_cannot_enumerate_another_through_the_residue_count():
    """`unredactable_count` is a count over rows, and a count is a read. If it
    were unbound it would report another tenant's v1 residue as this tenant's."""
    a, b = await _tenant(), await _tenant()
    await _seed(b, "alice", 2)
    assert await unredactable_count(user_id="alice", principal=_p(a)) == 0


async def test_redaction_cannot_be_aimed_at_another_tenant():
    """There is no tenant argument to aim. The only tenant a redaction can
    touch is the bound one, and naming a different one is refused upstream by
    `LedgerTenantMismatch` — asserted here so the absence stays deliberate."""
    import inspect

    from trust import ledger

    signature = inspect.signature(ledger.redact_records)
    assert "tenant_id" not in signature.parameters, (
        "redact_records grew a tenant argument. A caller that can name the "
        "tenant it erases is a caller that can erase somebody else's.")


# ─────────────────────────────────────────────────────────────────────────────
# 3. The chain still verifies, and v1 is still refused
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_chain_still_verifies_after_a_redaction():
    """Chain v2 hashes `prev_hash ++ body_digest`, so the body can go and every
    link still verifies. If redaction ever started touching `body_digest` or
    `hash`, this is where it shows."""
    tenant = await _tenant()
    action = await _seed(tenant, "erin", 4)
    await redact_records(user_id="erin", principal=_p(tenant))

    rows = await records_for(action, principal=_p(tenant))
    verify_chain(rows, allow_partial=True)


async def test_a_v1_record_is_left_alone_rather_than_blanked():
    """A v1 record's content is inside its hash. Erasing it would make the chain
    unverifiable from that point, and the code refuses to pretend otherwise —
    it is counted as residue instead."""
    tenant = await _tenant()
    action = f"p1003-v1-{uuid.uuid4().hex[:8]}"
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(
            "INSERT INTO trust_ledger (user_id, action_id, kind, record, "
            "prev_hash, hash, body_digest, tenant_id) "
            "VALUES ('frank',$1,'action','{\"v\":1}'::jsonb,$2,$3,NULL,$4)",
            action, "0" * 64, "a" * 64, tenant)
    finally:
        await conn.close()

    assert await redact_records(user_id="frank", principal=_p(tenant)) == 0
    assert await unredactable_count(user_id="frank", principal=_p(tenant)) == 1

    rows = await records_for(action, principal=_p(tenant))
    assert rows[0]["record"] == {"v": 1}, "a v1 record was blanked"


async def test_the_residue_is_reported_and_not_rounded_to_zero():
    """The Privacy Centre states the residue plainly rather than implying the
    deletion was total. A v1 record beside a v2 one must show up as 1."""
    tenant = await _tenant()
    await _seed(tenant, "gina", 2)
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(
            "INSERT INTO trust_ledger (user_id, action_id, kind, record, "
            "prev_hash, hash, body_digest, tenant_id) "
            "VALUES ('gina',$1,'action','{\"v\":1}'::jsonb,$2,$3,NULL,$4)",
            f"p1003-mix-{uuid.uuid4().hex[:8]}", "0" * 64, "b" * 64, tenant)
    finally:
        await conn.close()

    assert await redact_records(user_id="gina", principal=_p(tenant)) == 2
    assert await unredactable_count(user_id="gina", principal=_p(tenant)) == 1


async def test_the_tombstone_is_recognisable_as_erased_not_empty():
    """"Erased on request" and "this action had no payload" must not render the
    same, or a privacy report cannot tell them apart either."""
    tenant = await _tenant()
    action = await _seed(tenant, "hana", 1)
    await redact_records(user_id="hana", principal=_p(tenant))

    row = (await records_for(action, principal=_p(tenant)))[0]
    assert row["record"] == REDACTION_TOMBSTONE
    assert is_redacted(row["record"])
    assert row["record"] != {}


# ─────────────────────────────────────────────────────────────────────────────
# 4. No tenant context is a refusal, not a completed erasure
# ─────────────────────────────────────────────────────────────────────────────


async def test_redaction_with_no_tenant_context_refuses_in_cloud(monkeypatch):
    """The defect, stated. Returning `0` here tells somebody exercising a right
    to erasure that there was nothing to erase."""
    monkeypatch.setenv("IRA_EDITION", "cloud")
    with pytest.raises(LedgerNotBound):
        await redact_records(user_id="anyone")


async def test_the_residue_count_refuses_rather_than_reporting_none(monkeypatch):
    """`-1` means "the database could not tell me". Nobody having said whose
    records these are is not that, so it raises instead — otherwise a wiring
    defect would be reported as a transient read failure."""
    monkeypatch.setenv("IRA_EDITION", "cloud")
    with pytest.raises(LedgerNotBound):
        await unredactable_count(user_id="anyone")


async def test_the_erasure_path_passes_its_principal_through():
    """`erase_audit` already had a principal and did not pass it. Asserted on
    the call rather than on a comment: a caller that resolves a principal and
    then makes an unbound call is the shape of this whole phase."""
    import inspect

    from core.privacy import erasure

    source = inspect.getsource(erasure.erase_audit)
    assert "redact_records(" in source and "principal=principal" in source
    assert "unredactable_count(" in source


async def test_a_write_can_still_follow_a_redaction():
    """Redaction must not leave the chain unable to continue: the head it reads
    is a redacted record, and `prev_hash` is untouched by the tombstone."""
    tenant = await _tenant()
    await _seed(tenant, "iris", 2)
    await redact_records(user_id="iris", principal=_p(tenant))

    after = await append_record("action", user_id="iris",
                                action_id="after-redaction",
                                principal=_p(tenant))
    async with tenant_connection(_p(tenant)) as conn:
        rows = await conn.fetch(
            "SELECT seq, user_id, action_id, task_id, kind, record, prev_hash, "
            "hash, body_digest FROM trust_ledger ORDER BY seq ASC")
    records = []
    for row in rows:
        item = dict(row)
        if isinstance(item["record"], str):
            item["record"] = json.loads(item["record"])
        records.append(item)
    verify_chain(records)
    assert records[-1]["hash"] == after["hash"]
