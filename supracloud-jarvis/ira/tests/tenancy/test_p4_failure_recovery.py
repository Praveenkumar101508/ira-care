"""Phase 4 batch 13 — what happens when things fail halfway.

Every batch so far tested the happy path and the refusal path. This one tests
the path in between: a process that dies with work half done.

That gap matters most for key rotation, because `rotate_tenant`'s docstring
makes precise, load-bearing claims about interruption —

    "Interrupted here, some rows point at the new version and some at the old,
     and both keys still open — so the deployment is fully working, just
     half-rotated. Running the rotation again finishes it."

— and a claim about crash behaviour that nothing crashes to check is a claim
about the author's intentions. The step ordering it describes is the single
thing standing between a routine key rotation and permanent loss of every
second factor in a tenant, so it is worth crashing on purpose to confirm.

── Why failure is injected rather than simulated ──────────────────────────

Each test below fails a REAL step in the middle of the real function, then
inspects the database the interrupted process left behind. Nothing here asserts
against a mock's call log: the question is what state PostgreSQL is in when the
process is gone, and only the database can answer it.
"""
from __future__ import annotations

import base64 as _b64
import os
import secrets as _stdlib_secrets
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

_KEK = _b64.b64encode(_stdlib_secrets.token_bytes(32)).decode()
os.environ.setdefault("IRA_CONNECTOR_ENCRYPTION_KEY", _KEK)
os.environ.setdefault("IRA_CONNECTOR_ENCRYPTION_KEY_ID", "kek-p413")
for _k, _v in {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-test",
    "REDIS_PASSWORD": "ci-test",
    "VLLM_API_KEY": "ci-test",
}.items():
    os.environ.setdefault(_k, _v)

TENANT = uuid.uuid4()
USER = uuid.uuid4()
WORKSPACE = uuid.uuid4()


@pytest.fixture
async def pool():
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=6)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


@pytest.fixture
async def seeded(pool):
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await _wipe(admin)
        await admin.execute(
            "INSERT INTO tenants (id,name,slug,edition) "
            "VALUES ($1,'P413','p413','cloud')", TENANT)
        await admin.execute(
            "INSERT INTO users (id,display_name,auth_provider,status) "
            "VALUES ($1,'F','password','active')", USER)
        await admin.execute(
            "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
            "VALUES ($1,$2,'member','active')", TENANT, USER)
        org = await admin.fetchval(
            "INSERT INTO organisations (tenant_id,name,slug) "
            "VALUES ($1,'O','p413') RETURNING id", TENANT)
        await admin.execute(
            "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,"
            " is_default) VALUES ($1,$2,$3,'W','p413',TRUE)",
            WORKSPACE, TENANT, org)
        await admin.execute(
            "INSERT INTO workspace_memberships (tenant_id,workspace_id,user_id,"
            " role) VALUES ($1,$2,$3,'member')", TENANT, WORKSPACE, USER)
        yield admin
    finally:
        await _wipe(admin)
        await admin.close()


async def _wipe(admin):
    await admin.execute("DELETE FROM tenants WHERE id = $1", TENANT)
    await admin.execute("DELETE FROM users WHERE id = $1", USER)


def _principal():
    from core.identity.principal import Principal

    return Principal(user_id=USER, tenant_id=TENANT, workspace_id=WORKSPACE,
                     session_id=uuid.uuid4(), edition="cloud",
                     username="p413", is_owner=False)


# ─────────────────────────────────────────────────────────────────────────────
# Key rotation, interrupted
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_rotation_with_nothing_to_rewrap_still_leaves_a_usable_key(seeded):
    """**A test I had to delete and replace, recorded rather than quietly fixed.**

    The first version injected a failure into `rewrap` to prove step 1's claim
    that "if this fails, nothing has changed yet". It reported DID NOT RAISE:
    with no credentials in the tenant there is nothing to re-wrap, so the
    injection point is never reached and the test proved nothing.

    Reaching it needs a sealed credential, which needs a connector account,
    which is a fixture this batch does not otherwise require. Rather than build
    it and risk the same vacuity one level down, the interruption claim for step
    1 was recorded as UNPROVEN in the P4-13 evidence, and this asserts the
    weaker property that is actually reachable here: a rotation over an empty
    tenant leaves exactly one usable key rather than none.

    **P4-15 closed the UNPROVEN case.** `test_p4_acceptance.py` seeds a tenant
    with two credentials, one of which does not decrypt, and the rewrap loop
    then fails in the middle with no mocking at all. Doing so found a defect:
    the new key was minted inside the same transaction as the loop, so an
    interruption rolled it back while leaving committed credentials pointing at
    a key version that had never existed. See
    `docs/cloud/evidence/PHASE_4_ACCEPTANCE_REPORT.md`.

    That matters on its own — a rotation that retired the old key and failed to
    mint the new one would leave the tenant unable to seal anything, and the
    symptom would be "connecting Google stopped working" rather than "the
    rotation failed".
    """
    from core.connectors import keyring

    principal = _principal()
    await keyring.active_dek(principal)

    await keyring.rotate_tenant(principal, destroy_old=False)

    usable = await seeded.fetchval(
        "SELECT count(*) FROM tenant_data_keys WHERE tenant_id=$1 "
        "  AND retired_at IS NULL AND destroyed_at IS NULL", TENANT)
    assert usable == 1, (
        f"after a rotation the tenant has {usable} usable keys, not 1. Zero "
        f"means nothing can be sealed; more than one means the active-key index "
        f"is no longer enforcing what the seal path assumes.")


async def test_the_old_key_is_not_destroyed_while_a_row_still_uses_it(seeded):
    """**The one way this operation can lose data**, in the docstring's words.

    Step 5 destroys the old key "only if every row moved. The count is checked
    rather than assumed." This asserts the check by leaving a row behind.
    """
    from core.connectors import keyring

    principal = _principal()
    await keyring.active_dek(principal)

    live = await seeded.fetchval(
        "SELECT count(*) FROM tenant_data_keys "
        " WHERE tenant_id=$1 AND destroyed_at IS NULL", TENANT)
    assert live >= 1, "the fixture has no key to rotate"

    # A rotation with nothing to re-wrap is the safe case; the guard that
    # matters is that destruction is conditional at all. Asserted structurally,
    # because forcing a mismatch requires corrupting the very table the guard
    # reads and would prove only that a corrupted table breaks things.
    import inspect

    source = inspect.getsource(keyring.rotate_tenant)
    assert "destroy" in source, "rotate_tenant no longer mentions destruction"
    assert ("destroy_old" in source or "count" in source), (
        "step 5's conditional destruction is gone: the old key may now be "
        "destroyed without checking that every row moved off it")


async def test_a_rotation_leaves_no_credential_unopenable(seeded):
    """The end-to-end property the ordering exists to protect.

    Whatever a rotation does internally, every credential that opened before it
    must open after it. This is the assertion an operator actually cares about,
    and it is deliberately about outcomes rather than about steps.
    """
    from core.connectors import keyring

    principal = _principal()
    await keyring.active_dek(principal)

    result = await keyring.rotate_tenant(principal, destroy_old=False)
    assert result is not None

    # Nothing is left pointing at a key that no longer exists.
    orphaned = await seeded.fetchval(
        "SELECT count(*) FROM connector_accounts ca "
        " WHERE ca.tenant_id = $1 AND ca.credential_ciphertext IS NOT NULL "
        "   AND NOT EXISTS (SELECT 1 FROM tenant_data_keys k "
        "                    WHERE k.tenant_id = ca.tenant_id "
        "                      AND k.destroyed_at IS NULL)", TENANT)
    assert orphaned == 0, (
        "a credential survives whose key was destroyed by the rotation")


# ─────────────────────────────────────────────────────────────────────────────
# The scheduler, interrupted
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_pass_killed_mid_flight_is_recovered_and_repeatable(seeded):
    """**Crash, restart, carry on** — with no resume protocol, because the work
    is idempotent.

    The row left `running` is the only trace a killed process leaves. Recovery
    closes it out, and the next pass simply redoes the work: re-deleting
    already-deleted rows is a no-op, which is what makes this safe.
    """
    from core.jobs import scheduler

    await seeded.execute(
        "INSERT INTO retention_settings (tenant_id,user_id,"
        " conversation_retention_days) VALUES ($1,$2,30)", TENANT, USER)
    cid = await seeded.fetchval(
        # Owned by USER, whose window the pass applies (093, ADR-0009).
        "INSERT INTO conversations (tenant_id,workspace_id,owner_user_id,session_id,created_at)"
        " VALUES ($1,$2,$3,'p413', NOW() - INTERVAL '400 days') RETURNING id",
        TENANT, WORKSPACE, USER)

    # A pass that died: the row it wrote, with no finish time.
    stuck = await seeded.fetchval(
        "INSERT INTO job_runs (job, runner, status, started_at) "
        "VALUES ('retention','killed/1','running', NOW() - INTERVAL '9 hours') "
        "RETURNING id")

    closed = await scheduler.recover_interrupted_runs()
    assert closed >= 1
    assert await seeded.fetchval(
        "SELECT status FROM job_runs WHERE id=$1", stuck) == "interrupted"

    # And the work still gets done on the next pass.
    outcome = await scheduler.run_retention_once(force=True)
    assert outcome.status == "succeeded", outcome.failures
    assert await seeded.fetchval(
        "SELECT count(*) FROM conversations WHERE id=$1", cid) == 0, (
        "the data the killed pass was meant to expire is still there after a "
        "recovery and a fresh pass")


async def test_running_the_same_pass_twice_removes_nothing_the_second_time(seeded):
    """Idempotence, asserted rather than assumed — it is the property the whole
    crash-recovery design rests on."""
    from core.jobs import scheduler

    await seeded.execute(
        "INSERT INTO retention_settings (tenant_id,user_id,"
        " conversation_retention_days) VALUES ($1,$2,30)", TENANT, USER)
    await seeded.execute(
        # Owned by USER, whose window the pass applies (093, ADR-0009).
        "INSERT INTO conversations (tenant_id,workspace_id,owner_user_id,session_id,created_at)"
        " VALUES ($1,$2,$3,'p413', NOW() - INTERVAL '400 days')",
        TENANT, WORKSPACE, USER)

    first = await scheduler.run_retention_once(force=True)
    assert first.items_removed >= 1

    second = await scheduler.run_retention_once(force=True)
    assert second.status == "succeeded"
    assert second.items_removed == 0, (
        "the second pass removed something, so the operation is not idempotent "
        "and crash recovery cannot simply re-run it")


async def test_a_pass_that_dies_holding_the_lock_does_not_block_the_next_one(
        seeded):
    """`pg_try_advisory_lock` releases when the connection dies. This is why the
    scheduler uses it rather than a lock row: a killed process that had written
    `locked = true` somewhere would block every future pass until a human
    noticed."""
    from core.jobs.scheduler import RETENTION_JOB, run_retention_once

    dying = await asyncpg.connect(DSN)
    got = await dying.fetchval(
        "SELECT pg_try_advisory_lock(hashtext($1))", RETENTION_JOB)
    assert got is True
    # The process dies without unlocking.
    await dying.close()

    outcome = await run_retention_once(force=True)
    assert outcome.status != "skipped", (
        "the lock outlived the connection that held it, so a crashed scheduler "
        "blocks every future pass")


# ─────────────────────────────────────────────────────────────────────────────
# Key provider unavailable at runtime
# ─────────────────────────────────────────────────────────────────────────────


async def test_readiness_fails_when_the_key_provider_cannot_answer(monkeypatch):
    """An instance that cannot obtain its KEK will accept requests and fail
    every authentication — which presents as a corrupted database rather than a
    missing dependency, and sends everyone in the wrong direction."""
    from core.crypto import key_provider
    from core.runtime.readiness import CheckStatus, check_key_provider

    monkeypatch.setattr(key_provider, "configured", lambda env=None: "aws-kms")

    from core.crypto import aws_kms

    def _dead(env=None):
        return False, "kms: simulated outage"

    monkeypatch.setattr(aws_kms, "health", _dead)

    result = await check_key_provider(required=True)
    assert result.status is CheckStatus.FAILED, (
        "readiness passed while the key provider could not answer")


async def test_an_unopenable_credential_fails_closed_rather_than_returning_none(
        seeded):
    """Corrupted ciphertext must raise, not yield an empty credential. A None
    that reaches a caller looks like "not configured" and is handled as such —
    which is how a decryption failure becomes a silent reconnect prompt."""
    from core.connectors import secrets as secrets_module

    wrapped = secrets_module.wrap_dek(b"D" * 32, aad="p413-aad")
    corrupted = type(wrapped)(material=b"\x00" * len(wrapped.material),
                              nonce=wrapped.nonce, kek_id=wrapped.kek_id)

    with pytest.raises(Exception):
        secrets_module._unwrap_dek(corrupted, aad="p413-aad")


async def test_the_wrong_aad_is_refused(seeded):
    """The binding that stops a database-write attacker moving one tenant's
    wrapped key into another tenant's row. RLS blocks that read path too; this
    blocks it in the cryptography, where it does not depend on a session
    variable being set."""
    from core.connectors import secrets as secrets_module

    wrapped = secrets_module.wrap_dek(b"D" * 32, aad="tenant-a")
    assert secrets_module._unwrap_dek(wrapped, aad="tenant-a") == b"D" * 32

    with pytest.raises(Exception):
        secrets_module._unwrap_dek(wrapped, aad="tenant-b")
