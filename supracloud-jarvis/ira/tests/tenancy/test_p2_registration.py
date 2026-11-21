"""Phase 2 — registration and email verification.

Batch 1 made a verified user with a membership resolvable. This creates the
user and proves the address, and deliberately grants nothing: after
verification the account still resolves to *nothing*, because
`_resolve_cloud_principal` requires a membership that grants access and
provisioning is the next batch. `test_a_verified_account_still_resolves_to_
nothing` pins that, so "registered but not provisioned" is a state the system
holds rather than a gap between two half-applied writes.

── What most of this file is about ─────────────────────────────────────────

`POST /auth/register` is unauthenticated and, when enabled, public. Its
response is an account-enumeration oracle unless every branch looks the same,
so the assertions are mostly about *sameness*: the same status, the same body,
and no side channel that separates "this address is new" from "this address is
already registered".

The other half is the token. It is never stored — only a SHA-256 — for the
reason `user_sessions.token_hash` records: a table holding usable tokens turns
a read of that table into account takeover.
"""
from __future__ import annotations

import hashlib
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

GOOD_PASSWORD = "a-perfectly-adequate-passphrase"
DOMAIN = "p2reg.example"


def _email(local: str) -> str:
    return f"{local}@{DOMAIN}"


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
async def registration_on(monkeypatch, pool):
    """Registration enabled, and a clean slate for this domain."""
    from config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "ira_cloud_registration_enabled", True,
                        raising=False)
    monkeypatch.setattr(settings, "ira_email_verification_ttl_hours", 24,
                        raising=False)

    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await _wipe(admin)
        yield admin
    finally:
        await _wipe(admin)
        await admin.close()


async def _wipe(admin):
    """Remove every account this file creates, matched the way the DATABASE
    matches them.

    `normalised_email`, not `email`, and the difference is not cosmetic. One
    test registers `alice@P2REG.EXAMPLE` to prove the domain case does not
    create a second account. A case-sensitive `email LIKE '%@p2reg.example'`
    never matches that row, so it survived every teardown — and on the NEXT run
    the very first registration found an existing address and returned
    `created=False`, failing tests that had nothing to do with it.

    That made the suite pass once on a clean database and fail afterwards, and
    it briefly made six defect plants look like they were being caught when
    they were only colliding with leftover state. Matching on the normalised
    column removes the whole class: it is the same value the unique index and
    the resolver use.

    Child-first, though both foreign keys cascade — being explicit costs
    nothing and does not depend on the cascade staying.
    """
    await admin.execute(
        "DELETE FROM email_verification_tokens WHERE normalise_email(email) LIKE $1",
        f"%@{DOMAIN}")
    await admin.execute(
        "DELETE FROM user_credentials WHERE user_id IN "
        "(SELECT id FROM users WHERE normalised_email LIKE $1)", f"%@{DOMAIN}")
    await admin.execute("DELETE FROM users WHERE normalised_email LIKE $1",
                        f"%@{DOMAIN}")


# ─────────────────────────────────────────────────────────────────────────────
# The happy path
# ─────────────────────────────────────────────────────────────────────────────


async def test_registration_creates_an_unverified_account(registration_on):
    from core.identity.registration import register_account

    outcome = await register_account(_email("alice"), GOOD_PASSWORD)
    assert outcome.created is True
    assert outcome.token

    row = await registration_on.fetchrow(
        "SELECT status, email_verified_at FROM users WHERE email = $1",
        _email("alice"))
    assert row["status"] == "active"
    assert row["email_verified_at"] is None, (
        "registration marked the address verified. Registration proves nobody "
        "controls the address — only verification does.")


async def test_verification_marks_the_address_verified(registration_on):
    from core.identity.registration import register_account, verify_email

    outcome = await register_account(_email("alice"), GOOD_PASSWORD)
    assert await verify_email(outcome.token) == outcome.user_id

    verified = await registration_on.fetchval(
        "SELECT email_verified_at FROM users WHERE email = $1", _email("alice"))
    assert verified is not None


async def test_a_verified_account_still_resolves_to_nothing(registration_on, monkeypatch):
    """The batch boundary, asserted rather than assumed.

    A verified identity is not an authorisation. Until provisioning gives this
    user a membership, `_resolve_cloud_principal` must refuse — and if it did
    not, registration would be granting tenant access, which is precisely the
    thing this batch must not do.
    """
    from core.editions import EDITION_CLOUD
    from core.identity.registration import register_account, verify_email
    from core.identity.resolver import CloudIdentityNotAvailable, resolve_principal

    outcome = await register_account(_email("alice"), GOOD_PASSWORD)
    await verify_email(outcome.token)

    monkeypatch.setattr("core.identity.resolver.current_edition",
                        lambda: EDITION_CLOUD)
    with pytest.raises(CloudIdentityNotAvailable):
        await resolve_principal(_email("alice"))


# ─────────────────────────────────────────────────────────────────────────────
# Enumeration
# ─────────────────────────────────────────────────────────────────────────────


async def test_registering_an_existing_address_is_indistinguishable(registration_on):
    """Same outcome shape, and no exception, for an address already taken."""
    from core.identity.registration import register_account

    first = await register_account(_email("alice"), GOOD_PASSWORD)
    second = await register_account(_email("alice"), "another-long-password-x")

    assert first.created is True
    assert second.created is False, "the second registration created a duplicate"
    assert second.token is None

    n = await registration_on.fetchval(
        "SELECT count(*) FROM users WHERE email = $1", _email("alice"))
    assert n == 1


async def test_a_second_registration_does_not_overwrite_the_credential(registration_on):
    """The account-takeover shape of the same defect.

    If registering an existing address rewrote the password hash, anyone could
    reset any account by "registering" it. The second call must change nothing.
    """
    from core.identity.registration import register_account

    await register_account(_email("alice"), GOOD_PASSWORD)
    before = await registration_on.fetchval(
        "SELECT password_hash FROM user_credentials WHERE user_id = "
        "(SELECT id FROM users WHERE email = $1)", _email("alice"))

    await register_account(_email("alice"), "attacker-chosen-password-99")
    after = await registration_on.fetchval(
        "SELECT password_hash FROM user_credentials WHERE user_id = "
        "(SELECT id FROM users WHERE email = $1)", _email("alice"))

    assert before == after, (
        "registering an existing address rewrote its password hash — that is "
        "an unauthenticated password reset for any known address")


async def test_the_domain_case_does_not_create_a_second_account(registration_on):
    """`normalise_email()` decides identity, so `alice@P2REG.EXAMPLE` is the
    same account and must not register twice."""
    from core.identity.registration import register_account

    await register_account(_email("alice"), GOOD_PASSWORD)
    second = await register_account(f"alice@{DOMAIN.upper()}", GOOD_PASSWORD)
    assert second.created is False

    n = await registration_on.fetchval(
        "SELECT count(*) FROM users WHERE normalised_email = normalise_email($1)",
        _email("alice"))
    assert n == 1


# ─────────────────────────────────────────────────────────────────────────────
# The token
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_token_is_never_stored(registration_on):
    """Only its SHA-256. A row that holds a usable token is account takeover
    for any address whose mail the attacker cannot read."""
    from core.identity.registration import register_account

    outcome = await register_account(_email("alice"), GOOD_PASSWORD)
    stored = await registration_on.fetchval(
        "SELECT token_hash FROM email_verification_tokens WHERE email = $1",
        _email("alice"))

    assert stored != outcome.token
    assert stored == hashlib.sha256(outcome.token.encode()).hexdigest()
    # And the token does not appear anywhere in the row.
    row = await registration_on.fetchrow(
        "SELECT * FROM email_verification_tokens WHERE email = $1", _email("alice"))
    assert outcome.token not in " ".join(str(v) for v in row.values())


async def test_a_token_is_single_use(registration_on):
    from core.identity.registration import (
        VerificationError, register_account, verify_email,
    )

    outcome = await register_account(_email("alice"), GOOD_PASSWORD)
    await verify_email(outcome.token)
    with pytest.raises(VerificationError):
        await verify_email(outcome.token)


async def test_an_expired_token_is_refused(registration_on):
    from core.identity.registration import (
        VerificationError, register_account, verify_email,
    )

    outcome = await register_account(_email("alice"), GOOD_PASSWORD)
    # Both timestamps move. `email_verification_tokens_expiry_is_after_issue`
    # forbids a row whose expiry precedes its issue — correctly, since that is
    # nonsense — so back-dating only `expires_at` produces a row the database
    # refuses rather than an expired one. An old token issued two days ago and
    # good for one is what an expired token actually looks like.
    await registration_on.execute(
        "UPDATE email_verification_tokens "
        "   SET created_at = NOW() - interval '2 days', "
        "       expires_at = NOW() - interval '1 day' "
        " WHERE email = $1", _email("alice"))
    with pytest.raises(VerificationError):
        await verify_email(outcome.token)


async def test_a_token_for_a_changed_address_is_refused(registration_on):
    """A token proves ONE address. If the account's address changed afterwards,
    honouring the token would mark an address verified that nobody proved."""
    from core.identity.registration import (
        VerificationError, register_account, verify_email,
    )

    outcome = await register_account(_email("alice"), GOOD_PASSWORD)
    await registration_on.execute(
        "UPDATE users SET email = $1 WHERE id = $2", _email("alice-new"),
        outcome.user_id)
    with pytest.raises(VerificationError):
        await verify_email(outcome.token)


@pytest.mark.parametrize("bad", ["", "   ", "not-a-real-token", "x" * 43])
async def test_a_garbage_token_is_refused(registration_on, bad):
    from core.identity.registration import VerificationError, verify_email

    with pytest.raises(VerificationError):
        await verify_email(bad)


async def test_a_deleted_account_cannot_be_verified(registration_on):
    from core.identity.registration import (
        VerificationError, register_account, verify_email,
    )

    outcome = await register_account(_email("alice"), GOOD_PASSWORD)
    await registration_on.execute(
        "UPDATE users SET deleted_at = NOW() WHERE id = $1", outcome.user_id)
    with pytest.raises(VerificationError):
        await verify_email(outcome.token)


# ─────────────────────────────────────────────────────────────────────────────
# Passwords and the flag
# ─────────────────────────────────────────────────────────────────────────────


async def test_registration_is_refused_when_the_flag_is_off(pool, monkeypatch):
    """Building the capability and opening it to the internet are separate
    decisions, and the default is off."""
    from config import get_settings
    from core.identity.registration import RegistrationDisabled, register_account

    monkeypatch.setattr(get_settings(), "ira_cloud_registration_enabled", False,
                        raising=False)
    with pytest.raises(RegistrationDisabled):
        await register_account(_email("nobody"), GOOD_PASSWORD)


async def test_the_flag_defaults_to_off():
    """Read from the class default, not from the running settings object, so a
    test that flipped it cannot make this pass."""
    from config import Settings

    assert Settings.model_fields["ira_cloud_registration_enabled"].default is False


@pytest.mark.parametrize("password", ["", "short", "a" * 11])
async def test_a_weak_password_is_refused(registration_on, password):
    from core.identity.registration import RegistrationError, register_account

    with pytest.raises(RegistrationError):
        await register_account(_email("weak"), password)


async def test_a_password_longer_than_bcrypt_accepts_is_refused(registration_on):
    """bcrypt truncates at 72 bytes. Accepting a 200-character passphrase and
    storing a prefix of it means the user's password is not what they typed."""
    from core.identity.registration import RegistrationError, register_account

    with pytest.raises(RegistrationError):
        await register_account(_email("long"), "x" * 200)


async def test_the_password_is_stored_only_as_a_hash(registration_on):
    from core.identity.registration import register_account

    await register_account(_email("alice"), GOOD_PASSWORD)
    stored = await registration_on.fetchval(
        "SELECT password_hash FROM user_credentials WHERE user_id = "
        "(SELECT id FROM users WHERE email = $1)", _email("alice"))
    assert GOOD_PASSWORD not in stored
    assert stored.startswith("$2"), "not a bcrypt hash"


# ─────────────────────────────────────────────────────────────────────────────
# The credential table's boundary
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_user_cannot_read_another_users_credential(registration_on):
    """`user_credentials` is FORCE RLS keyed on `app.user_id` — the one thing
    that stops a bug in one account's code path reaching every password hash."""
    from core.identity.registration import register_account

    a = await register_account(_email("alice"), GOOD_PASSWORD)
    b = await register_account(_email("bob"), GOOD_PASSWORD)

    app = await asyncpg.connect(DSN)
    try:
        async with app.transaction():
            await app.execute("SELECT set_config('app.user_id',$1,true)",
                              str(a.user_id))
            rows = await app.fetch(
                "SELECT user_id FROM user_credentials WHERE user_id = $1", b.user_id)
        assert rows == [], "one account read another's password hash"
    finally:
        await app.close()


async def test_an_unbound_connection_reads_no_credentials(registration_on):
    """FORCE RLS with no `app.user_id` bound matches zero rows — the fail-closed
    direction, asserted so a future NO FORCE cannot pass silently."""
    from core.identity.registration import register_account

    await register_account(_email("alice"), GOOD_PASSWORD)
    app = await asyncpg.connect(DSN)
    try:
        assert await app.fetchval("SELECT count(*) FROM user_credentials") == 0
    finally:
        await app.close()


async def test_the_database_refuses_a_plaintext_password(registration_on):
    """`user_credentials_hash_is_not_plaintext`. The one catastrophic mistake
    is made impossible rather than merely unlikely."""
    uid = await registration_on.fetchval(
        "INSERT INTO users (id,email,display_name,auth_provider,status) "
        "VALUES (gen_random_uuid(),$1,'X','password','active') RETURNING id",
        _email("plain"))
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await registration_on.execute(
            "INSERT INTO user_credentials (user_id,password_hash) VALUES ($1,$2)",
            uid, "hunter2")
