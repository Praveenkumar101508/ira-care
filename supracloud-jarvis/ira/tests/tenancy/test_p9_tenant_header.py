"""P9-03 — a caller with several tenants can say which one, and only that one.

Finding F9-5. `resolve_principal` refuses when a user holds more than one
access-granting membership and names no tenant, and it is right to: picking the
first, the oldest, or whichever sorts first is how a request lands in a tenant
the user did not mean. But nothing gave the caller a way to name one. The
governance, connectors, realtime, models, companion and privacy surfaces each
carried a private copy of

    async def _principal(username):
        try:    return await resolve_principal(username)
        except CloudIdentityNotAvailable: raise HTTPException(503, ...)

— a plain function, with nowhere for a header to arrive. So the surfaces were
unreachable for exactly the users a multi-tenant product exists for, and the
refusal was reported as **503 Service Unavailable**: wait for something that was
never going to change.

── What this file asserts, and why each one is here ───────────────────────

**Reachability.** With `X-Tenant-ID` naming one of the caller's tenants, the
principal resolves into that tenant and no other. Without the header and with
several memberships, 400 — naming the header, because a refusal that does not
say what to send gets worked around rather than fixed.

**That naming is asking, not telling.** The header is verified against
`tenant_memberships` before it is honoured. A tenant the caller is not in, a
tenant that does not exist, and a well-formed invention are all refused with the
SAME 403 and the same message, so the endpoint cannot be walked to discover
which tenants exist. This is the security half of the batch and it is where the
plants aim.

**That the status split did not become an oracle.** `_resolve_cloud_principal`
funnels a database outage into the same exception type as a refusal on purpose —
*"'I could not tell' must never be a different code path from 'no'"* — so only
the two cases the resolver ALREADY distinguishes get their own status. An
unreachable register is still 503, and
`test_an_unreachable_register_is_still_503` is what stops that being quietly
widened later.

**That the single-membership path did not change.** One membership and no header
still resolves, exactly as before. A fix for multi-tenant users that made
single-tenant users start sending a header would be a migration, not a fix.

Running them: `IRA_TEST_DATABASE_DSN` (the `ira_app` role) and
`IRA_TEST_ADMIN_DSN`, as everywhere else in this directory.
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

from fastapi import HTTPException  # noqa: E402

from api.middleware.workspace import (  # noqa: E402
    TENANT_HEADER,
    principal_from_username,
    require_tenant_principal,
)
from core.identity.resolver import (  # noqa: E402
    AmbiguousTenant,
    CloudIdentityNotAvailable,
    TenantNotPermitted,
    resolve_principal,
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
def cloud_edition(monkeypatch):
    """Every property here is a cloud property. Private has one tenant and
    nothing to name."""
    monkeypatch.setenv("IRA_EDITION", "cloud")


class World:
    pass


async def _make_user(conn, *, tenants: int) -> World:
    w = World()
    w.user = uuid.uuid4()
    w.email = f"{w.user.hex[:12]}@example.test"
    w.tenants = []
    await conn.execute(
        "INSERT INTO users (id, email, email_verified_at, status) "
        "VALUES ($1, $2, NOW(), 'active')", w.user, w.email)
    for _ in range(tenants):
        tenant = uuid.uuid4()
        await conn.execute("INSERT INTO tenants (id, name, slug) VALUES ($1,$2,$2)",
                           tenant, f"p903-{tenant.hex[:10]}")
        await conn.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role, status) "
            "VALUES ($1, $2, 'member', 'active')", tenant, w.user)
        w.tenants.append(tenant)
    return w


@pytest.fixture
async def admin():
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        yield conn
    finally:
        await conn.close()


async def _remove(conn, w: World) -> None:
    """Child-first teardown.

    `tests/tenancy/test_zz_no_fixture_residue.py` fails the whole directory when
    a fixture leaves a tenant holding memberships, because
    `rollback/035_memberships_down.sql` then refuses later in the same CI job and
    names 035 rather than the fixture that caused it. It caught this file on its
    first run.
    """
    await conn.execute("DELETE FROM tenant_memberships WHERE user_id = $1", w.user)
    await conn.execute("DELETE FROM users WHERE id = $1", w.user)
    for tenant in w.tenants:
        await conn.execute("DELETE FROM tenants WHERE id = $1", tenant)


@pytest.fixture
async def multi(admin) -> World:
    """A user in three tenants — the shape that was unreachable."""
    world = await _make_user(admin, tenants=3)
    yield world
    await _remove(admin, world)


@pytest.fixture
async def solo(admin) -> World:
    world = await _make_user(admin, tenants=1)
    yield world
    await _remove(admin, world)


@pytest.fixture
async def stranger(admin) -> World:
    """Somebody else's tenant, which `multi` must not be able to reach."""
    world = await _make_user(admin, tenants=1)
    yield world
    await _remove(admin, world)


# ─────────────────────────────────────────────────────────────────────────────
# 1. The surface is reachable, and lands in the tenant that was named
# ─────────────────────────────────────────────────────────────────────────────


async def test_naming_a_tenant_resolves_into_that_tenant(multi):
    for tenant in multi.tenants:
        principal = await principal_from_username(multi.email, str(tenant))
        assert principal.tenant_id == tenant


async def test_without_the_header_several_memberships_refuse_with_400(multi):
    """400, not 503. The client can fix this; a server-error status told the
    user to wait for something that was never going to happen."""
    with pytest.raises(HTTPException) as exc:
        await principal_from_username(multi.email)
    assert exc.value.status_code == 400


async def test_the_400_names_the_header_to_send(multi):
    """A refusal that does not say what to send gets worked around, and the
    workaround is somebody choosing a tenant in application code."""
    with pytest.raises(HTTPException) as exc:
        await principal_from_username(multi.email)
    assert TENANT_HEADER in str(exc.value.detail)


async def test_one_membership_still_needs_no_header(solo):
    """The single-tenant path is untouched. A fix for multi-tenant users that
    made single-tenant users start sending a header would be a migration."""
    principal = await principal_from_username(solo.email)
    assert principal.tenant_id == solo.tenants[0]


async def test_one_membership_may_still_name_it(solo):
    principal = await principal_from_username(solo.email, str(solo.tenants[0]))
    assert principal.tenant_id == solo.tenants[0]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Naming is asking. Membership decides.
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_tenant_the_caller_is_not_in_is_refused(multi, stranger):
    """The forged-header case, and the one that matters most."""
    with pytest.raises(HTTPException) as exc:
        await principal_from_username(multi.email, str(stranger.tenants[0]))
    assert exc.value.status_code == 403


async def test_a_tenant_that_does_not_exist_is_refused_identically(multi,
                                                                  stranger):
    """Same status AND same body as a real tenant the caller cannot reach.

    A different answer for "exists but not yours" and "no such tenant" turns
    this endpoint into a directory of the deployment's customers.
    """
    invented = uuid.uuid4()
    with pytest.raises(HTTPException) as real:
        await principal_from_username(multi.email, str(stranger.tenants[0]))
    with pytest.raises(HTTPException) as fake:
        await principal_from_username(multi.email, str(invented))
    assert real.value.status_code == fake.value.status_code == 403
    assert str(real.value.detail) == str(fake.value.detail)


async def test_a_revoked_membership_stops_working(multi, admin):
    """`membership_grants_access` is a GENERATED column, so revocation is the
    database's answer rather than a re-implementation of it here.

    `status` and `revoked_at` are set together because
    `tenant_memberships_status_revoked_agree` makes `status = 'removed'` and a
    stamped `revoked_at` the same fact, refusing a half-recorded
    revocation — a stamped timestamp with an `active` status, or the reverse,
    cannot exist. Written out rather than worked around: the constraint is part
    of why the generated column can be trusted.
    """
    revoked = multi.tenants[0]
    await admin.execute(
        "UPDATE tenant_memberships SET status = 'removed', revoked_at = NOW() "
        "WHERE user_id = $1 AND tenant_id = $2", multi.user, revoked)
    with pytest.raises(HTTPException) as exc:
        await principal_from_username(multi.email, str(revoked))
    assert exc.value.status_code == 403
    # ...and the other two still work, so this refused the membership rather
    # than the user.
    still = await principal_from_username(multi.email, str(multi.tenants[1]))
    assert still.tenant_id == multi.tenants[1]


async def test_a_suspended_membership_stops_working(multi, admin):
    suspended = multi.tenants[2]
    await admin.execute(
        "UPDATE tenant_memberships SET status = 'suspended' "
        "WHERE user_id = $1 AND tenant_id = $2", multi.user, suspended)
    with pytest.raises(HTTPException) as exc:
        await principal_from_username(multi.email, str(suspended))
    assert exc.value.status_code == 403


async def test_the_header_cannot_carry_a_role_or_a_scope(multi):
    """It selects a membership; it does not describe one. The role and scopes
    come from the membership row, and `is_owner` is hard-false — a tenant's
    primary owner is not the deployment owner (3C-04, 3C-08, P2-07)."""
    principal = await principal_from_username(multi.email, str(multi.tenants[0]))
    assert principal.is_owner is False
    assert principal.roles == frozenset({"member"})


async def test_a_malformed_header_is_400_not_a_silent_miss(multi):
    """A string that is not a UUID must not reach the membership comparison.

    `tenant_memberships.tenant_id` comes back from asyncpg as a `uuid.UUID`, so
    an unparsed string compares unequal to every membership — a legitimate user
    told they do not belong to their own tenant, with nothing in the log saying
    why.
    """
    with pytest.raises(HTTPException) as exc:
        await principal_from_username(multi.email, "not-a-uuid")
    assert exc.value.status_code == 400
    assert TENANT_HEADER in str(exc.value.detail)


async def test_a_blank_header_is_treated_as_absent(solo):
    """Clients send empty headers. Blank must mean "did not name one" rather
    than "named the empty tenant", which would refuse a single-tenant user."""
    principal = await principal_from_username(solo.email, "   ")
    assert principal.tenant_id == solo.tenants[0]


# ─────────────────────────────────────────────────────────────────────────────
# 3. The status split did not become an oracle
# ─────────────────────────────────────────────────────────────────────────────


async def test_an_unreachable_register_is_still_503(monkeypatch, multi):
    """The one that stops the split being widened later.

    `_resolve_cloud_principal` deliberately raises the BASE class when it cannot
    reach the identity register, so "I could not tell" is the same code path as
    "no". If a future change gave outages their own subclass to get a nicer
    status, an attacker could tell a real refusal from a broken database.
    """
    async def boom(*_a, **_kw):
        raise CloudIdentityNotAvailable("the identity register could not be reached")

    monkeypatch.setattr("api.middleware.workspace.resolve_principal", boom)
    with pytest.raises(HTTPException) as exc:
        await principal_from_username(multi.email, str(multi.tenants[0]))
    assert exc.value.status_code == 503


async def test_no_membership_at_all_is_503_not_403(admin):
    """Deliberately NOT 403. A user with no memberships is indistinguishable
    from an unknown user, and answering 403 for one and 503 for the other would
    confirm that an account exists."""
    world = await _make_user(admin, tenants=0)
    try:
        with pytest.raises(HTTPException) as exc:
            await principal_from_username(world.email)
        assert exc.value.status_code == 503
    finally:
        await _remove(admin, world)


async def test_the_two_narrow_refusals_are_still_subclasses():
    """Both stay under `CloudIdentityNotAvailable` so a caller that already
    fails closed on an unresolvable identity keeps failing closed here, rather
    than letting one through a narrower `except` it was never updated for."""
    assert issubclass(AmbiguousTenant, CloudIdentityNotAvailable)
    assert issubclass(TenantNotPermitted, CloudIdentityNotAvailable)


async def test_the_resolver_still_refuses_to_choose(multi):
    """Underneath the HTTP layer, the refusal itself is unchanged. If this ever
    starts returning a principal, every test above passes while the server picks
    a tenant on the user's behalf."""
    with pytest.raises(AmbiguousTenant):
        await resolve_principal(multi.email, edition="cloud")


# ─────────────────────────────────────────────────────────────────────────────
# 4. The routes actually went through the dependency
# ─────────────────────────────────────────────────────────────────────────────


def test_no_route_module_still_resolves_its_own_principal():
    """The six private copies are gone, not shadowed.

    A module that kept its own `resolve_principal(username)` helper would look
    fixed — the dependency exists and is imported — while that module's routes
    still had nowhere for the header to arrive. Read as structure, not prose:
    the assertion is on the call, not on a comment saying it was removed.
    """
    import pathlib
    import re

    routes = pathlib.Path(__file__).resolve().parents[2] / "api" / "routes"
    offenders = []
    for module in sorted(routes.glob("*.py")):
        text = module.read_text(encoding="utf-8")
        if re.search(r"await resolve_principal\(", text):
            offenders.append(module.name)
    assert offenders == [], (
        f"{offenders} resolve a principal themselves instead of receiving one "
        f"through require_tenant_principal / require_workspace_principal, so a "
        f"caller has no way to name a tenant on those routes")


def test_the_tenant_dependency_reads_the_header_it_documents():
    """The dependency's parameter must actually be bound to `X-Tenant-ID`.

    An alias typo, or a plain `str` default instead of a `Header`, produces a
    dependency that always sees None: every multi-tenant caller keeps getting
    400 no matter what they send, and every test that calls
    `principal_from_username` directly still passes.
    """
    import inspect

    from fastapi.params import Header as HeaderParam

    param = inspect.signature(require_tenant_principal).parameters["x_tenant_id"]
    assert isinstance(param.default, HeaderParam)
    assert param.default.alias == TENANT_HEADER


def test_the_workspace_dependency_carries_the_tenant_header_too():
    """A workspace is verified against the principal's tenant, so a caller with
    several memberships was refused before the workspace header was ever read.
    Fixing only the tenant-scoped surfaces would have left the workspace ones
    unreachable for the same users."""
    import inspect

    from fastapi.params import Header as HeaderParam

    from api.middleware.workspace import require_workspace_principal

    param = inspect.signature(require_workspace_principal).parameters["x_tenant_id"]
    assert isinstance(param.default, HeaderParam)
    assert param.default.alias == TENANT_HEADER
