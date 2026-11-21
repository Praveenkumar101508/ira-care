"""P12-06 finding L — `owner_profile` is one row for the whole deployment.

The Phase 12 truth pass exists to re-check a claim that was already found false
once. The 3C-10 debt list records, as its mitigation for eight unscoped tables:

    "reachable only through an owner-gated route, and `is_owner` refuses in
     cloud (3C-04), so none is reachable by a second tenant today"

P12-03 proved that false for `briefings`. This file proves it false for
`owner_profile`, and the shape here is worse than a disclosure.

── What the table is ────────────────────────────────────────────────────────

A SINGLETON. `ira/owner_profile.py` writes
`INSERT INTO owner_profile (id) VALUES (TRUE) ON CONFLICT DO NOTHING` and
`UPDATE owner_profile SET … WHERE id IS TRUE`. One row, for the entire
deployment. Asked of the catalogue rather than of the migration:

    tenant_id column      0
    RLS enabled           f
    FORCE RLS             f
    policies              0

── What reaches it ──────────────────────────────────────────────────────────

`GET /api/v1/profile` and `PUT /api/v1/profile`, both `Depends(require_auth)`
and nothing else. The router is mounted in `app/routers.py` with no
dependencies, and `is_owner` appears nowhere on the path — not in the route, not
in `owner_profile.py`.

So the mitigation is not weakened here; it is absent. `require_auth` proves
somebody is signed in, and nothing more.

── Why the PUT is the serious half ──────────────────────────────────────────

`owner_profile.py`'s own docstring: *"The profile is small business data
(Postgres) injected into every chat turn"*. A read across tenants is a
disclosure. A WRITE across tenants puts one tenant's text into every other
tenant's model prompts — the same class as `briefings`, in the opposite
direction, and reachable by any authenticated account.
"""
from __future__ import annotations

import os

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


@pytest.fixture
async def pool():
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=3)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


@pytest.fixture
async def admin():
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute("DELETE FROM owner_profile")
        yield conn
    finally:
        await conn.execute("DELETE FROM owner_profile")
        await conn.close()


@pytest.fixture
def client_for(pool):
    """A client authenticated as one named username, and nothing else.

    `require_auth` is what these routes actually depend on, and it yields a
    USERNAME STRING — there is no principal, no tenant and no workspace anywhere
    on this path. Overriding it is therefore not a simplification of the real
    identity chain; it IS the real identity chain.
    """
    from httpx import ASGITransport, AsyncClient

    from api.middleware.auth import require_auth
    from app.factory import create_app

    def build(username):
        app = create_app()
        app.dependency_overrides[require_auth] = lambda: username
        return AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://p1206.invalid")

    return build


#: FINDING L is CLOSED by `require_owner` on both routes, and the marker that
#: recorded it while it was open is gone — which is how it was designed to end.
#:
#: These two tests were `xfail(strict=True)` in the commit that found the defect.
#: Strict means an xfail that PASSES fails the suite, so applying the gate turned
#: this file red and forced the record to be updated in the same change. That is
#: the whole reason for marking a finding rather than deleting the test.
# ── The boundary that is not there ──────────────────────────────────────────


async def test_the_table_has_no_boundary_at_all(admin):
    """Asked of the catalogue, so the finding does not rest on reading a file."""
    row = await admin.fetchrow(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        " WHERE relname = 'owner_profile'")
    tenant_columns = await admin.fetchval(
        "SELECT count(*) FROM information_schema.columns "
        " WHERE table_name = 'owner_profile' AND column_name = 'tenant_id'")
    policies = await admin.fetchval(
        "SELECT count(*) FROM pg_policies WHERE tablename = 'owner_profile'")

    assert (row["relrowsecurity"], row["relforcerowsecurity"],
            tenant_columns, policies) == (False, False, 0, 0), (
        "owner_profile has gained a boundary. If that is deliberate, this test "
        "and the 3C-10 debt list both need updating — the finding it records "
        "would no longer be true."
    )


async def test_a_second_account_cannot_reach_the_profile_at_all(pool, admin,
                                                                client_for):
    """FINDING L, closed. `require_owner` refuses a non-owner — and refuses
    EVERYONE in cloud, because `is_owner` returns False there unconditionally.

    The table is still a singleton with no boundary; that is deliberate and is
    `TENANCY_COMPLETION_MIGRATION_PLAN.md` §2's batch-10 decomposition to
    resolve. What changed is that the 3C-10 debt list's recorded mitigation —
    *"reachable only through an owner-gated route"* — is now true instead of
    merely written down."""
    # Seeded as a superuser, so the test does not depend on the write path it
    # is about to prove is closed.
    await admin.execute(
        "INSERT INTO owner_profile (id, name, goals) VALUES (TRUE, $1, $2) "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, "
        "                               goals = EXCLUDED.goals",
        "ALICE-PRIVATE", "ALICE-CONFIDENTIAL-GOALS")

    async with client_for("mallory") as mallory:
        got = await mallory.get("/api/v1/profile")

    assert got.status_code == 403, (
        f"GET /api/v1/profile answered {got.status_code} to a non-owner. The "
        f"table is a deployment-wide singleton, so a 200 here is another "
        f"account's data."
    )
    assert "ALICE-CONFIDENTIAL-GOALS" not in got.text


async def test_a_second_account_cannot_overwrite_the_shared_profile(pool, admin,
                                                                    client_for):
    """The serious half.

    `owner_profile.py` says the profile is *"injected into every chat turn"*, so
    a cross-account WRITE puts one person's text into another's model prompts.
    That is not a disclosure; it is content injection with an audience.
    """
    await admin.execute(
        "INSERT INTO owner_profile (id, name) VALUES (TRUE, 'ALICE') "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name")

    async with client_for("mallory") as mallory:
        put = await mallory.put("/api/v1/profile",
                                json={"name": "MALLORY-OVERWROTE-THIS"})

    assert put.status_code == 403, (
        f"PUT /api/v1/profile answered {put.status_code} to a non-owner")
    assert await admin.fetchval("SELECT name FROM owner_profile") == "ALICE", (
        "a non-owner overwrote the profile — and this record is injected into "
        "every chat turn, so the write reaches other accounts' model prompts."
    )


async def test_it_is_still_a_singleton_and_that_is_deliberate(admin):
    """The gate closed the ACCESS. The shape underneath is unchanged.

    `owner_profile` is keyed `id IS TRUE` — one row for the whole deployment —
    and it still is. That is not an oversight this batch left behind:
    `TENANCY_COMPLETION_MIGRATION_PLAN.md` §2 decides the table is to be
    DECOMPOSED into `user_profiles` (USER_PRIVATE) + `tenant_settings`
    (TENANT_SCOPED) + `organisation_profiles`, marked *"Not migrated in Phase 1"*
    and scheduled for batch 10.

    Bolting a `tenant_id` onto the singleton would build the thing that plan
    says to replace. So this test pins the state rather than the fix, and it
    fails the day somebody changes the shape — at which point the batch-10 work
    has started and this file needs rewriting with it.
    """
    await admin.execute(
        "INSERT INTO owner_profile (id, name) VALUES (TRUE, 'ONE') "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name")

    assert await admin.fetchval("SELECT count(*) FROM owner_profile") == 1
    with pytest.raises(asyncpg.PostgresError):
        # There is no second row to have. `id` is a BOOLEAN primary key pinned
        # TRUE, so a second tenant's profile is not merely unscoped — it is
        # unrepresentable.
        await admin.execute(
            "INSERT INTO owner_profile (id, name) VALUES (TRUE, 'TWO')")


# ── FINDING M — a bound read over a table with nothing to bind to ───────────


#: FINDING M is CLOSED, and it was the INVERSE of this phase's recurring shape.
#:
#: Every other finding in Phase 12 was an unbound read matching NOTHING. This is
#: a correctly-bound read matching EVERYTHING: `worker/briefing.py`'s
#: `_gather_security_data` opens `workspace_connection(principal)` — which looks
#: right in review, and is what the P12-01 audit was built to check for — and
#: then queries `security_events`, which has no tenant column, no RLS and no
#: policies. The binding constrains nothing because there is no predicate to
#: constrain.
#:
#: So a grep for `workspace_connection` says this caller is bound, the P12-01
#: caller audit would classify it bound, and it still reads every tenant's rows.
#: That is the third distinct lesson of this phase: binding a connection is only
#: meaningful against a table that HAS a boundary.
#: Closed by WITHHOLDING rather than by a predicate, because `security_events`
#: has no column to filter on and giving it one is a recorded OPEN decision
#: (`TENANCY_COMPLETION_MIGRATION_PLAN.md` §18 item 3). See
#: `_gather_security_data`'s docstring.


async def test_a_cloud_briefing_does_not_carry_another_tenants_security_events(
        pool, admin, monkeypatch):
    """Seed an event belonging to nobody, and prove a cloud briefing withholds it.

    The event genuinely belongs to no tenant — `security_events` has no column
    to put one in — which is exactly why the read cannot be scoped and has to be
    refused instead.
    """
    import uuid as _uuid

    import core.editions as editions
    from core.editions import EDITION_CLOUD
    from core.identity.principal import Principal
    from worker.briefing import _gather_security_data

    monkeypatch.setattr(editions, "current_edition", lambda: EDITION_CLOUD,
                        raising=False)

    slug = f"p1206-{_uuid.uuid4().hex[:8]}"
    tenant_id = await admin.fetchval(
        "INSERT INTO tenants (name, slug, edition) VALUES ($1, $1, 'cloud') "
        "RETURNING id", slug)
    org_id = await admin.fetchval(
        "INSERT INTO organisations (tenant_id, name) VALUES ($1, 'P12-06') "
        "RETURNING id", tenant_id)
    workspace_id = await admin.fetchval(
        "INSERT INTO workspaces (tenant_id, organisation_id, name, slug, "
        "                        is_default, status) "
        "VALUES ($1, $2, 'Default', 'default', true, 'active') RETURNING id",
        tenant_id, org_id)
    user_id = await admin.fetchval(
        "INSERT INTO users (email, display_name, status) "
        "VALUES ($1, $2, 'active') RETURNING id",
        f"{slug}@example.invalid", slug)

    # Somebody ELSE's security event. It belongs to no tenant, because the
    # table has no column to put one in — which is the finding.
    await admin.execute(
        "INSERT INTO security_events (severity, event_type, description, resolved) "
        "VALUES ('critical', 'p1206_probe', $1, FALSE)",
        "OTHER-TENANT-SECRET-HOSTNAME")

    try:
        principal = Principal(
            user_id=user_id, tenant_id=tenant_id, session_id=_uuid.uuid4(),
            edition=EDITION_CLOUD, roles=frozenset({"owner"}),
            scopes=frozenset(), workspace_id=workspace_id)
        data = await _gather_security_data(principal)

        descriptions = [t["desc"] for t in data["recent_threats"]]
        assert "OTHER-TENANT-SECRET-HOSTNAME" not in descriptions, (
            f"this workspace's briefing carries another tenant's security "
            f"event text. critical_unresolved={data['critical_unresolved']}, "
            f"threats={descriptions}"
        )
        # Withheld, and SAYING so. A section that quietly returned zero would
        # read as "no threats" — the false-clean shape, in the surface P12-03
        # just gave a boundary to.
        assert data["critical_unresolved"] is None, (
            "the count is 0 rather than None, which asserts 'no critical "
            "threats' about data this build declined to read"
        )
        assert "unavailable" in data and "not tenant-scoped" in data["unavailable"]
    finally:
        await admin.execute(
            "DELETE FROM security_events WHERE event_type = 'p1206_probe'")
        await admin.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
        await admin.execute("DELETE FROM users WHERE id = $1", user_id)
