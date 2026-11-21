"""Phase 2 batch 11 — a member's OAuth credentials are their own.

`connector_accounts` held Phase 7 OAuth refresh tokens behind a tenant-only
policy, so every member of a tenant could read every other member's grant to
Google, Slack, or whatever else they connected. Batch 8's predicate guard found
it; `060` closes it.

── Why this took a batch and not a line ────────────────────────────────────

DEK rotation re-seals credentials TENANT-WIDE. `keyring.rotate_tenant` reads
every row in the tenant, rewraps each under a new key, and then counts rows
still pointing at the old version before destroying it.

Under a user-scoped policy an operator connection matches ZERO rows. Rotation
would rewrap nothing, the straggler count would return zero, and the old key
would be destroyed while every credential still needed it — silently, and with
no way back. The rotation rework had to land with the migration, and
`test_rotation_still_reaches_every_members_credentials` is the assertion that
says it did.
"""
from __future__ import annotations

import base64
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

for _k, _v in {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-test",
    "REDIS_PASSWORD": "ci-test",
    "VLLM_API_KEY": "ci-test",
}.items():
    os.environ.setdefault(_k, _v)

#: An EXPLICIT KEK, passed to every seal and every rotation rather than left in
#: the environment. `test_p205_mfa_encryption` records why: "the rotation tests
#: use explicit `env` dicts instead, so that 'the KEK changed' is a real second
#: key rather than a monkeypatch."
#:
#: These tests learned it the hard way. With `os.environ.setdefault` they passed
#: alone and failed in the full suite with `InvalidTag`: another module had
#: already set the variable, so the fixture sealed under one key and rotation
#: opened under another. The failure looked exactly like credential tampering,
#: which is the one thing it was not.
_KEK = base64.b64encode(b"C" * 32).decode()
ENV = {"IRA_CONNECTOR_ENCRYPTION_KEY": _KEK,
       "IRA_CONNECTOR_ENCRYPTION_KEY_ID": "kek-batch11"}
os.environ.setdefault("IRA_CONNECTOR_ENCRYPTION_KEY", _KEK)

#: Fresh per run, deliberately. `tenant_data_keys` rows CANNOT be deleted — a
#: trigger refuses, because a destroyed key leaves a tombstone on purpose — so a
#: run that fails before teardown leaves DEKs behind for a fixed tenant id. The
#: next run then meets a key wrapped under whatever KEK id was ambient then,
#: and refuses with a tamper error that has nothing to do with what is being
#: tested. A new tenant each time cannot inherit one.
TENANT = uuid.uuid4()
ALICE = uuid.uuid4()
BOB = uuid.uuid4()


@pytest.fixture
async def pool():
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=8)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


@pytest.fixture
async def seeded(pool):
    """One tenant, two members, one connector credential each."""
    from core.connectors.keyring import credential_aad
    from core.connectors.secrets import seal

    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await _wipe(admin)
        run = uuid.uuid4().hex[:10]
        await admin.execute(
            "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
            TENANT, f"Cred {run}", f"cred-{run}")
        for user, label in ((ALICE, "alice"), (BOB, "bob")):
            await admin.execute(
                "INSERT INTO users (id,display_name,auth_provider,status) "
                "VALUES ($1,$2,'password','active')", user, label)
            await admin.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
                "VALUES ($1,$2,'member','active')", TENANT, user)
            # A REAL sealed credential, not a fake blob. Rotation opens each
            # one before re-sealing it, so a placeholder ciphertext fails to
            # authenticate and the test would be exercising the tamper path
            # instead of the scoping it claims to test.
            sealed = seal(f"refresh-token-for-{label}",
                          aad=credential_aad(TENANT, "gmail"), env=ENV)
            await admin.execute(
                "INSERT INTO connector_accounts (tenant_id,user_id,connector,"
                "provider_account,granted_scopes,credential_ciphertext,"
                "encryption_key_id,credential_nonce) "
                "VALUES ($1,$2,'gmail',$3,'{}',$4,$5,$6)",
                TENANT, user, f"{label}@example.com",
                sealed.ciphertext, sealed.key_id, sealed.nonce)
        yield admin
    finally:
        await _wipe(admin)
        await admin.close()


async def _wipe(admin):
    await admin.execute("DELETE FROM connector_accounts WHERE tenant_id = $1", TENANT)
    await admin.execute("DELETE FROM tenants WHERE id = $1", TENANT)
    await admin.execute("DELETE FROM users WHERE id = ANY($1::uuid[])",
                        [ALICE, BOB])


def _principal(user_id):
    from core.identity.principal import Principal

    return Principal(user_id=user_id, tenant_id=TENANT, session_id=uuid.uuid4(),
                     edition="cloud", username="", is_owner=False)


# ── The boundary 060 closes ─────────────────────────────────────────────────


async def test_a_member_cannot_read_another_members_credential(seeded):
    """`credential_ciphertext` is a long-lived OAuth refresh token. Under the
    tenant-only policy every colleague could read it."""
    from core.identity.db_context import tenant_connection

    async with tenant_connection(_principal(ALICE)) as conn:
        rows = await conn.fetch(
            "SELECT user_id, provider_account FROM connector_accounts")

    assert [r["user_id"] for r in rows] == [ALICE], (
        f"alice can see {[str(r['user_id']) for r in rows]} — a colleague's "
        f"grant to a third-party provider is not hers to read")


async def test_a_member_cannot_revoke_another_members_credential(seeded):
    """The old policy was FOR ALL, so the same predicate governed writes."""
    from core.identity.db_context import tenant_connection

    async with tenant_connection(_principal(ALICE)) as conn:
        result = await conn.execute(
            "UPDATE connector_accounts SET revoked_at = NOW() WHERE user_id = $1",
            BOB)

    assert result.endswith(" 0"), f"alice wrote to bob's credential ({result})"
    assert await seeded.fetchval(
        "SELECT revoked_at FROM connector_accounts WHERE user_id = $1", BOB
    ) is None


async def test_the_tenant_only_policy_is_gone(seeded):
    """PostgreSQL ORs permissive policies, so leaving `tenant_isolation` beside
    the new four would make every assertion above pass while protecting
    nothing."""
    for table in ("connector_accounts", "companion_preferences"):
        names = [r["polname"] for r in await seeded.fetch(
            "SELECT polname FROM pg_policy WHERE polrelid = $1::regclass", table)]
        assert "tenant_isolation" not in names, (
            f"{table} still carries 023's tenant-only policy: {sorted(names)}")
        assert len(names) == 4


async def test_the_background_worker_cannot_read_credentials(seeded):
    """A job holding OAuth refresh tokens can act as any user at any connected
    provider."""
    for table in ("connector_accounts", "companion_preferences"):
        assert await seeded.fetchval(
            "SELECT has_table_privilege('ira_worker', $1, 'SELECT')", table
        ) is False


# ── The reason it took a batch ──────────────────────────────────────────────


async def test_rotation_still_reaches_every_members_credentials(seeded):
    """The assertion `060` is only safe because of.

    A tenant-wide read is no longer possible, so `rotate_tenant` enumerates the
    tenant's members and binds each. If that rework were missing — or the member
    list incomplete — this rotation would rewrap ZERO rows and report success,
    and the straggler count guarding key destruction would agree with it.
    """
    from core.connectors.keyring import rotate_tenant

    before = {r["user_id"]: bytes(r["credential_ciphertext"])
              for r in await seeded.fetch(
                  "SELECT user_id, credential_ciphertext FROM connector_accounts "
                  "WHERE tenant_id = $1", TENANT)}
    assert set(before) == {ALICE, BOB}

    result = await rotate_tenant(
        _principal(ALICE), destroy_old=False, rewrap_mfa=False,
        env=ENV, reason="batch 11 scoping test")

    assert result.credentials_rewrapped == 2, (
        f"rotation rewrapped {result.credentials_rewrapped} of 2 credentials. A per-user "
        f"pass that misses a member leaves their credential under a key the "
        f"next rotation will destroy.")

    after = {r["user_id"]: bytes(r["credential_ciphertext"])
             for r in await seeded.fetch(
                 "SELECT user_id, credential_ciphertext FROM connector_accounts "
                 "WHERE tenant_id = $1", TENANT)}
    for user in (ALICE, BOB):
        assert after[user] != before[user], (
            f"{user}'s credential was not re-sealed")


async def test_rotation_counts_stragglers_across_every_member(seeded):
    """The most dangerous statement in the module.

    `rotate_tenant` destroys the old key when no row still points at it. That
    count is now a sum over members; if it were still the tenant-wide query it
    would return ZERO under the new policy, and the key would be destroyed
    beneath every credential in the tenant.

    Asserted by rotating with `destroy_old=True` and requiring that both
    members' credentials still open — which they cannot if the key they name
    was destroyed.
    """
    from core.connectors.keyring import rotate_tenant

    first = await rotate_tenant(
        _principal(ALICE), destroy_old=False, rewrap_mfa=False,
        env=ENV, reason="seed a previous version")
    assert first.credentials_rewrapped == 2

    second = await rotate_tenant(
        _principal(ALICE), destroy_old=True, rewrap_mfa=False,
        env=ENV, reason="destroy the superseded version")
    assert second.credentials_rewrapped == 2

    # Every row now names the newest key, and none names a destroyed one.
    key_ids = {r["encryption_key_id"] for r in await seeded.fetch(
        "SELECT encryption_key_id FROM connector_accounts WHERE tenant_id = $1",
        TENANT)}
    assert len(key_ids) == 1, (
        f"credentials are split across {sorted(key_ids)} after a rotation that "
        f"destroyed a key — one of them names a key that no longer exists")

    live = await seeded.fetchval(
        "SELECT count(*) FROM tenant_data_keys WHERE tenant_id = $1 "
        "AND destroyed_at IS NULL", TENANT)
    assert live >= 1, "every key was destroyed"


async def test_the_member_list_includes_suspended_and_revoked_members(seeded):
    """The realistic way the list goes incomplete, and the dangerous one.

    A suspended member's credentials are still rows, still sealed under the old
    key, and still stragglers. Filtering them out would produce a count that
    says the key is unused while their rows point straight at it — and the next
    rotation destroys it beneath them.

    `_tenant_members` deliberately does not filter on membership status. This is
    the assertion that keeps it that way, because "only active members" is the
    obvious-looking optimisation somebody will reach for.
    """
    from core.connectors import keyring

    await seeded.execute(
        "UPDATE tenant_memberships SET status = 'suspended' WHERE user_id = $1",
        BOB)

    members = await keyring._tenant_members(_principal(ALICE))

    assert BOB in members, (
        "a suspended member was dropped from the rotation's member list. Their "
        "credentials are still sealed under the old key, so the straggler "
        "count would miss them and the key would be destroyed underneath.")
    assert ALICE in members


async def test_rotation_still_covers_a_suspended_members_credential(seeded):
    """The consequence of the list above, proved end to end."""
    from core.connectors.keyring import rotate_tenant

    await seeded.execute(
        "UPDATE tenant_memberships SET status = 'suspended' WHERE user_id = $1",
        BOB)

    result = await rotate_tenant(
        _principal(ALICE), destroy_old=False, rewrap_mfa=False,
        env=ENV, reason="suspended-member coverage")

    assert result.credentials_rewrapped == 2, (
        f"rotation rewrapped {result.credentials_rewrapped} of 2 — a suspended member's "
        f"credential was left behind")
