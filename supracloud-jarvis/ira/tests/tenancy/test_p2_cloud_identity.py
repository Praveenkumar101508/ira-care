"""Phase 2 — cloud principal resolution, which until now refused unconditionally.

`PHASE_2_AUTH_INVENTORY.md` §2 recorded the state plainly: `resolve_principal`
raised `CloudIdentityNotAvailable` for `EDITION_CLOUD`, so *"cloud does not
serve authenticated traffic. This is the Phase 2 deliverable."*

Refusing was right while there was nothing to resolve — the alternative was
minting a principal carrying `PRIVATE_TENANT_ID` inside a multi-tenant process.
This suite is about the resolution that replaces the refusal, and most of it is
about what must STILL be refused.

── The two properties that matter most ─────────────────────────────────────

**Nothing the caller says is authority.** `subject` selects a row, it does not
describe one. Tenant, role and scopes are read from the database; a requested
tenant is verified against membership before it is honoured.

**A tenant owner is not the deployment owner.** `Principal.is_owner` is hard-
false in cloud regardless of tenant role. 3C-04, 3C-08 and P2-07 were each a
version of that confusion, and `test_a_tenant_primary_owner_is_not_the_
deployment_owner` is the assertion that keeps it from returning — including via
the scope map, where reusing the private edition's `scopes_for` would have
handed a customer `security_admin` and `private_memory_read` because the role
name `primary_owner` happens to appear in both vocabularies.
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

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

TA = uuid.UUID("11111111-1111-1111-1111-1111111111c1")
TB = uuid.UUID("22222222-2222-2222-2222-2222222222c1")
U_SOLO = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000c101")
U_BOTH = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000c102")
U_NONE = uuid.UUID("cccccccc-0000-0000-0000-00000000c103")
U_UNVERIFIED = uuid.UUID("dddddddd-0000-0000-0000-00000000c104")
U_SUSPENDED = uuid.UUID("eeeeeeee-0000-0000-0000-00000000c105")
U_DELETED = uuid.UUID("ffffffff-0000-0000-0000-00000000c106")

E_SOLO = "solo@p2cloud.example"
E_BOTH = "both@p2cloud.example"
E_NONE = "none@p2cloud.example"
E_UNVERIFIED = "unverified@p2cloud.example"
E_SUSPENDED = "suspended@p2cloud.example"
E_DELETED = "deleted@p2cloud.example"

ALL_USERS = [U_SOLO, U_BOTH, U_NONE, U_UNVERIFIED, U_SUSPENDED, U_DELETED]


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
async def cloud(monkeypatch, pool):
    """A populated cloud identity register, with the edition set to cloud.

    Seeded on the ADMIN dsn: `tenant_memberships` is forced-RLS and the fixture
    has to create rows for six different users, which no single bound
    connection may do.
    """
    from core.editions import EDITION_CLOUD

    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_CLOUD)
    monkeypatch.setattr("core.identity.resolver.current_edition",
                        lambda: EDITION_CLOUD)

    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await _wipe(admin)
        await admin.execute(
            "INSERT INTO tenants (id,name,slug) VALUES "
            "($1,'P2-Cloud-A','p2-cloud-a'),($2,'P2-Cloud-B','p2-cloud-b')",
            TA, TB)

        async def _user(uid, email, *, verified=True, status="active",
                        deleted=False):
            # `normalised_email` is GENERATED (`normalise_email(email)`) and
            # cannot be inserted. That is the point — normalisation has exactly
            # one definition, and neither this fixture nor the resolver owns it.
            await admin.execute(
                "INSERT INTO users (id,email,display_name,"
                " auth_provider,status,email_verified_at,deleted_at) "
                "VALUES ($1,$2,'T','local',$3,"
                "        CASE WHEN $4 THEN NOW() ELSE NULL END,"
                "        CASE WHEN $5 THEN NOW() ELSE NULL END)",
                uid, email, status, verified, deleted)

        await _user(U_SOLO, E_SOLO)
        await _user(U_BOTH, E_BOTH)
        await _user(U_NONE, E_NONE)
        await _user(U_UNVERIFIED, E_UNVERIFIED, verified=False)
        await _user(U_SUSPENDED, E_SUSPENDED, status="suspended")
        await _user(U_DELETED, E_DELETED, deleted=True)

        await admin.execute(
            "INSERT INTO tenant_memberships (tenant_id,user_id,role,status,joined_at) "
            "VALUES ($1,$2,'admin','active',NOW()),"
            "       ($1,$3,'primary_owner','active',NOW()),"
            "       ($4,$3,'member','active',NOW()),"
            "       ($1,$5,'member','active',NOW()),"
            "       ($1,$6,'member','active',NOW())",
            TA, U_SOLO, U_BOTH, TB, U_SUSPENDED, U_DELETED)
        yield admin
    finally:
        await _wipe(admin)
        await admin.close()


async def _wipe(admin):
    await admin.execute(
        "DELETE FROM tenant_memberships WHERE tenant_id = ANY($1::uuid[])",
        [TA, TB])
    await admin.execute("DELETE FROM users WHERE id = ANY($1::uuid[])", ALL_USERS)
    await admin.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", [TA, TB])


async def _resolve(subject, **kw):
    from core.identity.resolver import resolve_principal

    return await resolve_principal(subject, **kw)


# ─────────────────────────────────────────────────────────────────────────────
# The positive control — cloud now serves authenticated traffic
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_cloud_user_resolves_to_a_real_tenant(cloud):
    """The deliverable. Every negative assertion below would pass against an
    implementation that still refused everything, so this comes first."""
    from core.editions import EDITION_CLOUD

    p = await _resolve(E_SOLO)
    assert p.tenant_id == TA
    assert p.user_id == U_SOLO
    assert p.edition == EDITION_CLOUD
    assert p.roles == frozenset({"admin"})
    assert p.username == E_SOLO


async def test_the_domain_is_matched_case_insensitively(cloud):
    """`normalise_email()` lowercases the DOMAIN only, and the resolver calls
    that function rather than reimplementing it."""
    local, _, domain = E_SOLO.partition("@")
    p = await _resolve(f"{local}@{domain.upper()}")
    assert p.user_id == U_SOLO


async def test_the_local_part_keeps_its_case(cloud):
    """The half a Python `.lower()` gets wrong.

    Local-parts are case-sensitive per RFC 5321, and `normalise_email()`
    preserves them. The first version of the resolver lowercased the whole
    address, which would have refused every user whose address has an uppercase
    local-part — fail-closed, but a legitimate account locked out.

    So `SOLO@…` must NOT resolve to the account registered as `solo@…`, and
    this asserts the refusal rather than the convenience.
    """
    from core.identity.resolver import CloudIdentityNotAvailable

    local, _, domain = E_SOLO.partition("@")
    with pytest.raises(CloudIdentityNotAvailable):
        await _resolve(f"{local.upper()}@{domain}")


async def test_the_resolved_username_is_the_stored_normalised_form(cloud):
    """Not the string the caller sent — what reaches logs and audit rows should
    be the identity the database knows."""
    local, _, domain = E_SOLO.partition("@")
    p = await _resolve(f"  {local}@{domain.upper()}  ")
    assert p.username == E_SOLO


# ─────────────────────────────────────────────────────────────────────────────
# Nothing the caller says is authority
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_requested_tenant_is_verified_against_membership(cloud):
    """`U_SOLO` belongs to TA only. Asking for TB must not produce TB."""
    from core.identity.resolver import CloudIdentityNotAvailable

    with pytest.raises(CloudIdentityNotAvailable):
        await _resolve(E_SOLO, tenant_id=TB)


async def test_a_tenant_that_does_not_exist_is_refused_the_same_way(cloud):
    """Refused identically to a real tenant the user is not in, so the response
    cannot be used to discover which tenants exist."""
    from core.identity.resolver import CloudIdentityNotAvailable

    with pytest.raises(CloudIdentityNotAvailable):
        await _resolve(E_SOLO, tenant_id=uuid.uuid4())


async def test_a_member_of_two_tenants_may_name_either(cloud):
    """The positive control for the refusal below — naming a tenant works, so
    ambiguity is not being resolved by breaking multi-tenant users."""
    a = await _resolve(E_BOTH, tenant_id=TA)
    b = await _resolve(E_BOTH, tenant_id=TB)
    assert a.tenant_id == TA and b.tenant_id == TB
    assert a.roles == frozenset({"primary_owner"})
    assert b.roles == frozenset({"member"})


async def test_an_unnamed_tenant_with_several_memberships_refuses(cloud):
    """**It must not choose.** Picking the first, the oldest or the
    lowest-sorting membership is how a request lands in a tenant the user did
    not mean — the same reason `select_workspace` refuses on more than one."""
    from core.identity.resolver import AmbiguousTenant

    with pytest.raises(AmbiguousTenant):
        await _resolve(E_BOTH)


async def test_ambiguity_is_a_subclass_so_narrow_handlers_still_fail_closed(cloud):
    from core.identity.resolver import AmbiguousTenant, CloudIdentityNotAvailable

    assert issubclass(AmbiguousTenant, CloudIdentityNotAvailable)


# ─────────────────────────────────────────────────────────────────────────────
# A tenant owner is not the deployment owner
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_tenant_primary_owner_is_not_the_deployment_owner(cloud):
    """3C-04, 3C-08 and P2-07 were each a version of this confusion."""
    p = await _resolve(E_BOTH, tenant_id=TA)
    assert p.roles == frozenset({"primary_owner"})
    assert p.is_owner is False, (
        "a tenant's primary owner resolved with is_owner=True. That is the "
        "deployment owner's flag — it gates the owner-only routes and the "
        "security-admin surface, and no customer may hold it.")


async def test_a_tenant_owner_never_receives_the_deployment_owner_scopes(cloud):
    """The subtler half, and the reason cloud has its own role map.

    `security.roles.scopes_for("primary_owner")` returns every scope, because in
    the private edition that name means the person who owns the box. The name
    also exists in the cloud vocabulary, so reusing that function would have
    handed a customer `security_admin` and `private_memory_read` — and it would
    have looked right at the call site.
    """
    from security.roles import SCOPE_SECURITY_ADMIN, scopes_for

    p = await _resolve(E_BOTH, tenant_id=TA)
    assert SCOPE_SECURITY_ADMIN not in p.scopes
    assert "private_memory_read" not in p.scopes
    assert p.scopes != scopes_for("primary_owner"), (
        "the cloud primary_owner scope set is identical to the private "
        "edition's. Cloud must not reuse security.roles.scopes_for.")


async def test_every_database_role_is_mapped(cloud):
    """The map is checked against the DATABASE's vocabulary, not its own.

    An unmapped role fails closed to no scopes, which in production reads as
    "this role is broken" rather than "this role was never mapped". The CHECK
    constraint is the authority for what a role may be.
    """
    from core.identity.cloud_roles import CLOUD_ROLE_SCOPES, CLOUD_TENANT_ROLES

    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        definition = await admin.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='public.tenant_memberships'::regclass "
            "AND conname='tenant_memberships_role_check'")
    finally:
        await admin.close()

    in_db = {r for r in CLOUD_TENANT_ROLES if f"'{r}'" in definition}
    assert in_db == CLOUD_TENANT_ROLES, (
        f"CLOUD_TENANT_ROLES and the database disagree. The constraint is "
        f"{definition!r}")
    assert set(CLOUD_ROLE_SCOPES) == CLOUD_TENANT_ROLES, (
        f"roles with no scope mapping: "
        f"{sorted(CLOUD_TENANT_ROLES - set(CLOUD_ROLE_SCOPES))}")


# ─────────────────────────────────────────────────────────────────────────────
# Fail closed on every account state that is not "active and verified"
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("email,why", [
    (E_NONE, "holds no membership that grants access"),
    (E_UNVERIFIED, "email is not verified"),
    (E_SUSPENDED, "account is suspended"),
    (E_DELETED, "account is deleted"),
    ("nobody@p2cloud.example", "no such account"),
    ("", "empty subject"),
    ("   ", "whitespace subject"),
])
async def test_unresolvable_identities_are_refused(cloud, email, why):
    from core.identity.resolver import CloudIdentityNotAvailable

    with pytest.raises(CloudIdentityNotAvailable):
        await _resolve(email)


async def test_a_revoked_membership_stops_granting_access(cloud):
    """`membership_grants_access` is GENERATED — the database computes it, so
    revocation cannot be missed by re-implementing the predicate here."""
    from core.identity.resolver import CloudIdentityNotAvailable

    assert (await _resolve(E_SOLO)).tenant_id == TA
    await cloud.execute(
        "UPDATE tenant_memberships SET status='removed', revoked_at=NOW() "
        "WHERE tenant_id=$1 AND user_id=$2", TA, U_SOLO)
    with pytest.raises(CloudIdentityNotAvailable):
        await _resolve(E_SOLO)


async def test_a_suspended_membership_stops_granting_access(cloud):
    """Suspension is not removal, and both have to bite."""
    from core.identity.resolver import CloudIdentityNotAvailable

    await cloud.execute(
        "UPDATE tenant_memberships SET status='suspended', suspended_at=NOW() "
        "WHERE tenant_id=$1 AND user_id=$2", TA, U_SOLO)
    with pytest.raises(CloudIdentityNotAvailable):
        await _resolve(E_SOLO)


async def test_an_invited_membership_does_not_yet_grant_access(cloud):
    """An invitation is not a membership. Phase 2's invitation flow will create
    rows in exactly this state, and they must not authenticate before acceptance."""
    from core.identity.resolver import CloudIdentityNotAvailable

    await cloud.execute(
        "UPDATE tenant_memberships SET status='invited', joined_at=NULL "
        "WHERE tenant_id=$1 AND user_id=$2", TA, U_SOLO)
    with pytest.raises(CloudIdentityNotAvailable):
        await _resolve(E_SOLO)


# ─────────────────────────────────────────────────────────────────────────────
# The private edition is unchanged
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_private_edition_still_resolves_the_owner(pool):
    """The regression this batch could most easily cause: cloud resolution is
    new, and the private path must not have moved underneath it."""
    from core.editions import EDITION_PRIVATE
    from core.identity.principal import PRIVATE_TENANT_ID
    from core.identity.resolver import resolve_principal

    p = await resolve_principal("admin", edition=EDITION_PRIVATE)
    assert p.tenant_id == PRIVATE_TENANT_ID
    assert p.edition == EDITION_PRIVATE


# ─────────────────────────────────────────────────────────────────────────────
# The bootstrap connection is a bootstrap, not an escape hatch
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_bootstrap_connection_cannot_read_tenant_data(cloud):
    """It binds a user and no tenant, so every tenant-scoped table matches zero
    rows under FORCE RLS. That is the property that makes it safe to use before
    a tenant is known, and it is asserted rather than assumed."""
    from core.identity.db_context import identity_bootstrap_connection

    async with identity_bootstrap_connection(U_SOLO) as conn:
        assert await conn.fetchval("SELECT count(*) FROM conversations") == 0
        assert await conn.fetchval("SELECT count(*) FROM organisations") == 0


async def test_the_bootstrap_connection_shows_only_this_users_memberships(cloud):
    """`tenant_memberships` policy is `tenant_id = app.tenant_id OR user_id =
    app.user_id`. With no tenant bound, only the user's own rows are visible —
    so the bootstrap cannot enumerate anyone else's memberships."""
    from core.identity.db_context import identity_bootstrap_connection

    async with identity_bootstrap_connection(U_SOLO) as conn:
        rows = await conn.fetch("SELECT user_id FROM tenant_memberships")
    assert rows, "the bootstrap saw none of its own memberships"
    assert {r["user_id"] for r in rows} == {U_SOLO}


async def test_the_bootstrap_connection_refuses_a_missing_user():
    """Binding neither a tenant nor a user leaves a connection matching nothing
    anywhere — a silent no-op rather than an error, which is the failure mode
    this whole phase keeps finding."""
    from core.identity.db_context import (
        MissingTenantContext,
        identity_bootstrap_connection,
    )

    with pytest.raises(MissingTenantContext):
        async with identity_bootstrap_connection(None):
            pass
