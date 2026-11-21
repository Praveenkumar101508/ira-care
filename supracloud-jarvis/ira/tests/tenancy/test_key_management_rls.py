"""Blocker B3 — crypto-shredding, against a real PostgreSQL.

`RELEASE_READINESS.md` R11's third fault was *"no crypto-shredding"*. That claim
cannot be tested anywhere but here: "destroying this row makes the ciphertext
permanently unopenable" is a statement about a database, a trigger, a CHECK
constraint and AES all at once, and a mock only does what it was written to do.

Two halves:

**The schema half** (psycopg2, same shape as every other file in this
directory): RLS is enabled *and forced*, the shred-only trigger refuses each
transition it exists to refuse, and the partial unique index permits exactly one
active key. Each is proved by planting the violation, not by reading the DDL.

**The end-to-end half** (asyncpg, the production driver): `utils.db._pool` is
pointed at the test database so the *real* `tenant_connection` runs — real
`SET LOCAL app.tenant_id`, real transaction, real RLS — and the *real*
`core/connectors/keyring.py` mints, seals, rotates and destroys. Nothing about
the code under test is substituted. `test_the_harness_really_binds_the_tenant`
asserts that arrangement before any conclusion rests on it.

Running them: see the header of test_tenant_isolation_rls.py. The chain must
include 030_key_management.sql.
"""
from __future__ import annotations

import base64
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")
asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="IRA_TEST_DATABASE_DSN not set — real-database key tests skipped"
)

TOKEN = "1//0gA-REFRESH-TOKEN-THAT-MUST-DIE-WITH-ITS-KEY"

#: Every test gets FRESH tenant ids rather than two fixed ones, because
#: `tenant_data_keys` rows cannot be deleted — the trigger refuses DELETE, which
#: is one of the properties under test. Cleaning up would mean disabling that
#: trigger, which needs table ownership the application role does not have in a
#: CI-shaped database (and must not have). Fresh ids sidestep the problem
#: entirely and make every test independent of every other.

#: A KEK for the test process. Generated per run, so nothing here depends on a
#: fixed key and a leaked test fixture cannot open anything real.
KEK_ENV = {
    "IRA_CONNECTOR_ENCRYPTION_KEY": base64.b64encode(os.urandom(32)).decode(),
    "IRA_CONNECTOR_ENCRYPTION_KEY_ID": "kek-test",
}


# ═══════════════════════════════════════════════════════════════════════════
# The schema half — psycopg2, planted violations
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def db():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    yield conn
    conn.close()


def _as_tenant(conn, tenant: uuid.UUID):
    cur = conn.cursor()
    cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))
    return cur


def _clear_tenant(conn):
    cur = conn.cursor()
    cur.execute("SELECT set_config('app.tenant_id', '', false)")
    return cur


@pytest.fixture
def tenants(db):
    """Two fresh tenants with one live key row each.

    No teardown, deliberately. See the note beside TOKEN: these rows cannot be
    deleted without disabling the very trigger under test, so each run uses ids
    nothing else will ever use.
    """
    a, b = uuid.uuid4(), uuid.uuid4()
    run = uuid.uuid4().hex[:12]
    cur = _clear_tenant(db)
    cur.execute(
        "INSERT INTO tenants (id, name, slug, edition) VALUES "
        "(%s,%s,%s,'cloud'), (%s,%s,%s,'cloud')",
        (str(a), f"Key A {run}", f"key-a-{run}",
         str(b), f"Key B {run}", f"key-b-{run}"),
    )
    for tenant in (a, b):
        cur = _as_tenant(db, tenant)
        cur.execute(
            "INSERT INTO tenant_data_keys "
            "(tenant_id, key_version, wrapped_dek, wrap_nonce, kek_id) "
            "VALUES (%s, 1, %s, %s, 'kek-test')",
            (str(tenant), psycopg2.Binary(b"wrapped"), psycopg2.Binary(b"nonce")),
        )
    return a, b


def test_the_app_role_is_not_a_superuser(db):
    """Every isolation claim below is void otherwise: a superuser bypasses RLS
    entirely, so the whole file would pass while proving nothing."""
    cur = db.cursor()
    cur.execute("SELECT current_setting('is_superuser')")
    assert cur.fetchone()[0] == "off", (
        "IRA_TEST_DATABASE_DSN points at a superuser. RLS does not apply to "
        "superusers, so these tests would pass regardless of the policy."
    )


def test_the_key_table_enables_and_forces_row_level_security(db):
    """FORCE matters as much as ENABLE: without it the table's owner bypasses
    the policy, and in a single-role deployment the owner is the app."""
    cur = db.cursor()
    cur.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relname = 'tenant_data_keys'"
    )
    enabled, forced = cur.fetchone()
    assert enabled, "tenant_data_keys does not have RLS enabled"
    assert forced, "tenant_data_keys does not FORCE RLS — its owner bypasses it"


def test_one_tenant_cannot_see_anothers_key_rows(db, tenants):
    a, _b = tenants
    cur = _as_tenant(db, a)
    cur.execute("SELECT tenant_id FROM tenant_data_keys")
    seen = {uuid.UUID(str(row[0])) for row in cur.fetchall()}
    assert seen == {a}, f"tenant A saw key rows for {seen}"


def test_no_tenant_context_sees_no_key_rows(db, tenants):
    """Missing context denies by construction — `tenant_id = NULL` is NULL,
    not true."""
    cur = _clear_tenant(db)
    cur.execute("SELECT COUNT(*) FROM tenant_data_keys")
    assert cur.fetchone()[0] == 0


def test_a_second_active_key_is_refused(db, tenants):
    """Two active DEKs would make "which key do I seal under?" ambiguous, and
    ambiguity there strands rows at the next rotation."""
    a, _b = tenants
    cur = _as_tenant(db, a)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(
            "INSERT INTO tenant_data_keys "
            "(tenant_id, key_version, wrapped_dek, wrap_nonce, kek_id) "
            "VALUES (%s, 2, %s, %s, 'kek-test')",
            (str(a), psycopg2.Binary(b"second"), psycopg2.Binary(b"n")),
        )


def test_a_row_cannot_be_destroyed_and_still_hold_its_key(db, tenants):
    """The load-bearing CHECK. No ordering of statements and no partial failure
    can produce a row claiming to be shredded with the material still in it."""
    a, _b = tenants
    cur = _as_tenant(db, a)
    with pytest.raises(psycopg2.errors.CheckViolation) as exc:
        cur.execute(
            "UPDATE tenant_data_keys SET destroyed_at = NOW(), "
            "destruction_reason = 'account erasure' WHERE tenant_id = %s",
            (str(a),),
        )
    assert "destroyed_keys_hold_no_material" in str(exc.value)


def test_a_destruction_must_state_a_reason(db, tenants):
    a, _b = tenants
    cur = _as_tenant(db, a)
    with pytest.raises(psycopg2.errors.CheckViolation) as exc:
        cur.execute(
            "UPDATE tenant_data_keys SET destroyed_at = NOW(), wrapped_dek = NULL, "
            "wrap_nonce = NULL WHERE tenant_id = %s",
            (str(a),),
        )
    assert "destruction_states_a_reason" in str(exc.value)


def test_a_destroyed_key_cannot_be_resurrected(db, tenants):
    """The transition that would make crypto-shredding a claim rather than a
    property. Planted deliberately: destroy, then try to undo it."""
    a, _b = tenants
    cur = _as_tenant(db, a)
    cur.execute(
        "UPDATE tenant_data_keys SET destroyed_at = NOW(), "
        "destruction_reason = 'account erasure', wrapped_dek = NULL, "
        "wrap_nonce = NULL WHERE tenant_id = %s",
        (str(a),),
    )
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        cur.execute(
            "UPDATE tenant_data_keys SET destroyed_at = NULL, wrapped_dek = %s, "
            "wrap_nonce = %s WHERE tenant_id = %s",
            (psycopg2.Binary(b"back"), psycopg2.Binary(b"n"), str(a)),
        )
    assert "Destruction is final" in str(exc.value)


def test_a_destroyed_key_row_cannot_be_deleted(db, tenants):
    """The tombstone is what distinguishes "deliberately destroyed" from
    "unopenable and nobody knows why"."""
    a, _b = tenants
    cur = _as_tenant(db, a)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        cur.execute("DELETE FROM tenant_data_keys WHERE tenant_id = %s", (str(a),))
    assert "not deletable" in str(exc.value)


def test_live_key_material_cannot_be_swapped_in_place(db, tenants):
    """A rotation mints a new version. Overwriting an old one would make every
    credential already pointing at it unopenable, with no record of why."""
    a, _b = tenants
    cur = _as_tenant(db, a)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        cur.execute(
            "UPDATE tenant_data_keys SET wrapped_dek = %s WHERE tenant_id = %s",
            (psycopg2.Binary(b"different"), str(a)),
        )
    assert "may not be replaced in place" in str(exc.value)


def test_a_retired_key_cannot_be_made_active_again(db, tenants):
    a, _b = tenants
    cur = _as_tenant(db, a)
    cur.execute(
        "UPDATE tenant_data_keys SET retired_at = NOW() WHERE tenant_id = %s",
        (str(a),),
    )
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        cur.execute(
            "UPDATE tenant_data_keys SET retired_at = NULL WHERE tenant_id = %s",
            (str(a),),
        )
    assert "cannot be made active again" in str(exc.value)


def test_a_key_rows_identity_is_immutable(db, tenants):
    """Repointing a key at another version would silently make ciphertext open
    under the wrong key — the one failure mode where shredding appears to work
    and does not."""
    a, _b = tenants
    cur = _as_tenant(db, a)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        cur.execute(
            "UPDATE tenant_data_keys SET key_version = 99 WHERE tenant_id = %s",
            (str(a),),
        )
    assert "immutable" in str(exc.value)


def test_the_destruction_record_cannot_be_rewritten(db, tenants):
    a, _b = tenants
    cur = _as_tenant(db, a)
    cur.execute(
        "UPDATE tenant_data_keys SET destroyed_at = NOW(), "
        "destruction_reason = 'account erasure', wrapped_dek = NULL, "
        "wrap_nonce = NULL WHERE tenant_id = %s",
        (str(a),),
    )
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        cur.execute(
            "UPDATE tenant_data_keys SET destruction_reason = 'routine cleanup' "
            "WHERE tenant_id = %s",
            (str(a),),
        )
    assert "may not be rewritten" in str(exc.value)


# ═══════════════════════════════════════════════════════════════════════════
# The end-to-end half — asyncpg, the real keyring, the real tenant_connection
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
async def pool():
    """Point `utils.db` at the test database so the REAL `tenant_connection`
    runs. Nothing about the code under test is substituted — the binding of
    `app.tenant_id`, the transaction scoping and the RLS are all production."""
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


def _principal(tenant: uuid.UUID):
    from core.editions import EDITION_CLOUD
    from core.identity import Principal

    return Principal(
        user_id=uuid.uuid5(tenant, "user"),
        tenant_id=tenant,
        session_id=uuid.uuid4(),
        edition=EDITION_CLOUD,
    )


@pytest.fixture
async def accounts(pool):
    """Two fresh tenants, each with a user, ready for real keyring calls.

    Fresh rather than cleaned up, for the reason given at the top of the file:
    a key row cannot be deleted without disabling the trigger under test, and
    the application role neither owns the table nor should.
    """
    a, b = uuid.uuid4(), uuid.uuid4()
    run = uuid.uuid4().hex[:12]
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, slug, edition) VALUES "
            "($1,$2,$3,'cloud'), ($4,$5,$6,'cloud')",
            a, f"AKey A {run}", f"akey-a-{run}",
            b, f"AKey B {run}", f"akey-b-{run}",
        )
        for tenant, label in ((a, "a"), (b, "b")):
            await conn.execute(
                "INSERT INTO users (id, external_subject) VALUES ($1, $2)",
                uuid.uuid5(tenant, "user"), f"akey-{label}-{run}",
            )
    return _principal(a), _principal(b)


async def test_the_harness_really_binds_the_tenant(accounts):
    """Asserted before anything else rests on it.

    If `tenant_connection` were not setting `app.tenant_id`, every isolation
    result in this half would be meaningless — and would look like a pass.
    """
    from core.identity.db_context import tenant_connection

    principal, _other = accounts
    async with tenant_connection(principal) as conn:
        bound = await conn.fetchval("SELECT current_setting('app.tenant_id', true)")
    assert bound == str(principal.tenant_id), (
        f"the harness bound {bound!r}, not tenant A — the RLS assertions below "
        f"would prove nothing"
    )


async def test_a_first_seal_mints_a_key_and_the_credential_opens(accounts):
    """The ordinary path, end to end: no key exists, one is minted, the token
    round-trips through the real database."""
    from core.connectors import keyring

    principal, _other = accounts
    sealed = await keyring.seal_credential(
        principal, TOKEN, connector="gmail", env=KEK_ENV
    )

    assert sealed.key_id == "dek:1"
    assert TOKEN.encode() not in sealed.ciphertext

    opened = await keyring.use_credential(
        principal, sealed, lambda t: t, connector="gmail", env=KEK_ENV
    )
    assert opened == TOKEN


async def test_destroying_the_key_makes_the_credential_unopenable(accounts):
    """**The crypto-shredding claim, tested as a claim about ciphertext.**

    The credential bytes are captured before the destruction and are still held
    afterwards — as an attacker with a database copy would hold them — and they
    do not open. Not "the row is gone": the row is right here and useless.
    """
    from core.connectors import keyring

    principal, _other = accounts
    sealed = await keyring.seal_credential(
        principal, TOKEN, connector="gmail", env=KEK_ENV
    )
    assert await keyring.use_credential(
        principal, sealed, lambda t: t, connector="gmail", env=KEK_ENV
    ) == TOKEN

    result = await keyring.destroy_keys(principal, reason="account erasure test")
    assert result.versions_destroyed == (1,)

    with pytest.raises(keyring.KeyDestroyed) as exc:
        await keyring.use_credential(
            principal, sealed, lambda t: t, connector="gmail", env=KEK_ENV
        )
    assert "permanently unopenable" in str(exc.value)
    assert "account erasure test" in str(exc.value), (
        "the refusal does not carry the reason, so a user asking why their "
        "connector stopped working cannot be told"
    )


async def test_the_tombstone_survives_and_carries_the_reason(accounts):
    """A destroyed key leaves evidence. Without it, "your Gmail stopped
    working" has no answer."""
    from core.connectors import keyring

    principal, _other = accounts
    await keyring.seal_credential(principal, TOKEN, connector="gmail", env=KEK_ENV)
    await keyring.destroy_keys(principal, reason="account erasure test")

    records = await keyring.inventory(principal)
    assert len(records) == 1
    assert records[0].destroyed is True
    assert records[0].destruction_reason == "account erasure test"
    assert records[0].destroyed_at


async def test_a_key_record_can_never_carry_key_material(accounts):
    """A record type that *could* hold a key is one that eventually does, in a
    log line somebody added in a hurry. Checked structurally."""
    import dataclasses

    from core.connectors import keyring

    principal, _other = accounts
    await keyring.seal_credential(principal, TOKEN, connector="gmail", env=KEK_ENV)
    record = (await keyring.inventory(principal))[0]

    for f in dataclasses.fields(record):
        assert not isinstance(getattr(record, f.name), (bytes, bytearray)), (
            f"KeyRecord.{f.name} holds bytes. This type is returned to API "
            f"surfaces and written to logs; it must never carry material."
        )


async def test_destroying_needs_a_real_reason(accounts):
    from core.connectors import keyring

    with pytest.raises(keyring.KeyringError, match="at least 8 characters"):
        await keyring.destroy_keys(accounts[0], reason="x")


async def test_destroying_twice_reports_the_second_as_already_done(accounts):
    """Idempotent, and honest about which it was — an erasure retried must not
    claim to have destroyed something twice."""
    from core.connectors import keyring

    principal, _other = accounts
    await keyring.seal_credential(principal, TOKEN, connector="gmail", env=KEK_ENV)

    first = await keyring.destroy_keys(principal, reason="account erasure test")
    second = await keyring.destroy_keys(principal, reason="account erasure test")

    assert first.versions_destroyed == (1,)
    assert second.versions_destroyed == ()
    assert second.already_destroyed == (1,)
    assert second.anything_destroyed is False


async def test_one_tenants_destruction_does_not_touch_another(accounts):
    """Shredding is per-tenant. A deletion that took out a neighbour's keys
    would be the worst possible bug in this feature."""
    from core.connectors import keyring

    a, b = accounts
    sealed_a = await keyring.seal_credential(a, TOKEN, connector="gmail", env=KEK_ENV)
    sealed_b = await keyring.seal_credential(b, TOKEN, connector="gmail", env=KEK_ENV)

    await keyring.destroy_keys(a, reason="account erasure test")

    with pytest.raises(keyring.KeyDestroyed):
        await keyring.use_credential(a, sealed_a, lambda t: t, connector="gmail", env=KEK_ENV)

    assert await keyring.use_credential(
        b, sealed_b, lambda t: t, connector="gmail", env=KEK_ENV
    ) == TOKEN


async def test_a_credential_sealed_by_one_tenant_cannot_be_opened_by_another(accounts):
    """Two defences, and this exercises both: the AAD binds the ciphertext to
    the tenant, and RLS keeps B's lookup away from A's key row."""
    from core.connectors import keyring

    a, b = accounts
    sealed_a = await keyring.seal_credential(a, TOKEN, connector="gmail", env=KEK_ENV)
    await keyring.seal_credential(b, "other", connector="gmail", env=KEK_ENV)

    from core.connectors.secrets import CredentialTampered

    with pytest.raises((CredentialTampered, keyring.KeyNotFound)):
        await keyring.use_credential(
            b, sealed_a, lambda t: t, connector="gmail", env=KEK_ENV
        )


async def test_a_missing_key_version_refuses_rather_than_minting_one(accounts):
    """A partial restore brings back credential rows without key rows. Minting
    a replacement would produce a key that opens nothing while looking healthy."""
    from core.connectors import keyring
    from core.connectors.secrets import SealedSecret

    principal, _other = accounts
    orphan = SealedSecret(ciphertext=b"whatever", key_id="dek:7", nonce=b"0" * 12)

    with pytest.raises(keyring.KeyNotFound, match="partial restore"):
        await keyring.use_credential(
            principal, orphan, lambda t: t, connector="gmail", env=KEK_ENV
        )


async def test_a_legacy_phase_7_credential_still_opens(accounts):
    """The upgrade must not break existing deployments. A credential sealed
    under the environment key before B3 opens through the same call as a
    DEK-sealed one, with no branch at the call site."""
    from core.connectors import keyring
    from core.connectors.secrets import seal

    principal, _other = accounts
    legacy = seal(TOKEN, aad=keyring.credential_aad(principal.tenant_id, "gmail"), env=KEK_ENV)

    opened = await keyring.use_credential(
        principal, legacy, lambda t: t, connector="gmail", env=KEK_ENV
    )
    assert opened == TOKEN


# ── Rotation ─────────────────────────────────────────────────────────────────


async def _store_credential(principal, sealed, connector="gmail"):
    """Put a sealed credential in `connector_accounts`, as `upsert_account`
    would. Written here rather than calling the store so a rotation test does
    not depend on the store's own behaviour."""
    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal) as conn:
        return await conn.fetchval(
            "INSERT INTO connector_accounts "
            "(tenant_id, user_id, connector, provider_account, granted_scopes, "
            " access_level, status, credential_ciphertext, encryption_key_id, "
            " credential_nonce) "
            "VALUES ($1,$2,$3,$4,$5,'observe','connected',$6,$7,$8) RETURNING id",
            principal.tenant_id, principal.user_id, connector,
            f"{connector}@example.test", ["https://www.googleapis.com/auth/gmail.readonly"],
            sealed.ciphertext, sealed.key_id, sealed.nonce,
        )


async def test_rotation_re_encrypts_and_destroys_the_old_key(accounts):
    """**The claim R11 said was unmet:** rotating must actually re-encrypt.

    Three assertions, and the third is the one a fake would fail: the stored
    ciphertext CHANGED. A `rotate` that merely bumped a version number would
    pass "the credential still opens" perfectly.
    """
    from core.connectors import keyring
    from core.identity.db_context import tenant_connection

    principal, _other = accounts
    sealed = await keyring.seal_credential(
        principal, TOKEN, connector="gmail", env=KEK_ENV
    )
    account_id = await _store_credential(principal, sealed)
    before = sealed.ciphertext

    result = await keyring.rotate_tenant(
        principal, reason="rotation under test", env=KEK_ENV
    )

    assert result.from_version == 1
    assert result.to_version == 2
    assert result.credentials_rewrapped == 1
    assert result.old_key_destroyed is True

    async with tenant_connection(principal) as conn:
        row = await conn.fetchrow(
            "SELECT credential_ciphertext, encryption_key_id, credential_nonce "
            "FROM connector_accounts WHERE id = $1", account_id,
        )

    assert row["encryption_key_id"] == "dek:2"
    assert bytes(row["credential_ciphertext"]) != before, (
        "the ciphertext is unchanged after a rotation — nothing was re-encrypted"
    )

    from core.connectors.secrets import SealedSecret

    rotated = SealedSecret(
        ciphertext=bytes(row["credential_ciphertext"]),
        key_id=row["encryption_key_id"],
        nonce=bytes(row["credential_nonce"]),
    )
    assert await keyring.use_credential(
        principal, rotated, lambda t: t, connector="gmail", env=KEK_ENV
    ) == TOKEN


async def test_after_a_rotation_the_old_ciphertext_is_dead(accounts):
    """A copy of the pre-rotation ciphertext — the thing an attacker with an
    old backup holds — must not open once the old key is destroyed."""
    from core.connectors import keyring

    principal, _other = accounts
    sealed = await keyring.seal_credential(
        principal, TOKEN, connector="gmail", env=KEK_ENV
    )
    await _store_credential(principal, sealed)

    await keyring.rotate_tenant(principal, reason="rotation under test", env=KEK_ENV)

    with pytest.raises(keyring.KeyDestroyed):
        await keyring.use_credential(
            principal, sealed, lambda t: t, connector="gmail", env=KEK_ENV
        )


async def test_rotation_migrates_a_phase_7_credential_onto_a_dek(accounts):
    """The migration path for an existing deployment: rows sealed under the
    environment key move onto a tenant key by rotating."""
    from core.connectors import keyring
    from core.connectors.secrets import SealedSecret, seal
    from core.identity.db_context import tenant_connection

    principal, _other = accounts
    legacy = seal(TOKEN, aad=keyring.credential_aad(principal.tenant_id, "gmail"), env=KEK_ENV)
    account_id = await _store_credential(principal, legacy)

    result = await keyring.rotate_tenant(
        principal, reason="migrating off the environment key", env=KEK_ENV
    )
    assert result.from_version is None, "this tenant had no previous key"
    assert result.credentials_rewrapped == 1

    async with tenant_connection(principal) as conn:
        row = await conn.fetchrow(
            "SELECT credential_ciphertext, encryption_key_id, credential_nonce "
            "FROM connector_accounts WHERE id = $1", account_id,
        )
    assert row["encryption_key_id"] == "dek:1"

    moved = SealedSecret(
        ciphertext=bytes(row["credential_ciphertext"]),
        key_id=row["encryption_key_id"],
        nonce=bytes(row["credential_nonce"]),
    )
    assert await keyring.use_credential(
        principal, moved, lambda t: t, connector="gmail", env=KEK_ENV
    ) == TOKEN


async def test_rotation_keeps_the_old_key_when_asked_not_to_destroy_it(accounts):
    """`destroy_old=False` is the reversible setting for introducing a rotation
    somewhere it has never run."""
    from core.connectors import keyring

    principal, _other = accounts
    sealed = await keyring.seal_credential(
        principal, TOKEN, connector="gmail", env=KEK_ENV
    )
    await _store_credential(principal, sealed)

    result = await keyring.rotate_tenant(
        principal, destroy_old=False, reason="reversible rotation", env=KEK_ENV
    )
    assert result.old_key_destroyed is False

    # The old ciphertext still opens, which is the whole point of not
    # destroying: the step can be undone.
    assert await keyring.use_credential(
        principal, sealed, lambda t: t, connector="gmail", env=KEK_ENV
    ) == TOKEN


async def test_rotation_rewraps_every_connector_not_just_the_first(accounts):
    """Three accounts, one key. A loop that stopped after one would leave two
    credentials pointing at a key the rotation then destroys."""
    from core.connectors import keyring
    from core.identity.db_context import tenant_connection

    principal, _other = accounts
    for connector in ("gmail", "google_calendar", "google_drive"):
        sealed = await keyring.seal_credential(
            principal, f"{TOKEN}-{connector}", connector=connector, env=KEK_ENV
        )
        await _store_credential(principal, sealed, connector=connector)

    result = await keyring.rotate_tenant(
        principal, reason="rotation under test", env=KEK_ENV
    )
    assert result.credentials_rewrapped == 3

    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT connector, encryption_key_id FROM connector_accounts "
            "WHERE tenant_id = $1", principal.tenant_id,
        )
    assert {r["encryption_key_id"] for r in rows} == {"dek:2"}, (
        f"not every credential moved: {[dict(r) for r in rows]}"
    )


async def test_a_rotation_does_not_touch_another_tenant(accounts):
    from core.connectors import keyring

    a, b = accounts
    sealed_b = await keyring.seal_credential(b, TOKEN, connector="gmail", env=KEK_ENV)
    await _store_credential(b, sealed_b)
    await keyring.seal_credential(a, TOKEN, connector="gmail", env=KEK_ENV)

    await keyring.rotate_tenant(a, reason="rotation under test", env=KEK_ENV)

    assert await keyring.use_credential(
        b, sealed_b, lambda t: t, connector="gmail", env=KEK_ENV
    ) == TOKEN


async def test_versions_are_never_reused_after_destruction(accounts):
    """Reusing a destroyed version number would let a credential still pointing
    at it open under a different key — shredding that appears to work and does
    not."""
    from core.connectors import keyring

    principal, _other = accounts
    await keyring.seal_credential(principal, TOKEN, connector="gmail", env=KEK_ENV)
    await keyring.destroy_keys(principal, reason="account erasure test")

    fresh = await keyring.seal_credential(
        principal, TOKEN, connector="gmail", env=KEK_ENV
    )
    assert fresh.key_id == "dek:2", (
        "a destroyed version number was reused; ciphertext referring to it "
        "would open under the wrong key"
    )


# ── Fail closed on the KEK itself ────────────────────────────────────────────


async def test_no_kek_configured_refuses_to_mint(accounts):
    """"Missing encryption key" is on the brief's fail-closed list, and it must
    stay fail-closed for the outer envelope too."""
    from core.connectors import keyring
    from core.connectors.secrets import MissingEncryptionKey

    with pytest.raises(MissingEncryptionKey):
        await keyring.seal_credential(
            accounts[0], TOKEN, connector="gmail", env={}
        )


async def test_a_rotated_kek_cannot_open_the_old_dek(accounts):
    """Rotating the KEK without re-wrapping the DEKs is a real operational
    mistake. It must present as a clear KEK mismatch, not as "your database was
    tampered with"."""
    from core.connectors import keyring
    from core.connectors.secrets import CredentialTampered

    principal, _other = accounts
    sealed = await keyring.seal_credential(
        principal, TOKEN, connector="gmail", env=KEK_ENV
    )

    other_kek = {
        "IRA_CONNECTOR_ENCRYPTION_KEY": base64.b64encode(os.urandom(32)).decode(),
        "IRA_CONNECTOR_ENCRYPTION_KEY_ID": "kek-rotated",
    }
    with pytest.raises(CredentialTampered, match="configured KEK"):
        await keyring.use_credential(
            principal, sealed, lambda t: t, connector="gmail", env=other_kek
        )
