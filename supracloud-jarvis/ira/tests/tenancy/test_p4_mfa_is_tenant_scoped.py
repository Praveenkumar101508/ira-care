"""Phase 4 batch 09 — D-23 closed: a second factor belongs to a membership.

**The decision.** A human who belongs to two organisations holds two enrolments,
configured independently, and neither organisation can see or use the other's.

The alternative — one factor following the human — is what users would ask for,
and it is the one D-17 exists to prevent: `user_mfa_keys` is keyed per
(tenant, user), so satisfying a login in Globex with the seed Alice enrolled at
Acme means opening key material scoped to a tenant Globex has no relationship
with. The answer is the one that does not cross that boundary.

The cost is a wording obligation. "MFA is on for your account" is a sentence
this system cannot truthfully say, so `GET /auth/totp/status` reports which
tenant the answer is about and calls its scope `tenant` in the payload.

── The regression this file exists for ─────────────────────────────────────

`065` rescoped the enrolment index from `(user_id)` to `(tenant_id, user_id)`.
`api/routes/totp.py` still named the old one in its `ON CONFLICT` target.
PostgreSQL does not fall back — it raises *"there is no unique or exclusion
constraint matching the ON CONFLICT specification"* — so **enrolment was broken
for every user the moment 065 applied**, and the phase that shipped 065 declared
itself complete.

Nothing caught it because the migration had its own tests, the route had its
own, and the pair is what broke. So these tests drive the ROUTE against the real
schema: enrol twice in one tenant, and once in each of two.
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

#: The tenant KEK the MFA seeds are sealed under. Generated, never written
#: down — a 32-byte literal in a test file is what Gitleaks blocks.
_KEK = _b64.b64encode(_stdlib_secrets.token_bytes(32)).decode()
os.environ.setdefault("IRA_CONNECTOR_ENCRYPTION_KEY", _KEK)
os.environ.setdefault("IRA_CONNECTOR_ENCRYPTION_KEY_ID", "kek-d20")

for _k, _v in {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-test",
    "REDIS_PASSWORD": "ci-test",
    "VLLM_API_KEY": "ci-test",
}.items():
    os.environ.setdefault(_k, _v)

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
USER = uuid.uuid4()
OTHER_USER = uuid.uuid4()
USERNAME = f"d20-{uuid.uuid4().hex[:8]}"


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
    """One human in two tenants — the situation D-23 is about."""
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await _wipe(admin)
        for tenant, slug in ((TENANT_A, "d20-a"), (TENANT_B, "d20-b")):
            await admin.execute(
                "INSERT INTO tenants (id,name,slug,edition) "
                "VALUES ($1,$2,$3,'cloud')", tenant, f"D20 {slug}", slug)
        for user in (USER, OTHER_USER):
            await admin.execute(
                "INSERT INTO users (id,display_name,auth_provider,status) "
                "VALUES ($1,'D','password','active')", user)
        for tenant in (TENANT_A, TENANT_B):
            await admin.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
                "VALUES ($1,$2,'member','active')", tenant, USER)
        # A second human in tenant A, for the "another member" boundary.
        await admin.execute(
            "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
            "VALUES ($1,$2,'member','active')", TENANT_A, OTHER_USER)
        yield admin
    finally:
        await _wipe(admin)
        await admin.close()


async def _wipe(admin):
    for t in (TENANT_A, TENANT_B):
        await admin.execute("DELETE FROM tenants WHERE id = $1", t)
    for u in (USER, OTHER_USER):
        await admin.execute("DELETE FROM users WHERE id = $1", u)


def _principal(tenant=TENANT_A, user=USER, username=None):
    from core.identity.principal import Principal

    return Principal(user_id=user, tenant_id=tenant, workspace_id=None,
                     session_id=uuid.uuid4(), edition="cloud",
                     username=username or USERNAME, is_owner=False)


async def _enrol_through_the_route(monkeypatch, principal):
    """Drive `POST /auth/totp/enroll` itself.

    Not a hand-written INSERT. The defect this file exists for lived in the
    route's `ON CONFLICT` clause, and a test that wrote its own statement would
    have been green through the entire outage.

    The route re-authenticates since P0-01. These principals are built here
    and hold no password, so the password check is answered "yes" — it is not
    what this file is about, and `test_totp_enrolment_reauth.py` drives it for
    real against the owner, a household account and a cloud member. Everything
    this file asserts happens after it: the write, the conflict target and RLS.
    """
    from api.routes import totp as totp_routes

    async def _fake_principal(username):
        return principal

    async def _password_confirmed(*_a, **_k):
        return True

    monkeypatch.setattr(totp_routes, "principal_from_username", _fake_principal)
    monkeypatch.setattr("security.reauth.verify_account_password", _password_confirmed)
    result = await totp_routes.totp_enroll(
        totp_routes.EnrollRequest(password="confirmed-by-the-stub"),
        _user=principal.username)
    assert isinstance(result, totp_routes.EnrollResponse), (
        f"enrolment was refused: {getattr(result, 'body', result)!r}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# The regression
# ─────────────────────────────────────────────────────────────────────────────


async def test_enrolment_works_at_all(seeded, monkeypatch):
    """**The outage.** After `065` this raised for every user, because the route
    named an index the migration had dropped."""
    result = await _enrol_through_the_route(monkeypatch, _principal())
    assert result.secret and result.provisioning_uri

    assert await seeded.fetchval(
        "SELECT count(*) FROM totp_secrets WHERE user_id=$1 AND tenant_id=$2",
        USER, TENANT_A) == 1


async def test_re_enrolling_replaces_rather_than_duplicating(seeded, monkeypatch):
    """The `ON CONFLICT` path, which is the half that was broken. A second
    enrolment in the same tenant must UPDATE — two live seeds for one account in
    one tenant is an ambiguity the login path cannot resolve."""
    first = await _enrol_through_the_route(monkeypatch, _principal())
    second = await _enrol_through_the_route(monkeypatch, _principal())
    assert first.secret != second.secret

    assert await seeded.fetchval(
        "SELECT count(*) FROM totp_secrets WHERE user_id=$1 AND tenant_id=$2",
        USER, TENANT_A) == 1, "re-enrolling created a second live enrolment"


async def test_the_same_human_enrols_independently_in_each_tenant(seeded,
                                                                  monkeypatch):
    """**D-23's decision, exercised through the route.**

    Two memberships, two enrolments, two different seeds. Before `065` the
    second was refused outright; before this batch's route fix, neither worked.
    """
    a = await _enrol_through_the_route(monkeypatch, _principal(TENANT_A))
    b = await _enrol_through_the_route(monkeypatch, _principal(TENANT_B))
    assert a.secret != b.secret, (
        "the two tenants share a seed — the enrolment did not go to the tenant "
        "the caller was in")

    rows = await seeded.fetch(
        "SELECT tenant_id FROM totp_secrets WHERE user_id=$1 ORDER BY tenant_id",
        USER)
    assert {r["tenant_id"] for r in rows} == {TENANT_A, TENANT_B}


# ─────────────────────────────────────────────────────────────────────────────
# The boundary D-23 refuses to cross
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_tenant_cannot_see_another_tenants_enrolment(seeded, monkeypatch):
    """The whole reason the answer is "per membership".

    Enrolled in A only. A principal bound to B must find nothing — not a
    disabled factor, not somebody else's: nothing.
    """
    from core.identity.db_context import tenant_connection

    await _enrol_through_the_route(monkeypatch, _principal(TENANT_A))

    async with tenant_connection(_principal(TENANT_B)) as conn:
        visible = await conn.fetchval(
            "SELECT count(*) FROM totp_secrets WHERE user_id = $1", USER)
    assert visible == 0, (
        "a principal in tenant B can see the enrolment this user made in "
        "tenant A. The factor is no longer scoped to the membership, and B can "
        "reason about a credential belonging to a tenant it has no "
        "relationship with.")


async def test_a_login_in_the_unenrolled_tenant_is_not_challenged(seeded,
                                                                  monkeypatch):
    """**The honest consequence, asserted rather than left implicit.**

    This is what D-23 decides and what the wording obligation exists for.
    Enrolled in A, Alice signs into B and is NOT asked for a code, because she
    has not configured one there. That is correct under the decision — and it is
    exactly why no surface may say "MFA is on for your account".
    """
    from security.totp import check_login_second_factor

    await _enrol_through_the_route(monkeypatch, _principal(TENANT_A))
    # An enrolment is `enabled=FALSE` until `/auth/totp/verify` confirms the
    # authenticator produces valid codes, and the LOGIN gate reads only enabled
    # rows. Mirroring that here rather than skipping it: without this the test
    # would compare two `not_enrolled` answers and pass for the wrong reason.
    await seeded.execute(
        "UPDATE totp_secrets SET enabled=TRUE WHERE user_id=$1 AND tenant_id=$2",
        USER, TENANT_A)

    in_a = await check_login_second_factor(
        USERNAME, None, principal=_principal(TENANT_A))
    assert in_a != "not_enrolled", (
        "the tenant where the factor WAS configured did not demand it")

    in_b = await check_login_second_factor(
        USERNAME, None, principal=_principal(TENANT_B))
    assert in_b == "not_enrolled", (
        "tenant B demanded a factor the user never configured there — which "
        "could only be satisfied by tenant A's seed, and reaching for it is the "
        "cross-tenant key access D-17 forbids")


async def test_another_member_of_the_same_tenant_is_unaffected(seeded, monkeypatch):
    """Tenant-scoped is not tenant-shared. Two members of one organisation hold
    separate factors."""
    from core.identity.db_context import tenant_connection

    await _enrol_through_the_route(monkeypatch, _principal(TENANT_A, USER))

    other = _principal(TENANT_A, OTHER_USER, username=f"{USERNAME}-other")
    async with tenant_connection(other) as conn:
        visible = await conn.fetchval(
            "SELECT count(*) FROM totp_secrets WHERE user_id = $1", USER)
    assert visible == 0, (
        "one member of a tenant can see another member's enrolment row")


# ─────────────────────────────────────────────────────────────────────────────
# The wording obligation
# ─────────────────────────────────────────────────────────────────────────────


async def test_status_names_the_tenant_it_is_talking_about(seeded, monkeypatch):
    """A client cannot render this answer without saying which organisation it
    is about, because the tenant is in the payload and the scope is a constant
    that reads `tenant`."""
    from api.routes import totp as totp_routes

    await _enrol_through_the_route(monkeypatch, _principal(TENANT_A))

    status = await totp_routes.totp_status(_user=USERNAME)
    assert status.enrolled is True
    assert status.scope == "tenant"
    assert status.tenant_id == str(TENANT_A)


async def test_status_reports_the_other_tenant_as_unenrolled(seeded, monkeypatch):
    """The same call, the same human, a different membership, a different
    answer. If this returned `True` the endpoint would be describing the person
    rather than the membership, and the wording obligation would be unmeetable.
    """
    from api.routes import totp as totp_routes

    async def _fake_principal(username):
        return _principal(TENANT_B)

    await _enrol_through_the_route(monkeypatch, _principal(TENANT_A))
    monkeypatch.setattr(totp_routes, "principal_from_username", _fake_principal)

    status = await totp_routes.totp_status(_user=USERNAME)
    assert status.enrolled is False, (
        "status reported an enrolment in a tenant where none exists — it is "
        "answering about the account, which is the claim D-23 forbids")
    assert status.tenant_id == str(TENANT_B)


def test_no_surface_claims_mfa_is_on_for_the_account():
    """The wording obligation, checked in the source rather than trusted.

    D-23's cost is that "MFA is enabled for your account" becomes false. A
    string like that is how the decision gets quietly reversed in the UI while
    the backend stays correct.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    offenders = []
    for path in list((root / "api" / "routes").glob("*.py")) + [
            root / "auth" / "routes.py"]:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8").lower()
        for phrase in ("mfa is enabled for your account",
                       "mfa enabled for this account",
                       "two-factor is on for your account"):
            if phrase in text:
                offenders.append(f"{path.name}: {phrase!r}")
    assert not offenders, (
        f"account-level MFA wording found: {offenders}. A second factor is "
        f"scoped to a membership (D-23); saying otherwise tells a user they are "
        f"protected in organisations where they are not.")
