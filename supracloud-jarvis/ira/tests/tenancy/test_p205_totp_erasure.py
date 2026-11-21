"""P2-05 — account erasure removes the user's second factor.

Giving `totp_secrets` a tenant in `052` made it tenant-scoped, and
`test_erasure_coverage.py` immediately said so: a tenant-scoped table that
erasure neither deletes, redacts, shreds nor exempts is a table a deletion
request silently leaves behind.

It is **deleted**, not redacted. A redaction preserves a record for somebody
else's accountability; a TOTP seed protects nobody but its owner, and once that
owner is gone it is a live credential for an account that should not exist. It
has no retention purpose and is a liability for as long as it is kept.

The deletion needs no special handling in `erase_postgres` because that function
runs inside `tenant_connection`, which binds `app.user_id` as well as
`app.tenant_id` — and 052's policies require both. A tenant-only connection
would match zero rows here and report a successful erasure that deleted nothing,
which is the failure mode this table specialises in; `test_erasure_deletes_only_the_erased_users_row`
is the assertion that would catch it.
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

from core.identity.principal import Principal  # noqa: E402

TA = uuid.UUID("11111111-1111-1111-1111-11111111e500")
TB = uuid.UUID("22222222-2222-2222-2222-22222222e500")
UA = uuid.UUID("aaaaaaaa-0000-0000-0000-0000000e5001")   # tenant A, erased
UA2 = uuid.UUID("aaaaaaaa-0000-0000-0000-0000000e5002")  # tenant A, kept
UB = uuid.UUID("bbbbbbbb-0000-0000-0000-0000000e5003")   # tenant B, kept

NAME = "p205-erase"

OA = uuid.UUID("cccccccc-0000-0000-0000-0000000e5001")
OB = uuid.UUID("cccccccc-0000-0000-0000-0000000e5002")
WA = uuid.UUID("dddddddd-0000-0000-0000-0000000e5001")
WB = uuid.UUID("dddddddd-0000-0000-0000-0000000e5002")


def _principal(tenant, user):
    return Principal(user_id=user, tenant_id=tenant, session_id=uuid.uuid4(),
                     edition="private", username=NAME, is_owner=False)


@pytest.fixture
async def seeded():
    """Three enrolments: the user to erase, a co-tenant, and another tenant."""
    import utils.db as db_module

    admin = await asyncpg.connect(ADMIN_DSN)
    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous = db_module._pool
    db_module._pool = created
    try:
        await admin.execute(
            # Distinct names AND slugs: `tenants.name` is unique too, so
            # reusing a name another fixture already took makes ON CONFLICT
            # swallow the insert and the FK below fails on a tenant that was
            # never created.
            "INSERT INTO tenants (id,name,slug) VALUES"
            " ($1,'P205 Erase A','p205-e-a'),($2,'P205 Erase B','p205-e-b')"
            " ON CONFLICT DO NOTHING", TA, TB)
        await admin.execute(
            "INSERT INTO users (id,display_name,auth_provider,status)"
            " VALUES ($1,'A1','local','active'),($2,'A2','local','active'),"
            "        ($3,'B1','local','active') ON CONFLICT DO NOTHING", UA, UA2, UB)
        # A workspace per tenant. `erase_postgres` runs one bound pass per
        # workspace and REFUSES when it cannot enumerate any — "refusing to
        # report a partial erasure as complete", which is correct behaviour and
        # a fixture gap rather than a finding.
        await admin.execute(
            "INSERT INTO organisations (id,tenant_id,name) VALUES ($1,$2,'P205 Org A'),"
            " ($3,$4,'P205 Org B') ON CONFLICT DO NOTHING", OA, TA, OB, TB)
        await admin.execute(
            "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default,status)"
            " VALUES ($1,$2,$3,'P205 WS A','p205-ws-a',TRUE,'active'),"
            "        ($4,$5,$6,'P205 WS B','p205-ws-b',TRUE,'active')"
            " ON CONFLICT DO NOTHING", WA, TA, OA, WB, TB, OB)
        await admin.execute("DELETE FROM totp_secrets WHERE username=$1", NAME)
        await admin.execute(
            "INSERT INTO totp_secrets (username,secret,enabled,user_id,tenant_id)"
            " VALUES ($1,'SEED-A1',TRUE,$2,$3),($1,'SEED-A2',TRUE,$4,$3),"
            "        ($1,'SEED-B1',TRUE,$5,$6)", NAME, UA, TA, UA2, UB, TB)
        yield admin
    finally:
        await admin.execute("DELETE FROM totp_secrets WHERE username=$1", NAME)
        await admin.close()
        db_module._pool = previous
        await created.close()


async def _remaining(admin) -> set[str]:
    rows = await admin.fetch(
        "SELECT secret FROM totp_secrets WHERE username=$1", NAME)
    return {r["secret"] for r in rows}


async def test_erasure_deletes_only_the_erased_users_row(seeded):
    """The obligation `test_erasure_coverage.py` exposed, and its blast radius.

    A tenant-only connection would delete nothing here and still report success,
    so the assertion is on what survives as much as on what goes.
    """
    from core.privacy.erasure import erase_postgres

    result = await erase_postgres(_principal(TA, UA), scope="account")
    # Deliberately NOT asserting `result.ok`. That flag also covers the
    # LangGraph checkpoint pass, which needs `sentence_transformers` — absent in
    # this environment, so `ok` is False for a reason that has nothing to do
    # with TOTP. Asserting it here would couple a credential-deletion test to an
    # unrelated optional dependency and make it fail for the wrong reason. What
    # matters is the row count and, below, exactly which rows survived.
    assert result.deleted >= 1, result

    left = await _remaining(seeded)
    assert "SEED-A1" not in left, "the erased user's second factor survived erasure"
    assert left == {"SEED-A2", "SEED-B1"}, (
        f"erasure reached beyond the erased user: {sorted(left)}")


async def test_a_co_tenants_second_factor_is_untouched(seeded):
    """Same tenant, different person. Tenant-level authority must not erase a
    member's credential — that is the USER_PRIVATE half of 052."""
    from core.privacy.erasure import erase_postgres

    await erase_postgres(_principal(TA, UA), scope="account")
    left = await _remaining(seeded)
    assert "SEED-A2" in left


async def test_another_tenants_second_factor_is_untouched(seeded):
    from core.privacy.erasure import erase_postgres

    await erase_postgres(_principal(TA, UA), scope="account")
    assert "SEED-B1" in await _remaining(seeded)


async def test_erasure_cannot_be_redirected_by_a_forged_identifier(seeded):
    """Authority comes from the Principal the server resolved, and RLS enforces
    it — a Principal naming tenant A cannot reach tenant B's row even though the
    caller chose both values here.

    This is the test-side equivalent of a request body carrying `tenant_id`: the
    identifiers are attacker-chosen, and the boundary holds anyway because the
    database, not the caller, decides what the connection can see.
    """
    from core.privacy.erasure import erase_postgres

    # A principal claiming tenant A but pointing at tenant B's user.
    await erase_postgres(_principal(TA, UB), scope="account")
    left = await _remaining(seeded)
    assert "SEED-B1" in left, "a mismatched principal erased another tenant's row"
    assert left == {"SEED-A1", "SEED-A2", "SEED-B1"}, (
        f"a mismatched principal changed something: {sorted(left)}")


async def test_erasure_reports_the_rows_it_actually_removed(seeded):
    """A deletion that cannot count what it deleted cannot be audited, and
    silently-zero is exactly what an unbound connection produces."""
    from core.privacy.erasure import erase_postgres

    result = await erase_postgres(_principal(TA, UA), scope="account")
    assert result.deleted >= 1, (
        "erasure reported deleting nothing at all; under RLS that is what a "
        "wrongly-bound connection looks like")


def test_totp_secrets_is_in_the_account_erasure_scope():
    """Static counterpart, so the coverage sweep and this file agree.

    Reads `SUBJECT_TABLES` directly. It used to walk the AST of
    `erase_postgres`, because the scope table was a local inside that function
    and there was no other way to reach it; P12-05 lifted it to module scope so
    `core/privacy/export.py` could share one definition.

    `tests/readiness/test_erasure_coverage.py` still parses the source rather
    than importing, and that difference is deliberate: it compares the scope
    against the LIVE SCHEMA, so a fixture that mutated the dict would make it
    agree with itself and with nothing else. This test asserts one membership,
    where that failure mode does not arise.
    """
    from core.privacy.erasure import SUBJECT_TABLES

    assert "totp_secrets" in SUBJECT_TABLES["account"], (
        "totp_secrets left the account erasure scope; a deletion "
        "request would leave a live second-factor credential behind")
