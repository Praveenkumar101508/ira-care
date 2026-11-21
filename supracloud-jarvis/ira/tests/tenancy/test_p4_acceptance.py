"""Phase 4 batch 15 — end-to-end acceptance of the combined system.

Every earlier batch tested one mechanism against the schema it owns. This one
runs the mechanisms *together*, against a database built by the whole migration
chain, and asks the questions an operator would ask on the day something goes
wrong.

── The one that mattered ───────────────────────────────────────────────────

P4-13 recorded the rotation-interruption claim as **UNPROVEN**: the injection
point it chose — a failure inside `rewrap` — was never reached, because the
fixture's tenant had no credentials and so there was nothing to re-wrap. The
test reported DID NOT RAISE and proved nothing.

The instruction for this batch was to establish a reachable interruption point
or design a legitimate one, and not to make a test green by mocking a path
production cannot reach. What was missing turned out to be small: a tenant with
*two* credentials, one of which does not decrypt. No mocking is involved at all
— a stored ciphertext that no longer verifies is a state a real deployment
reaches through bit rot, a partial restore, or a connector renamed so its AAD
no longer matches. The rotation then fails in the middle of its rewrap loop,
which is exactly where `rotate_tenant`'s docstring makes its load-bearing claim.

Reaching it found a defect that had been sitting under the UNPROVEN note:
`rotate_tenant` minted the new key inside the same transaction as the rewrap
loop, while each credential was written on its own connection and committed
immediately. An interruption therefore rolled the new key row back and left
committed credentials pointing at a key version that had never existed —
`KeyNotFound`, permanently, with no plaintext to re-seal. The function's own
docstring said the opposite: "both keys still open — so the deployment is fully
working, just half-rotated".

`test_an_interrupted_rotation_leaves_every_credential_openable` is that
reproduction, and it fails against the pre-fix ordering.
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
os.environ.setdefault("IRA_CONNECTOR_ENCRYPTION_KEY_ID", "kek-p415")
for _k, _v in {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-test",
    "REDIS_PASSWORD": "ci-test",
    "VLLM_API_KEY": "ci-test",
}.items():
    os.environ.setdefault(_k, _v)

# ── Why every test gets a NEW tenant id ─────────────────────────────────────
#
# `tenant_data_keys` has a shred-only trigger and no cascade from `tenants`, so
# key rows deliberately outlive the tenant: destroying key material must be an
# explicit act, not a side effect of deleting a row. That is correct, and it
# means a module-level tenant id accumulates key versions across tests.
#
# It is not a tidiness point. With a shared id,
# `test_an_interrupted_rotation_leaves_every_credential_openable` — the
# regression test for the defect this batch found — PASSED against the buggy
# ordering when the whole file ran, because a key version left behind by an
# earlier test happened to occupy the number the rolled-back mint had chosen.
# It failed in isolation. A regression test that passes on the broken code in
# the ordinary run is not a regression test, so the tenant is fresh each time.
class _Ids:
    def __init__(self):
        self.tenant_a = uuid.uuid4()
        self.tenant_b = uuid.uuid4()
        self.user_a = uuid.uuid4()
        self.user_b = uuid.uuid4()
        self.workspace_a = uuid.uuid4()
        self.workspace_b = uuid.uuid4()


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


async def _seed_tenant(admin, tenant, user, workspace, slug):
    await admin.execute(
        "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
        tenant, slug.upper(), slug)
    await admin.execute(
        "INSERT INTO users (id,display_name,auth_provider,status) "
        "VALUES ($1,$2,'password','active')", user, slug)
    await admin.execute(
        "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
        "VALUES ($1,$2,'member','active')", tenant, user)
    org = await admin.fetchval(
        "INSERT INTO organisations (tenant_id,name,slug) VALUES ($1,'O',$2) "
        "RETURNING id", tenant, slug)
    await admin.execute(
        "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,"
        " is_default) VALUES ($1,$2,$3,'W',$4,TRUE)",
        workspace, tenant, org, slug)
    await admin.execute(
        "INSERT INTO workspace_memberships (tenant_id,workspace_id,user_id,role)"
        " VALUES ($1,$2,$3,'member')", tenant, workspace, user)


@pytest.fixture
async def ids():
    return _Ids()


@pytest.fixture
async def seeded(pool, ids):
    admin = await asyncpg.connect(ADMIN_DSN)
    slug = uuid.uuid4().hex[:10]
    try:
        await _seed_tenant(admin, ids.tenant_a, ids.user_a, ids.workspace_a,
                           f"a{slug}")
        await _seed_tenant(admin, ids.tenant_b, ids.user_b, ids.workspace_b,
                           f"b{slug}")
        yield admin
    finally:
        for t in (ids.tenant_a, ids.tenant_b):
            await admin.execute("DELETE FROM tenants WHERE id = $1", t)
        for u in (ids.user_a, ids.user_b):
            await admin.execute("DELETE FROM users WHERE id = $1", u)
        await admin.close()


def _principal(tenant, user, workspace):
    from core.identity.principal import Principal

    return Principal(user_id=user, tenant_id=tenant, workspace_id=workspace,
                     session_id=uuid.uuid4(), edition="cloud",
                     username="p415", is_owner=False)


def _tenant_a(ids):
    return _principal(ids.tenant_a, ids.user_a, ids.workspace_a)


def _tenant_b(ids):
    return _principal(ids.tenant_b, ids.user_b, ids.workspace_b)


async def _store_credential(principal, connector, plaintext=None, *, corrupt=False):
    """One connector account, sealed under the tenant's active DEK.

    `corrupt=True` writes a ciphertext that will not verify. That is not a mock:
    it is the row a partial restore leaves behind, and `rewrap` refuses it
    through the same code path a real one would take.
    """
    from core.connectors import keyring
    from core.identity.db_context import tenant_connection

    if corrupt:
        # Still has to satisfy `credential_is_not_obvious_plaintext`, which is
        # the point — a corrupt credential is indistinguishable from a good one
        # until something tries to open it.
        key_id = (await keyring.active_dek(principal))[1]
        ciphertext = _stdlib_secrets.token_bytes(64)
        nonce = _stdlib_secrets.token_bytes(12)
    else:
        sealed = await keyring.seal_credential(
            principal, plaintext, connector=connector)
        ciphertext, key_id, nonce = sealed.ciphertext, sealed.key_id, sealed.nonce

    async with tenant_connection(principal) as conn:
        return await conn.fetchval(
            "INSERT INTO connector_accounts (tenant_id,user_id,connector,"
            " provider_account,credential_ciphertext,encryption_key_id,"
            " credential_nonce) VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id",
            principal.tenant_id, principal.user_id, connector,
            f"{connector}@example.test", ciphertext, key_id, nonce)


async def _active_version(admin, tenant):
    return await admin.fetchval(
        "SELECT key_version FROM tenant_data_keys WHERE tenant_id = $1 "
        "  AND retired_at IS NULL AND destroyed_at IS NULL", tenant)


async def _open(principal, account_id, connector, admin):
    """Decrypt one stored credential through the ordinary read path."""
    from core.connectors import keyring
    from core.connectors.secrets import SealedSecret

    row = await admin.fetchrow(
        "SELECT credential_ciphertext, encryption_key_id, credential_nonce "
        "FROM connector_accounts WHERE id = $1", account_id)
    sealed = SealedSecret(
        ciphertext=bytes(row["credential_ciphertext"]),
        key_id=row["encryption_key_id"],
        nonce=bytes(row["credential_nonce"]))
    return await keyring.use_credential(
        principal, sealed, lambda s: s, connector=connector)


# ─────────────────────────────────────────────────────────────────────────────
# Flow: rotation interrupted, and recovered
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_interruption_point_is_actually_reached(seeded, ids):
    """**The check P4-13 was missing.**

    Before asserting anything about what an interruption leaves behind, prove
    the interruption happens at all. P4-13's version asserted the aftermath of
    an event that never occurred, and passed a `pytest.raises` that reported
    DID NOT RAISE only because it was looking for the wrong thing.

    So: rotation must raise, and it must raise from inside the rewrap loop —
    evidenced by one credential having moved to the new key version before the
    failure. A rotation that failed before writing anything would leave a clean
    database and every assertion below would hold vacuously.
    """
    from core.connectors import keyring
    from core.connectors.secrets import CredentialTampered

    p = _tenant_a(ids)
    good = await _store_credential(p, "gmail", "token-good")
    await _store_credential(p, "google_calendar", corrupt=True)
    # Version numbers are per tenant and never reset: `tenant_data_keys` has a
    # shred-only trigger and no cascade from `tenants`, so a key row outlives
    # the tenant on purpose. Asserting `dek:1` would pass on the first run of
    # the file and fail on the second — which it did.
    before = await _active_version(seeded, ids.tenant_a)

    with pytest.raises(CredentialTampered):
        await keyring.rotate_tenant(p, destroy_old=True)

    moved = await seeded.fetchval(
        "SELECT encryption_key_id FROM connector_accounts WHERE id = $1", good)
    assert moved == f"dek:{before + 1}", (
        f"the good credential is on {moved!r} rather than the new key "
        f"dek:{before + 1}, so the rotation failed BEFORE it wrote anything "
        f"and this test is checking the aftermath of an interruption that did "
        f"not happen — exactly the vacuity P4-13 hit. If the read order "
        f"changed, seed the corrupt credential later.")


async def test_an_interrupted_rotation_leaves_every_credential_openable(seeded, ids):
    """**The defect this batch found, as a regression test.**

    `rotate_tenant` minted the new key inside the same transaction as the rewrap
    loop. Each credential is written on its own connection — it must be, because
    `connector_accounts` is user-scoped — and commits immediately. So an
    interruption rolled back the key row and left a committed credential naming
    a key version that had never existed.

    Against the pre-fix ordering this fails with `KeyNotFound`, and there is no
    recovery: the ciphertext is the only copy and its key was never persisted.
    """
    from core.connectors import keyring
    from core.connectors.secrets import CredentialTampered

    p = _tenant_a(ids)
    good = await _store_credential(p, "gmail", "token-good")
    await _store_credential(p, "google_calendar", corrupt=True)

    with pytest.raises(CredentialTampered):
        await keyring.rotate_tenant(p, destroy_old=True)

    assert await _open(p, good, "gmail", seeded) == "token-good", (
        "a credential the rotation successfully re-sealed can no longer be "
        "opened — the key it now names does not exist")


async def test_the_interrupted_state_is_the_one_the_docstring_promises(seeded, ids):
    """"Some rows point at the new version and some at the old, and both keys
    still open — so the deployment is fully working, just half-rotated."

    Checked as three separate facts, because the sentence is three claims and
    only one of them is about the rows."""
    from core.connectors import keyring
    from core.connectors.secrets import CredentialTampered

    p = _tenant_a(ids)
    await _store_credential(p, "gmail", "token-good")
    await _store_credential(p, "google_calendar", corrupt=True)
    old_v = await _active_version(seeded, ids.tenant_a)
    new_v = old_v + 1

    with pytest.raises(CredentialTampered):
        await keyring.rotate_tenant(p, destroy_old=True)

    versions = sorted(r["encryption_key_id"] for r in await seeded.fetch(
        "SELECT encryption_key_id FROM connector_accounts WHERE tenant_id = $1",
        ids.tenant_a))
    assert versions == [f"dek:{old_v}", f"dek:{new_v}"], (
        f"rows are on {versions}; a half-rotation should leave a mixture of "
        f"dek:{old_v} and dek:{new_v}")

    keys = {r["key_version"]: r for r in await seeded.fetch(
        "SELECT key_version, retired_at, destroyed_at FROM tenant_data_keys "
        "WHERE tenant_id = $1 AND key_version = ANY($2)",
        ids.tenant_a, [old_v, new_v])}
    assert set(keys) == {old_v, new_v}, (
        f"expected key versions {old_v} and {new_v} to exist, got {set(keys)}")
    assert keys[old_v]["retired_at"] is not None, "the old key was never retired"

    # The load-bearing half. A destroyed old key would make every row still on
    # it permanently unopenable, and this is the interruption most likely to
    # tempt an operator into destroying it.
    assert keys[old_v]["destroyed_at"] is None, (
        "the old key was destroyed by a rotation that did not finish — every "
        "credential still on it is now unrecoverable")
    assert keys[new_v]["destroyed_at"] is None, "the new key was destroyed"


async def test_the_interrupted_rotation_did_not_report_success(seeded, ids):
    """An operation that returns a RotationResult after failing halfway is
    worse than one that crashes: the crash gets investigated."""
    from core.connectors import keyring
    from core.connectors.secrets import CredentialTampered

    p = _tenant_a(ids)
    await _store_credential(p, "gmail", "token-good")
    await _store_credential(p, "google_calendar", corrupt=True)

    result = None
    try:
        result = await keyring.rotate_tenant(p, destroy_old=True)
    except CredentialTampered:
        pass
    assert result is None, (
        f"rotation returned {result!r} despite failing to re-seal a credential")


async def test_rerunning_the_rotation_finishes_what_the_first_one_started(seeded, ids):
    """"Running the rotation again finishes it." — the recovery half of the
    claim, and the reason the interrupted state is allowed to exist.

    The corrupt row is removed first, because that is what an operator does:
    the rotation told them precisely which credential would not open, and a
    credential that cannot be decrypted has to be reconnected rather than
    re-sealed. What must then be true is that the rotation picks up from where
    it stopped rather than needing the tenant rebuilt.
    """
    from core.connectors import keyring
    from core.connectors.secrets import CredentialTampered

    p = _tenant_a(ids)
    good = await _store_credential(p, "gmail", "token-good")
    bad = await _store_credential(p, "google_calendar", corrupt=True)

    with pytest.raises(CredentialTampered):
        await keyring.rotate_tenant(p, destroy_old=True)

    await seeded.execute("DELETE FROM connector_accounts WHERE id = $1", bad)

    result = await keyring.rotate_tenant(p, destroy_old=True)
    assert result is not None

    assert await _open(p, good, "gmail", seeded) == "token-good", (
        "the surviving credential stopped opening after the second rotation")

    live = await seeded.fetch(
        "SELECT key_version FROM tenant_data_keys WHERE tenant_id = $1 "
        "  AND retired_at IS NULL AND destroyed_at IS NULL", ids.tenant_a)
    assert len(live) == 1, (
        f"after a completed rotation the tenant has {len(live)} active keys, "
        f"not 1")

    # And nothing is left naming a key that is gone.
    rows = await seeded.fetch(
        "SELECT ca.encryption_key_id FROM connector_accounts ca "
        " WHERE ca.tenant_id = $1 AND ca.credential_ciphertext IS NOT NULL",
        ids.tenant_a)
    existing = {f"dek:{r['key_version']}" for r in await seeded.fetch(
        "SELECT key_version FROM tenant_data_keys WHERE tenant_id = $1 "
        "  AND destroyed_at IS NULL", ids.tenant_a)}
    for r in rows:
        assert r["encryption_key_id"] in existing, (
            f"a credential names {r['encryption_key_id']}, which is not a key "
            f"this tenant still has")


async def test_an_interruption_in_one_tenant_does_not_touch_its_neighbour(seeded, ids):
    """The failure must not cross a tenant boundary — not the credentials, and
    not the keys."""
    from core.connectors import keyring
    from core.connectors.secrets import CredentialTampered

    a, b = _tenant_a(ids), _tenant_b(ids)
    await _store_credential(a, "gmail", "token-a")
    await _store_credential(a, "google_calendar", corrupt=True)
    b_account = await _store_credential(b, "gmail", "token-b")

    b_key_before = await seeded.fetchval(
        "SELECT key_version FROM tenant_data_keys WHERE tenant_id = $1 "
        "  AND retired_at IS NULL AND destroyed_at IS NULL", ids.tenant_b)

    with pytest.raises(CredentialTampered):
        await keyring.rotate_tenant(a, destroy_old=True)

    assert await _open(b, b_account, "gmail", seeded) == "token-b", (
        "tenant B's credential stopped opening because tenant A's rotation "
        "failed")
    b_key_after = await seeded.fetchval(
        "SELECT key_version FROM tenant_data_keys WHERE tenant_id = $1 "
        "  AND retired_at IS NULL AND destroyed_at IS NULL", ids.tenant_b)
    assert b_key_after == b_key_before, (
        f"tenant B's active key moved from {b_key_before} to {b_key_after} "
        f"during a rotation of tenant A")


async def test_a_completed_rotation_still_destroys_the_old_key(seeded, ids):
    """The fix moved the mint out of the loop's transaction. It must not have
    also broken the ordinary case — a rotation with nothing wrong must still
    reach step 5 and destroy the key it replaced."""
    from core.connectors import keyring

    p = _tenant_a(ids)
    account = await _store_credential(p, "gmail", "token-good")

    result = await keyring.rotate_tenant(p, destroy_old=True)
    assert result.credentials_rewrapped == 1, (
        f"the rotation re-sealed {result.credentials_rewrapped} credentials, "
        f"not 1")
    assert result.old_key_destroyed is True, (
        f"the old key survived a clean rotation: {result.note!r}")

    destroyed = await seeded.fetchval(
        "SELECT destroyed_at FROM tenant_data_keys "
        " WHERE tenant_id = $1 AND key_version = $2",
        ids.tenant_a, result.from_version)
    assert destroyed is not None, (
        f"key version {result.from_version} was not destroyed")
    assert await _open(p, account, "gmail", seeded) == "token-good", (
        "the credential does not open under the new key")


# ─────────────────────────────────────────────────────────────────────────────
# Flow: one rotation, three kinds of protected material, and a restart
# ─────────────────────────────────────────────────────────────────────────────


async def _enrol_totp(principal, seed="JBSWY3DPEHPK3PXP"):
    from security.mfa_crypto import seal_totp_secret
    from core.identity.db_context import tenant_connection

    ciphertext, nonce, version = await seal_totp_secret(principal, seed)
    async with tenant_connection(principal) as conn:
        await conn.execute(
            "INSERT INTO totp_secrets (tenant_id,user_id,username,"
            " secret_ciphertext,secret_nonce,encryption_key_version,"
            " migration_status,encrypted_at,enabled) "
            "VALUES ($1,$2,$3,$4,$5,$6,'ENCRYPTED',NOW(),TRUE)",
            principal.tenant_id, principal.user_id, str(principal.user_id),
            ciphertext, nonce, version)
    return ciphertext, nonce, version


async def _read_totp(admin, principal):
    return await admin.fetchrow(
        "SELECT secret_ciphertext, secret_nonce, encryption_key_version "
        "FROM totp_secrets WHERE tenant_id = $1 AND user_id = $2 "
        "  AND revoked_at IS NULL", principal.tenant_id, principal.user_id)


async def _enrol_voice(admin, principal, template="voice-template-0123456789"):
    from security.voice_crypto import seal_template

    profile_id = await admin.fetchval(
        "INSERT INTO voice_profiles (owner_name,embedding,user_id,"
        " enrolled_in_tenant_id) VALUES ('v','',$1,$2) RETURNING id",
        principal.user_id, principal.tenant_id)
    sealed = seal_template(profile_id, 1, template)
    await admin.execute(
        "UPDATE voice_profiles SET template_ciphertext=$2, template_nonce=$3,"
        " wrapped_dek=$4, wrap_nonce=$5, kek_id=$6, encryption_key_version=$7,"
        " encrypted_at=NOW(), migration_status='ENCRYPTED', embedding='' "
        "WHERE id=$1",
        profile_id, sealed["template_ciphertext"], sealed["template_nonce"],
        sealed["wrapped_dek"], sealed["wrap_nonce"], sealed["kek_id"],
        sealed["encryption_key_version"])
    return profile_id


async def test_one_rotation_leaves_credential_totp_and_voice_all_openable(
        seeded, ids):
    """**The combined-system question.** Three families of protected material
    live under three different arrangements — a credential under the tenant DEK,
    a TOTP seed under a per-user MFA DEK, a voice template under a per-record
    DEK — and one operator action touches all three.

    Every earlier batch proved its own family in isolation. What no isolated
    suite can catch is a rotation that reaches two of the three, because the
    tenant it ran against only had two.
    """
    from core.connectors import keyring
    from security.mfa_crypto import use_totp_secret
    from security.voice_crypto import use_template

    p = _tenant_a(ids)
    account = await _store_credential(p, "gmail", "token-good")
    await _enrol_totp(p, "JBSWY3DPEHPK3PXP")
    profile_id = await _enrol_voice(seeded, p, "template-alpha")

    result = await keyring.rotate_tenant(p, destroy_old=True)
    assert result.old_key_destroyed is True, result.note
    assert not result.mfa_keys_failed, result.mfa_keys_failed
    assert not result.voice_keys_failed, result.voice_keys_failed

    assert await _open(p, account, "gmail", seeded) == "token-good"

    row = await _read_totp(seeded, p)
    seed = await use_totp_secret(
        p, row["secret_ciphertext"], row["secret_nonce"],
        row["encryption_key_version"], lambda s: s)
    assert seed == "JBSWY3DPEHPK3PXP", (
        "the second factor did not survive a routine key rotation")

    voice_row = dict(await seeded.fetchrow(
        "SELECT * FROM voice_profiles WHERE id = $1", profile_id))
    assert await use_template(voice_row, lambda t: t) == "template-alpha", (
        "the voiceprint did not survive a routine key rotation, and after the "
        "contract stage there is no plaintext to fall back to")


async def test_the_material_still_opens_after_a_restart(seeded, ids):
    """A restart must not be what makes decryption work.

    Every key here is resolved from the database on each use, so a process that
    has just started should be able to open material sealed by a process that
    is gone. Proven by dropping the pool and the provider's cached KEK — the
    two pieces of state a restart actually clears — and reading again.
    """
    import utils.db as db_module
    from core.crypto import key_provider
    from security.mfa_crypto import use_totp_secret
    from security.voice_crypto import use_template

    p = _tenant_a(ids)
    account = await _store_credential(p, "gmail", "token-restart")
    await _enrol_totp(p, "KRSXG5CTMVRXEZLU")
    profile_id = await _enrol_voice(seeded, p, "template-restart")

    # A restart: new pool, and any per-process key cache dropped.
    old_pool = db_module._pool
    if key_provider.configured() == key_provider.AWS_KMS:
        from core.crypto import aws_kms
        aws_kms.reset_cache()
    fresh = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    db_module._pool = fresh
    try:
        assert await _open(p, account, "gmail", seeded) == "token-restart"

        row = await _read_totp(seeded, p)
        assert await use_totp_secret(
            p, row["secret_ciphertext"], row["secret_nonce"],
            row["encryption_key_version"], lambda s: s) == "KRSXG5CTMVRXEZLU"

        voice_row = dict(await seeded.fetchrow(
            "SELECT * FROM voice_profiles WHERE id = $1", profile_id))
        assert await use_template(voice_row, lambda t: t) == "template-restart"
    finally:
        db_module._pool = old_pool
        await fresh.close()


async def test_the_neighbouring_tenant_cannot_reach_any_of_it(seeded, ids):
    """The same three families, asked the cross-tenant question at once.

    Tenant B's principal is a real, valid principal — this is not an
    unauthenticated request. It simply belongs to a different tenant, which is
    the case that matters: an attacker with a legitimate account somewhere.
    """
    from security.mfa_crypto import MfaKeyUnavailable, use_totp_secret
    from security.voice_crypto import use_template

    a, b = _tenant_a(ids), _tenant_b(ids)
    a_account = await _store_credential(a, "gmail", "token-a")
    await _enrol_totp(a, "JBSWY3DPEHPK3PXP")
    a_profile = await _enrol_voice(seeded, a, "template-a")

    # The credential: B's principal resolves keys in B's tenant, so A's key
    # version is simply not there.
    with pytest.raises(Exception) as exc:
        await _open(b, a_account, "gmail", seeded)
    assert type(exc.value).__name__ in {"KeyNotFound", "CredentialTampered",
                                        "KeyNotResolved"}, (
        f"tenant B opened tenant A's credential, or failed for an unrelated "
        f"reason: {type(exc.value).__name__}: {exc.value}")

    # The second factor: A's seed under B's principal. `use_totp_secret` refuses
    # rather than returning None, which is the fail-closed direction.
    row = await _read_totp(seeded, a)
    with pytest.raises(MfaKeyUnavailable):
        await use_totp_secret(
            b, row["secret_ciphertext"], row["secret_nonce"],
            row["encryption_key_version"], lambda s: s)

    # The voiceprint: bound to the profile id by AAD, so moving the row's
    # ciphertext under another profile is refused. Proven by asking a template
    # to open under a profile id that is not its own.
    a_row = dict(await seeded.fetchrow(
        "SELECT * FROM voice_profiles WHERE id = $1", a_profile))
    b_profile = await _enrol_voice(seeded, b, "template-b")
    b_row = dict(await seeded.fetchrow(
        "SELECT * FROM voice_profiles WHERE id = $1", b_profile))
    stolen = dict(b_row)
    stolen["template_ciphertext"] = a_row["template_ciphertext"]
    stolen["template_nonce"] = a_row["template_nonce"]
    with pytest.raises(Exception):
        await use_template(stolen, lambda t: t)


# ─────────────────────────────────────────────────────────────────────────────
# Flow: retention through the REAL scheduler entry point
# ─────────────────────────────────────────────────────────────────────────────


async def _conversation(admin, *, tenant, workspace, owner, age_days: int):
    # `owner` is the tenant's user, whose window the pass applies: since 093
    # (ADR-0009) a pass reaches only that user's own conversations.
    cid = await admin.fetchval(
        "INSERT INTO conversations (tenant_id, workspace_id, owner_user_id, session_id, "
        " created_at) VALUES ($1,$2,$3,'p415', NOW() - ($4 || ' days')::interval) "
        "RETURNING id", tenant, workspace, owner, str(age_days))
    await admin.execute(
        "INSERT INTO messages (conversation_id, tenant_id, workspace_id, owner_user_id, "
        " role, content, created_at) VALUES ($1,$2,$3,$4,'user','hello', "
        " NOW() - ($5 || ' days')::interval)",
        cid, tenant, workspace, owner, str(age_days))
    return cid


async def test_the_scheduler_expires_one_tenant_and_leaves_its_neighbour(
        seeded, ids):
    """**Through `run_retention_once`, not through `enforce_for`.**

    The retention suite calls the enforcement function directly, which proves
    the deletion logic. What it cannot prove is that the *scheduler* — a
    different process, on a connection bound by `scheduler_connection` rather
    than by a request — reaches the same rows and stops at the same boundary.

    That distinction has already produced one defect: the scheduler originally
    used `owner_connection`, which binds a private tenant, and the test fixture
    used a superuser DSN that bypassed RLS and hid it. So the boundary is
    asserted here through the real entry point.
    """
    from core.jobs.scheduler import run_retention_once
    from core.privacy.memory import RetentionSettings
    from core.privacy.store import save_settings

    a, b = _tenant_a(ids), _tenant_b(ids)
    await save_settings(a, RetentionSettings(conversation_retention_days=30))
    await save_settings(b, RetentionSettings(conversation_retention_days=30))

    a_old = await _conversation(seeded, tenant=ids.tenant_a,
                                workspace=ids.workspace_a, owner=ids.user_a, age_days=90)
    a_new = await _conversation(seeded, tenant=ids.tenant_a,
                                workspace=ids.workspace_a, owner=ids.user_a, age_days=1)
    b_old = await _conversation(seeded, tenant=ids.tenant_b,
                                workspace=ids.workspace_b, owner=ids.user_b, age_days=90)

    outcome = await run_retention_once(force=True)
    assert outcome.status in {"succeeded", "skipped"}, outcome
    if outcome.status == "skipped":
        pytest.skip("another instance holds the retention lock")

    async def alive(cid):
        return await seeded.fetchval(
            "SELECT count(*) FROM conversations WHERE id = $1", cid)

    assert await alive(a_old) == 0, (
        "the scheduler did not remove a conversation past tenant A's window")
    assert await alive(a_new) == 1, (
        "the scheduler removed a conversation inside tenant A's window")

    # The neighbour. B has the same window and an equally old conversation, so
    # this is not testing that B was skipped — it is testing that B's pass was
    # B's own.
    assert await alive(b_old) == 0, (
        "the scheduler reached tenant A but not tenant B, so a fleet-wide pass "
        "is not fleet-wide")

    # And the run is recorded by the process that did it.
    seen = await seeded.fetchval(
        "SELECT tenants_seen FROM job_runs WHERE id = $1", outcome.run_id)
    assert seen >= 2, (
        f"the pass recorded {seen} tenant(s); both seeded tenants have a "
        f"retention window, so a fleet pass must have seen at least 2")


async def test_a_tenant_with_no_window_is_left_entirely_alone(seeded, ids):
    """Unset means keep, not delete. Asserted through the scheduler because the
    scheduler is what would delete it at 3am."""
    from core.jobs.scheduler import run_retention_once
    from core.privacy.memory import RetentionSettings
    from core.privacy.store import save_settings

    a = _tenant_a(ids)
    await save_settings(a, RetentionSettings(conversation_retention_days=30))
    # Tenant B deliberately has no settings row at all.
    a_old = await _conversation(seeded, tenant=ids.tenant_a,
                                workspace=ids.workspace_a, owner=ids.user_a, age_days=90)
    b_old = await _conversation(seeded, tenant=ids.tenant_b,
                                workspace=ids.workspace_b, owner=ids.user_b, age_days=900)

    outcome = await run_retention_once(force=True)
    if outcome.status == "skipped":
        pytest.skip("another instance holds the retention lock")

    assert await seeded.fetchval(
        "SELECT count(*) FROM conversations WHERE id = $1", a_old) == 0
    assert await seeded.fetchval(
        "SELECT count(*) FROM conversations WHERE id = $1", b_old) == 1, (
        "a tenant that never configured retention had 900-day-old data deleted")


# ─────────────────────────────────────────────────────────────────────────────
# Flow: erasure, and the blast radius it must not exceed
# ─────────────────────────────────────────────────────────────────────────────


async def test_erasure_shreds_this_account_and_only_this_account(seeded, ids):
    """Crypto-shredding is the strongest deletion in the system and therefore
    the one with the worst failure mode. Two things have to be true at once: the
    erased account's material must become unopenable, and the neighbour's must
    not.

    Both tenants hold all three families, so a shred that reached one row too
    far has somewhere to show up.
    """
    from core.privacy.erasure import erase_keys
    from security.mfa_crypto import MfaKeyUnavailable, use_totp_secret

    a, b = _tenant_a(ids), _tenant_b(ids)
    a_account = await _store_credential(a, "gmail", "token-a")
    b_account = await _store_credential(b, "gmail", "token-b")
    await _enrol_totp(a, "JBSWY3DPEHPK3PXP")
    await _enrol_totp(b, "KRSXG5CTMVRXEZLU")

    a_row = await _read_totp(seeded, a)
    b_row = await _read_totp(seeded, b)

    result = await erase_keys(a, scope="account")
    assert result.ok, result

    # A's credential: the key is gone, so the ciphertext is unopenable. This is
    # the point of shredding — the row may still exist in a replica or an
    # unvacuumed page and it no longer matters.
    with pytest.raises(Exception) as exc:
        await _open(a, a_account, "gmail", seeded)
    assert type(exc.value).__name__ in {"KeyDestroyed", "KeyNotFound"}, (
        f"tenant A's credential still opens after erasure, or failed for an "
        f"unrelated reason: {type(exc.value).__name__}")

    # A's second factor.
    with pytest.raises(MfaKeyUnavailable):
        await use_totp_secret(
            a, a_row["secret_ciphertext"], a_row["secret_nonce"],
            a_row["encryption_key_version"], lambda s: s)

    # B is untouched — both families.
    assert await _open(b, b_account, "gmail", seeded) == "token-b", (
        "erasing tenant A's keys made tenant B's credential unopenable")
    assert await use_totp_secret(
        b, b_row["secret_ciphertext"], b_row["secret_nonce"],
        b_row["encryption_key_version"], lambda s: s) == "KRSXG5CTMVRXEZLU", (
        "erasing tenant A's keys destroyed tenant B's second factor")


async def test_a_shredded_key_is_not_quietly_re_minted(seeded, ids):
    """The failure that would undo erasure without any error.

    `active_dek` mints when a tenant has no live key. After a shred a tenant has
    no live key — so a code path that minted here would hand the account a
    working keyring again and report success, and nothing in the response would
    say that the erasure had been reversed.
    """
    from core.privacy.erasure import erase_keys
    from security.mfa_crypto import active_mfa_dek

    a = _tenant_a(ids)
    await _store_credential(a, "gmail", "token-a")
    await _enrol_totp(a, "JBSWY3DPEHPK3PXP")

    assert (await erase_keys(a, scope="account")).ok

    live_mfa = await seeded.fetchval(
        "SELECT count(*) FROM user_mfa_keys WHERE tenant_id = $1 "
        "  AND user_id = $2 AND destroyed_at IS NULL",
        ids.tenant_a, ids.user_a)
    assert live_mfa == 0, "an MFA key survived the shred"

    # Opening must not mint. `mint=False` is the contract; this asserts it from
    # the outside, where a regression would actually be felt.
    with pytest.raises(Exception):
        await active_mfa_dek(a, mint=False)

    still_zero = await seeded.fetchval(
        "SELECT count(*) FROM user_mfa_keys WHERE tenant_id = $1 "
        "  AND user_id = $2 AND destroyed_at IS NULL",
        ids.tenant_a, ids.user_a)
    assert still_zero == 0, (
        "reading after an erasure created new key material, which silently "
        "undoes the erasure")
