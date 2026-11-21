"""P10-02 — the ledger API answers four questions, not one.

`GET /api/v1/trust/ledger` gated on `is_owner`, which is `False` in the cloud
edition whatever the username is (3C-04). So the endpoint was not "owner-only"
in cloud — it was **unreachable**, which is a different thing that looked the
same from the code. And underneath it ran its **own** `acquire()` query rather
than calling the ledger module, so P10-01's binding did not reach it: the one
surface a customer would use to inspect their audit trail was the one still
reading unbound.

── Owner decision B, implemented ──────────────────────────────────────────

A tenant administrator may read **their own** tenant's ledger. A cloud customer
with an audit trail nobody on their side can inspect has one in name only.

Authority is the verified, server-derived principal and the role→scope map —
never a client-supplied tenant. `primary_owner`, `owner`, `admin` and the
read-only `security_admin` hold `trust_ledger_read`; `member`, `viewer` and
`billing_admin` do not, because an audit ledger is tenant-wide and every member
reading every colleague's actions is a different product.

`security_admin` gaining its first real capability here is deliberate and is
what `cloud_roles.py`'s own header predicted: *"their elevated capabilities
arrive with the surfaces they administer."* The tenant Trust Ledger is the first
such surface. It gets the ledger scope ONLY — auditing is not approving actions
or writing settings.

── The four answers ───────────────────────────────────────────────────────

    status: OK, records: []      this tenant really has no records
    403                          not authorised — raised, never a body
    NO_TENANT_CONTEXT            our wiring failed; never an empty list
    LEDGER_UNAVAILABLE           the database could not be reached

The old response had `available: true/false` and free text. `available: false`
covered the last three and `available: true` with `[]` covered the first two —
so a client rendering "no records" was rendering a clean audit trail that had
never been consulted.
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

import api.routes.trust as trust_routes  # noqa: E402
from core.identity.cloud_roles import cloud_scopes_for  # noqa: E402
from core.identity.principal import Principal  # noqa: E402
from security.roles import SCOPE_TRUST_LEDGER_READ  # noqa: E402
from trust.ledger import append_record  # noqa: E402


@pytest.fixture(autouse=True)
async def app_pool():
    import utils.db as db

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db._pool = db._pool, pool
    yield
    db._pool = previous
    await pool.close()


async def _tenant() -> uuid.UUID:
    tenant = uuid.uuid4()
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute("INSERT INTO tenants (id, name, slug) VALUES ($1,$2,$2)",
                           tenant, f"p1002-{tenant.hex[:10]}")
    finally:
        await conn.close()
    return tenant


def _principal(tenant: uuid.UUID, role: str = "admin") -> Principal:
    """A principal exactly as `resolve_principal` would build one.

    Scopes come from `cloud_scopes_for(role)` rather than being written out
    here: a test that hand-picked its scopes would pass while the real role map
    said something else, which is the shape of every "the code is fine, the
    wiring is not" defect this phase has found.
    """
    return Principal(
        user_id=uuid.uuid4(), tenant_id=tenant, session_id=uuid.uuid4(),
        edition="cloud", roles=frozenset({role}), scopes=cloud_scopes_for(role),
        username=f"{role}@example.test", is_owner=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Who may read it
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["primary_owner", "owner", "admin",
                                  "security_admin"])
async def test_an_administrator_may_read_their_own_tenants_ledger(role):
    tenant = await _tenant()
    await append_record("action", user_id="u", action_id="visible",
                        principal=_principal(tenant))

    out = await trust_routes.trust_ledger(principal=_principal(tenant, role))
    assert out["available"] is True
    assert out["status"] == trust_routes.LEDGER_OK
    assert out["tenant_id"] == str(tenant)
    assert any(r["action_id"] == "visible" for r in out["records"])


@pytest.mark.parametrize("role", ["member", "viewer", "billing_admin"])
async def test_an_ordinary_role_is_refused(role):
    """Not an empty ledger — a 403. An audit ledger is tenant-wide."""
    tenant = await _tenant()
    with pytest.raises(HTTPException) as exc:
        await trust_routes.trust_ledger(principal=_principal(tenant, role))
    assert exc.value.status_code == 403


async def test_the_role_map_is_the_authority_not_this_test():
    """If `cloud_roles` and this file ever disagree, the map wins and this
    fails. A test that asserted its own idea of the roles would keep passing
    through a change to the real one."""
    for role in ("primary_owner", "owner", "admin", "security_admin"):
        assert SCOPE_TRUST_LEDGER_READ in cloud_scopes_for(role), role
    for role in ("member", "viewer", "billing_admin"):
        assert SCOPE_TRUST_LEDGER_READ not in cloud_scopes_for(role), role


async def test_security_admin_gets_the_ledger_and_nothing_else_new():
    """The read-only audit role. Auditing is not approving actions or writing
    settings, and widening it here would be inventing a capability rather than
    granting the one the owner decided on."""
    from security.roles import SCOPE_ACTIONS_APPROVE, SCOPE_SETTINGS_WRITE

    scopes = cloud_scopes_for("security_admin")
    assert SCOPE_TRUST_LEDGER_READ in scopes
    assert SCOPE_ACTIONS_APPROVE not in scopes
    assert SCOPE_SETTINGS_WRITE not in scopes


# ─────────────────────────────────────────────────────────────────────────────
# 2. Whose records they are is decided by the server
# ─────────────────────────────────────────────────────────────────────────────


async def test_an_administrator_cannot_see_another_tenants_records():
    """The boundary underneath is RLS; the scope check is a legible early
    refusal on top of it, never instead of it."""
    a, b = await _tenant(), await _tenant()
    await append_record("action", user_id="u", action_id="tenant-a-secret",
                        principal=_principal(a))

    out = await trust_routes.trust_ledger(principal=_principal(b))
    assert out["available"] is True
    assert not any(r["action_id"] == "tenant-a-secret" for r in out["records"])


async def test_action_id_lookup_is_scoped_too():
    """The `?action_id=` branch takes a different code path from the listing,
    and a scope check on one of them is a scope check on neither."""
    a, b = await _tenant(), await _tenant()
    await append_record("action", user_id="u", action_id="scoped-lookup",
                        principal=_principal(a))

    assert (await trust_routes.trust_ledger(
        action_id="scoped-lookup", principal=_principal(a)))["count"] == 1
    assert (await trust_routes.trust_ledger(
        action_id="scoped-lookup", principal=_principal(b)))["count"] == 0


async def test_the_head_belongs_to_the_reader():
    a, b = await _tenant(), await _tenant()
    await append_record("action", user_id="u", action_id="head-check",
                        principal=_principal(a))

    out_a = await trust_routes.trust_ledger(principal=_principal(a))
    out_b = await trust_routes.trust_ledger(principal=_principal(b))
    assert out_a["head"]["tenant_id"] == str(a)
    assert out_b["head"]["tenant_id"] == str(b)
    assert out_a["head"]["hash"] != out_b["head"]["hash"]


# ─────────────────────────────────────────────────────────────────────────────
# 3. A real empty ledger, and the three things it is not
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_tenant_with_no_records_gets_a_successful_empty_answer():
    out = await trust_routes.trust_ledger(principal=_principal(await _tenant()))
    assert out["available"] is True
    assert out["status"] == trust_routes.LEDGER_OK
    assert out["records"] == [] and out["count"] == 0


async def test_a_real_chain_verifies_through_the_endpoint():
    """End to end: the write binds, the read binds, and what comes back through
    the API verifies. If the read ever stopped being tenant-scoped this reports
    a broken link rather than quietly returning more rows."""
    tenant = await _tenant()
    for i in range(4):
        await append_record("action", user_id="u", action_id=f"chain-{i}",
                            principal=_principal(tenant))
    out = await trust_routes.trust_ledger(principal=_principal(tenant))
    assert out["verified"] is True
    assert out["count"] == 4


async def test_the_four_statuses_are_distinct_strings():
    """They are compared by clients. Two of them collapsing to the same value
    would restore exactly the ambiguity this batch removed."""
    codes = {trust_routes.LEDGER_OK,
             trust_routes.LEDGER_NO_TENANT_CONTEXT,
             trust_routes.LEDGER_UNAVAILABLE}
    assert len(codes) == 3


# ─────────────────────────────────────────────────────────────────────────────
# 4. The query that survived P10-01 is gone
# ─────────────────────────────────────────────────────────────────────────────


def test_the_route_module_owns_no_ledger_query_of_its_own():
    """`api/routes/trust.py:164` ran its own `acquire()` against
    `trust_ledger`, which is why P10-01 binding the module did not reach the
    surface a customer actually uses. A second copy of a query is a second thing
    to fix, and it was missed once already.

    Read as structure, not prose: the assertion is on the SQL and the import,
    not on a comment saying they were removed.
    """
    import pathlib
    import re

    source = (pathlib.Path(trust_routes.__file__)).read_text(encoding="utf-8")
    assert not re.search(r"FROM\s+trust_ledger", source, re.IGNORECASE), (
        "api/routes/trust.py queries trust_ledger directly again — the module's "
        "binding does not reach a query written out in the route")

    # Deliberately NOT "this module may not import acquire". A first version of
    # this test said that and failed on `_voice_enrolled`, which reads
    # `voice_profiles` for `/trust/status` — a different table, a different
    # control, and not Phase 10's. Banning a whole import to enforce a rule
    # about one table is how a guard starts being edited around rather than
    # obeyed. (`voice_profiles` is USER_PRIVATE with its own RLS; an unbound
    # read there answers "unknown" rather than "not enrolled". Noted in the
    # Phase 10 plan as an observation, not silently folded into this batch.)
    ledger_lines = [ln for ln in source.splitlines()
                    if "trust_ledger" in ln and "acquire" in ln]
    assert ledger_lines == [], f"a bare acquire still reaches the ledger: {ledger_lines}"


def test_no_route_reaches_the_ledger_table_directly():
    """The general form, so the next surface to want a ledger listing has to
    come through the module rather than write its own SELECT."""
    import pathlib
    import re

    routes = pathlib.Path(trust_routes.__file__).parent
    offenders = [
        m.name for m in sorted(routes.glob("*.py"))
        if re.search(r"FROM\s+trust_ledger", m.read_text(encoding="utf-8"),
                     re.IGNORECASE)
    ]
    assert offenders == [], (
        f"{offenders} query trust_ledger directly rather than through "
        f"trust/ledger.py, so the tenant binding does not apply to them")
