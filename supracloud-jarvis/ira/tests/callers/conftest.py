"""A real tenant, a real workspace, a real pool — so the callers can RUN.

── Why this directory exists ────────────────────────────────────────────────

P12-01 asked which callers of the twenty `workspace_id`-carrying tables bind a
workspace, and answered it by watching the tenancy, readiness and privacy
suites execute. That answered the question for twelve files of thirty-seven.
The other twenty-five never ran, and their absence from the unbound list was
read once — in the audit's own first draft — as evidence they were fine.

These tests exist to make those files execute. They are deliberately NOT
isolation tests: they assert almost nothing about their own subject. Their
product is the observation that `conftest_workspace_audit` records while they
run. An assertion here that a caller "works" would be a different claim from
the one P12-01 needs, which is only ever *this code touched that table, and
here is what `app.workspace_id` was at that moment*.

That is also why they are tolerant of a caller returning nothing, raising a
domain error, or finding no rows. A caller that raises `NotFound` still issued
its query, and the query is the evidence. What they are NOT tolerant of is a
caller that never reaches the database at all — `assert_touched` turns that
into a failure, because a test that silently stopped short would restore
exactly the false-green this batch exists to remove.

── Why a real database and not a double ─────────────────────────────────────

`app.workspace_id` is a PostgreSQL setting read by a PostgreSQL policy. A
double would record what the code intended to bind, which is the thing already
known from reading it. The whole point of an executing audit is to ask the
connection, not the source.
"""
from __future__ import annotations

import os
import uuid

import sys

import pytest

asyncpg = pytest.importorskip("asyncpg")

# `worker.notifier`'s stub is completed in the SHARED conftest — it was needed
# here first, and then again by P12-03's briefing tests, so it belongs where
# both can rely on it rather than in this directory.

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")

#: Set by the workflow step that runs this directory against PostgreSQL, on the
#: same reasoning as `tests/tenancy/test_harness_is_configured.py`: its presence
#: means "this job must have a database", independently of whether the DSN it is
#: meant to carry is still there.
MARKER = "IRA_REQUIRE_DB_HARNESS"


def pytest_ignore_collect(collection_path, config):
    """Do not COLLECT this directory when there is no database to reach.

    A `pytestmark` at conftest scope does not apply to the tests in the
    directory — it applies to the conftest module, which has no tests. The
    first version of this file used one and it did nothing, so the CI `test`
    job (no PostgreSQL service) collected all twenty-four and every fixture
    errored on connection refused.

    Worse, it errored rather than skipping: `asyncpg.connect(dsn="")` is not
    "no DSN", it is libpq's defaults — localhost:5432. An unset variable and a
    misconfigured one produced the same failure, which is the ambiguity this
    hook removes.

    Collection is refused ONLY when nothing has declared that a database is
    expected. If the marker or the DSN is present, the directory collects and
    `install_audit` fails loudly if it cannot connect — a job that asked for a
    database and did not get one must not report green having run nothing.
    """
    if os.getenv("IRA_TEST_DATABASE_DSN") or os.getenv(MARKER):
        return False
    return True

#: Same namespace the rest of the suite derives fixture ids from, so a
#: workspace created here and one created by `rls_workspace.ensure_workspace`
#: are the same row rather than two rows racing `033`'s one-default trigger.
_NAMESPACE = uuid.UUID("6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35")


def workspace_of(tenant: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"workspace:default:{tenant}")


@pytest.fixture(scope="session", autouse=True)
def install_audit():
    """Install the binding audit if the runner has not already done it.

    Without this, every assertion in this directory stands down when the suite
    is run by plain `pytest` — and `scripts/audit/workspace_binding_audit.py`
    is the only runner that installs it. Twenty-four tests that skip in CI and
    pass under one hand-run script are not a gate; they are a gate-shaped
    object, and the repository has been here before:
    `tests/tenancy/test_harness_is_configured.py` exists because three P2-05
    suites skipped themselves and the job went green having run none of them.

    So the directory installs its own instrument. Reading the table list and
    the live-predicate set from the catalogue rather than accepting a
    hard-coded pair, for the same reason the audit script does.
    """
    from tests import conftest_workspace_audit as audit

    if audit.WORKSPACE_TABLES:
        return  # the audit script got here first

    assert DSN, (
        f"{MARKER} is set, so this job declared it must reach a database, but "
        f"IRA_TEST_DATABASE_DSN is empty. Refusing to continue: an empty DSN "
        f"is not 'no database' to asyncpg, it is libpq's defaults, and the "
        f"connection error that produces names localhost rather than the "
        f"missing variable."
    )

    import asyncio

    async def _load():
        conn = await asyncpg.connect(dsn=DSN)
        try:
            tables = [r["table_name"] for r in await conn.fetch(
                "SELECT table_name FROM information_schema.columns "
                " WHERE column_name = 'workspace_id' AND table_schema = 'public'")]
            live = [r["tablename"] for r in await conn.fetch(
                "SELECT DISTINCT tablename FROM pg_policies "
                " WHERE schemaname = 'public' "
                "   AND (qual LIKE '%workspace_id%' "
                "     OR with_check LIKE '%workspace_id%')")]
            return tables, live
        finally:
            await conn.close()

    tables, live = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        _load())
    audit.install(tables, live)


#: `memory_embeddings.embedding` is `vector(1024)`.
EMBED_DIM = 1024


def _fake_vector(text: str) -> list[float]:
    """A deterministic pseudo-embedding, as `test_issue_55` already uses.

    Same text → same vector. It is not semantic and nothing here claims it is;
    what it buys is a real pgvector column receiving a real value, so the
    INSERT and the similarity SELECT are genuine queries under genuine
    policies. The model is the thing being avoided, not the database.
    """
    import hashlib

    digest = hashlib.sha256(text.encode()).digest()
    return [((digest[i % len(digest)] / 255.0) - 0.5) for i in range(EMBED_DIM)]


@pytest.fixture(autouse=True)
def stub_embeddings(monkeypatch):
    """No model in this environment, and the model is not the subject.

    `sentence_transformers` is stubbed away in `tests/conftest.py` already, so
    an unpatched `embed_one` raises ImportError before the caller reaches the
    database — which would leave exactly the "never observed" gap this whole
    directory exists to close.
    """
    async def embed_one(text: str):
        return _fake_vector(text)

    async def embed(texts):
        return [_fake_vector(t) for t in texts]

    # Patched on `memory.embeddings` itself, because every caller imports the
    # function lazily INSIDE the function body — `from memory.embeddings import
    # embed_one` runs at call time, so it picks up whatever the module holds
    # then. Patching each caller's namespace instead would miss them all.
    monkeypatch.setattr("memory.embeddings.embed_one", embed_one, raising=False)
    monkeypatch.setattr("memory.embeddings.embed", embed, raising=False)

    from config import get_settings

    settings = get_settings()
    # The reranker is a second model, and it reorders results RLS has already
    # filtered — switching it off removes a dependency without removing the
    # query being observed.
    monkeypatch.setattr(settings, "reranker_enabled", False, raising=False)


@pytest.fixture
async def pool():
    """A real pool installed where `utils.db.acquire()` will find it.

    Every connection helper in `core.identity.db_context` goes through
    `utils.db.acquire()`, so replacing the module-level pool is what makes the
    product's own code path reach this database instead of failing to find one.
    """
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        try:
            await created.close()
        except Exception:  # noqa: BLE001 — teardown must not mask a failure
            created.terminate()


@pytest.fixture
async def tenant(pool):
    """One tenant with an organisation, a default workspace and a member.

    Provisioned through `ira_app` under forced RLS rather than through
    `provision_tenant_substrate`, for the reason `rls_workspace.py` already
    records: `039` granted EXECUTE on the provisioning function to the schema
    owner and the migrator only, and these suites connect as the runtime role
    on purpose. Rows created here go through the same policies everything else
    does — an `ira_app` connection bound to a tenant may create that tenant's
    own substrate, and if it could not, this fixture would fail rather than
    quietly reach around the boundary.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    ws = workspace_of(tenant_id)
    slug = f"p1201-{tenant_id.hex[:10]}"

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
            tenant_id, f"P12-01 {slug}", slug)
        await conn.execute("SELECT set_config('app.tenant_id',$1,false)", str(tenant_id))
        await conn.execute("SELECT set_config('app.user_id',$1,false)", str(user_id))
        org = await conn.fetchval(
            "INSERT INTO organisations (tenant_id,name) VALUES ($1,'P12-01') RETURNING id",
            tenant_id)
        await conn.execute(
            "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,"
            "                        is_default,status) "
            "VALUES ($1,$2,$3,'Default','default',TRUE,'active')",
            ws, tenant_id, org)
        # A real `users` row, because `tenant_memberships.user_id` is a foreign
        # key to it. Creating the membership without the user would have been
        # possible only by dropping the constraint, and a fixture that weakens
        # the schema to make a test pass is measuring a different database.
        await conn.execute(
            "INSERT INTO users (id,email,display_name,status) "
            "VALUES ($1,$2,'P12-01 probe','active')",
            user_id, f"p1201-{user_id.hex[:12]}@example.invalid")
        await conn.execute(
            "INSERT INTO tenant_memberships (tenant_id,user_id,role,status,joined_at) "
            "VALUES ($1,$2,'owner','active',now())", tenant_id, user_id)
        await conn.execute(
            "INSERT INTO workspace_memberships (tenant_id,workspace_id,user_id,role,status) "
            "VALUES ($1,$2,$3,'workspace_admin','active')", tenant_id, ws, user_id)
        # Leave the session carrying nothing. A fixture that returned a
        # connection still bound would make every "unbound" observation below
        # meaningless, and this pool is shared with the code under test.
        await conn.execute("SELECT set_config('app.tenant_id','',false)")
        await conn.execute("SELECT set_config('app.user_id','',false)")
        await conn.execute("SELECT set_config('app.workspace_id','',false)")

    yield {"tenant_id": tenant_id, "user_id": user_id, "workspace_id": ws,
           "organisation_id": org}

    # Memberships MUST go. `tests/tenancy/test_zz_no_fixture_residue.py`
    # explains the consequence in full: `rollback/035_memberships_down.sql`
    # refuses while more than one tenant holds membership data, so a leaked
    # fixture tenant fails a rollback step several stages later with an error
    # that names `035` and points nowhere near the fixture that caused it.
    #
    # Leftover `tenants`, `users` and `workspaces` rows are harmless to every
    # rollback guard and are deliberately left, matching the scope that test
    # sets for itself.
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,false)", str(tenant_id))
        await conn.execute("DELETE FROM workspace_memberships WHERE tenant_id = $1",
                           tenant_id)
        await conn.execute("DELETE FROM tenant_memberships WHERE tenant_id = $1",
                           tenant_id)
        await conn.execute("SELECT set_config('app.tenant_id','',false)")


@pytest.fixture
def principal(tenant):
    """A cloud principal carrying the tenant AND its verified workspace."""
    from core.editions import EDITION_CLOUD
    from core.identity import Principal

    return Principal(
        user_id=tenant["user_id"],
        tenant_id=tenant["tenant_id"],
        session_id=uuid.uuid4(),
        edition=EDITION_CLOUD,
        workspace_id=tenant["workspace_id"],
        workspace_role="workspace_admin",
    )


@pytest.fixture
def audit_probe():
    """Read what the audit observed for one file, over a bounded window.

    Returns a callable taking a source path suffix and giving back the
    bound/unbound totals recorded for it SINCE the fixture was created. The
    window matters: these files are also reached by other suites in the same
    process, and a test that read the global total could pass on somebody
    else's execution.
    """
    from tests import conftest_workspace_audit as audit

    def _snapshot() -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for site, rec in audit.observed().items():
            f = site.rsplit(":", 1)[0]
            d = out.setdefault(f, {"bound": 0, "unbound": 0})
            d["bound"] += rec["bound"]
            d["unbound"] += rec["unbound"]
        return out

    before = _snapshot()

    def since(path_suffix: str) -> dict[str, int]:
        after = _snapshot()
        total = {"bound": 0, "unbound": 0}
        for f, counts in after.items():
            if not f.endswith(path_suffix):
                continue
            was = before.get(f, {"bound": 0, "unbound": 0})
            total["bound"] += counts["bound"] - was["bound"]
            total["unbound"] += counts["unbound"] - was["unbound"]
        return total

    return since


def assert_touched(probe, path_suffix: str) -> dict[str, int]:
    """The caller must have reached the database, or this proves nothing.

    A test that exercised no query is indistinguishable, in the audit's output,
    from a test that was never written. This is the guard that stops that
    difference from being invisible.

    A missing audit is a FAILURE and not a skip. `install_audit` makes it
    unreachable, and it is asserted rather than assumed because the version of
    this function that skipped instead turned twenty-four tests into
    twenty-four green skips under plain `pytest` — which is the shape of a
    suite opting itself out of the job that is supposed to run it.
    """
    from tests import conftest_workspace_audit as audit

    assert audit.WORKSPACE_TABLES, (
        "the workspace binding audit is not installed, so nothing this test "
        "did was observed and its assertions mean nothing. `install_audit` in "
        "this directory's conftest is supposed to make that impossible."
    )
    counts = probe(path_suffix)
    assert counts["bound"] + counts["unbound"] > 0, (
        f"{path_suffix} issued no query against a workspace-carrying table "
        f"during this test, so it contributes NO evidence to P12-01. An "
        f"exercise that does not reach the database leaves the caller exactly "
        f"as unclassified as never running it, while looking like coverage."
    )
    return counts
