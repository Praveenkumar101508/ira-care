"""P12-05 — an export is scoped by RLS, or it is a privileged dump.

The plan's acceptance criterion 6: *"A user can export their data, and the
export is scoped to what they may see — not a privileged dump."*

The one thing that already resembled an export was
`GET /backup/download/{filename}` — a plain `pg_dump` of every tenant, served
off disk with no filtering. So "an export exists" is not the property worth
testing. **Whose data comes back is.**

Three failure modes, each with its own test below, because they are
indistinguishable from the outside:

  * the export reaches ANOTHER tenant's rows                  — a disclosure
  * the export reaches none of its own workspace-scoped rows  — a silent empty,
    twenty tables wide, reported as success
  * the export reaches credential material                    — an
    authenticated download that turns a session into an offline attack

The middle one is this phase's recurring shape and the reason every table
appears in the result with its count and its boundary, including the zeros.
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


@pytest.fixture
async def pool():
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
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
        yield conn
    finally:
        await conn.close()


async def _tenant(admin, label: str):
    """A tenant with a default workspace and one member, seeded as superuser.

    A fixture has to be able to create the rows the policies under test are
    about to filter, which is why this is the admin connection and not the
    export's own.
    """
    slug = f"p1205-{label}-{uuid.uuid4().hex[:8]}"
    tenant_id = await admin.fetchval(
        "INSERT INTO tenants (name, slug, edition) VALUES ($1, $1, 'cloud') "
        "RETURNING id", slug)
    org_id = await admin.fetchval(
        "INSERT INTO organisations (tenant_id, name) VALUES ($1, 'P12-05') "
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
    await admin.execute(
        "INSERT INTO tenant_memberships (tenant_id, user_id, role, status) "
        "VALUES ($1, $2, 'owner', 'active')", tenant_id, user_id)
    await admin.execute(
        "INSERT INTO workspace_memberships (tenant_id, workspace_id, user_id, "
        "                                   role, status) "
        "VALUES ($1, $2, $3, 'workspace_admin', 'active')",
        tenant_id, workspace_id, user_id)
    return {"tenant_id": tenant_id, "workspace_id": workspace_id,
            "user_id": user_id, "slug": slug}


def _principal(t):
    from core.identity.principal import Principal
    from core.editions import EDITION_CLOUD

    return Principal(
        user_id=t["user_id"], tenant_id=t["tenant_id"],
        session_id=uuid.uuid4(), edition=EDITION_CLOUD,
        roles=frozenset({"owner"}), scopes=frozenset(),
        workspace_id=t["workspace_id"],
    )


async def _conversation(admin, t, title: str):
    """One workspace-scoped row, written as superuser so RLS is not involved.

    Owned by the tenant's user, the one whose export reads it: since 093
    (ADR-0009) a conversation is visible to its owner only.
    """
    return await admin.fetchval(
        "INSERT INTO conversations (tenant_id, workspace_id, owner_user_id, session_id, title) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id",
        t["tenant_id"], t["workspace_id"], t["user_id"], f"p1205-{uuid.uuid4().hex[:10]}",
        title)


@pytest.fixture
async def two_tenants(admin):
    a = await _tenant(admin, "a")
    b = await _tenant(admin, "b")
    yield a, b

    # ── Teardown that cannot half-complete ──────────────────────────────────
    #
    # `test_zz_no_fixture_residue` fails the WHOLE directory over one tenant
    # left holding memberships, because `rollback/035` refuses while more than
    # one tenant does — and its error names 035, not the fixture that leaked.
    #
    # The first version deleted seven tables in child-first order in one loop.
    # A full-directory run left five `p1205-a-*` tenants behind and no `b` ones,
    # which is the signature of a statement raising partway: everything after it
    # in the loop is skipped, silently, inside a teardown nobody is watching.
    #
    # Two properties fix the class rather than that instance. Every child of
    # `tenants` cascades (checked in `pg_constraint`: `confdeltype = 'c'` for
    # conversations, memberships, workspaces, organisations, export_requests and
    # connector_accounts), so ONE delete removes all of them and there is no
    # order to get wrong. And each tenant is torn down independently, so a
    # failure on the first cannot leave the second standing.
    failures = []
    for t in (a, b):
        try:
            # The tenant first — everything tenant-scoped cascades with it.
            await admin.execute("DELETE FROM tenants WHERE id = $1", t["tenant_id"])
            # `users` carries no tenant_id, so it is the one row that does not.
            await admin.execute("DELETE FROM users WHERE id = $1", t["user_id"])
        except Exception as exc:  # noqa: BLE001 — reported, never swallowed
            failures.append(f"{t['slug']}: {type(exc).__name__}: {exc}")

    assert not failures, (
        "this fixture leaked tenants, and a leak here fails a DIFFERENT test "
        "later in the directory with an error naming migration 035:\n  "
        + "\n  ".join(failures)
    )


# ── The property the whole batch exists for ─────────────────────────────────


async def test_the_export_does_not_reach_another_tenant(pool, admin, two_tenants):
    """The disclosure this feature could become.

    Both tenants hold a conversation with a recognisable title. A's export must
    contain A's and not B's — and the assertion is on the CONTENT, not on a
    count, because two rows and two rows is the same number.
    """
    from core.privacy.export import export_subject_data

    a, b = two_tenants
    await _conversation(admin, a, "A-ONLY-SECRET")
    await _conversation(admin, b, "B-ONLY-SECRET")

    result = await export_subject_data(_principal(a), scope="conversations")
    assert result.ok, result.error

    titles = [row.get("title") for t in result.tables for row in t.rows]
    assert "A-ONLY-SECRET" in titles, (
        f"the export did not contain the caller's own conversation. "
        f"tables={[(t.table, t.boundary, t.count) for t in result.tables]}"
    )
    assert "B-ONLY-SECRET" not in titles, (
        "the export contained ANOTHER TENANT'S conversation. This is the "
        "`pg_dump` outcome the batch exists to avoid."
    )


async def test_a_workspace_scoped_table_is_read_under_a_workspace_binding(
        pool, admin, two_tenants):
    """The silent empty, caught by the thing that makes it visible.

    `conversations` carries a workspace predicate. Read on a tenant-only
    connection it matches nothing — no error, no warning, an export that looks
    complete and is empty. So the result records WHICH boundary each table was
    read under, and this asserts it rather than trusting the row count: a table
    with rows proves the binding was right, but a table with none proves
    nothing at all.
    """
    from core.privacy.export import export_subject_data

    a, _ = two_tenants
    await _conversation(admin, a, "A-WORKSPACE-ROW")

    result = await export_subject_data(_principal(a), scope="conversations")
    assert result.ok, result.error
    assert result.workspaces_read >= 1, (
        "no workspace was enumerated, so every workspace-scoped table was read "
        "under the wrong context or not at all"
    )

    conversations = [t for t in result.tables if t.table == "conversations"]
    assert conversations, "conversations is missing from the export entirely"
    assert all(t.boundary == "workspace" for t in conversations), (
        f"conversations was read under {[t.boundary for t in conversations]}. "
        f"It carries a workspace predicate, so a tenant-only read matches "
        f"nothing and reports success."
    )
    assert sum(t.count for t in conversations) >= 1


async def test_every_table_in_scope_appears_even_when_empty(pool, admin,
                                                            two_tenants):
    """An absent section and an empty one must not look the same.

    A user reading an export cannot otherwise tell "I have no messages" from
    "the messages were never read". Neither can an operator, which is how a
    broken export survives.
    """
    from core.privacy.export import export_subject_data, exportable_tables

    a, _ = two_tenants
    result = await export_subject_data(_principal(a), scope="everything")
    assert result.ok, result.error

    reported = {t.table for t in result.tables}
    expected = set(exportable_tables("everything"))
    assert expected <= reported, (
        f"tables in scope and missing from the export: "
        f"{sorted(expected - reported)}. An export with a missing section reads "
        f"as an account with no data of that kind."
    )
    assert result.total_rows == 0
    assert any("all were empty" in n for n in result.notes), (
        f"a wholly empty export said nothing about being empty; notes={result.notes}"
    )


# ── Secret material ─────────────────────────────────────────────────────────


def test_no_credential_table_is_ever_exportable():
    """The risk the feature introduces, asserted on the derivation.

    RLS grants a user their own credential row — that is exactly why an
    exclusion is needed rather than assumed. A subject is entitled to know a
    credential EXISTS; handing over its material converts a stolen session into
    an offline cracking attack, and for a voiceprint into a permanent one,
    because a person cannot rotate their voice.
    """
    from core.privacy.export import NEVER_EXPORTED, exportable_tables

    for scope in ("memories", "conversations", "voice", "everything", "account"):
        exported = set(exportable_tables(scope))
        leaked = exported & NEVER_EXPORTED
        assert not leaked, f"{scope!r} would export {sorted(leaked)}"

    # The exclusion has to actually bite somewhere, or it is decorative.
    assert set(exportable_tables("account")) < set(
        __import__("core.privacy.erasure", fromlist=["x"]).SUBJECT_TABLES["account"]
    ), "the account scope excludes nothing; NEVER_EXPORTED is not reaching it"


def test_the_export_inventory_is_derived_from_the_erasure_inventory():
    """One definition, two readers.

    A table erasure destroys and export cannot show is a table the product
    deletes on request and cannot account for. This asserts they are the same
    list minus a named, deliberate exclusion — rather than two lists that agree
    today.
    """
    from core.privacy.erasure import SUBJECT_TABLES
    from core.privacy.export import NEVER_EXPORTED, exportable_tables

    for scope, tables in SUBJECT_TABLES.items():
        assert set(exportable_tables(scope)) == set(tables) - NEVER_EXPORTED, (
            f"the export and erasure inventories disagree for {scope!r} beyond "
            f"the declared exclusions"
        )


async def test_a_redacted_column_is_stripped_but_its_table_is_kept(pool, admin,
                                                                   two_tenants):
    """`connector_accounts` is the case `REDACTED_COLUMNS` exists for.

    Which third-party accounts somebody connected, and when, is some of the most
    useful information in an export. The same row holds a refresh token, which
    is a live credential at another company. Dropping the table would withhold
    the useful half to protect the dangerous one.
    """
    from core.privacy.export import REDACTED_COLUMNS, exportable_tables

    # `account`, not `everything`: `connector_accounts` is deliberately outside
    # the content scope — erasure only reaches it when the account itself is
    # closed, because deleting one destroys a refresh token WITHOUT revoking it
    # at the provider.
    assert "connector_accounts" in exportable_tables("account")
    redacted = REDACTED_COLUMNS["connector_accounts"]

    # Checked against the LIVE SCHEMA, for every redacted table. This caught the
    # first version of `REDACTED_COLUMNS`, which named `access_token`,
    # `refresh_token` and `wrapped_dek` — none of which `connector_accounts`
    # has. It stripped nothing and read like protection.
    for table, columns in REDACTED_COLUMNS.items():
        live = {r["column_name"] for r in await admin.fetch(
            "SELECT column_name FROM information_schema.columns "
            " WHERE table_schema = 'public' AND table_name = $1", table)}
        assert live, f"{table} is not in the schema at all"
        missing = columns - live
        assert not missing, (
            f"REDACTED_COLUMNS strips {sorted(missing)} from {table}, and "
            f"{table} has no such column(s). A redaction aimed at a schema that "
            f"has moved protects nothing while reading like protection."
        )


# ── The record ──────────────────────────────────────────────────────────────


async def test_the_request_is_recorded(pool, admin, two_tenants):
    """`export_requests` has had a table since `026` and no writer.

    A request that produced a file and left no record of having been made is
    not an audit trail.
    """
    from core.privacy.export import export_subject_data, record_export_request

    a, _ = two_tenants
    principal = _principal(a)
    result = await export_subject_data(principal, scope="memories")
    await record_export_request(principal, scope="memories", result=result)

    row = await admin.fetchrow(
        "SELECT status, artifact_key, note FROM export_requests "
        " WHERE tenant_id = $1 ORDER BY requested_at DESC LIMIT 1",
        a["tenant_id"])
    try:
        assert row is not None, "the export left no record of having been requested"
        assert row["status"] == "completed"
        assert row["artifact_key"] is None, (
            "an artifact_key was recorded for a build with no object storage, "
            "so the record points at nothing"
        )
        assert "no object-storage backend" in row["note"], (
            f"the record does not say why there is no artifact; note={row['note']!r}"
        )
    finally:
        await admin.execute("DELETE FROM export_requests WHERE tenant_id = $1",
                            a["tenant_id"])


async def test_the_redaction_actually_strips_the_column(pool, admin, two_tenants):
    """The declaration is not the behaviour, and a plant proved it.

    `test_a_redacted_column_is_stripped_but_its_table_is_kept` checks that
    `REDACTED_COLUMNS` names real columns. It does NOT check that anything drops
    them: an adversarial plant that replaced `_read_table`'s lookup with an
    empty set left every test above green while the export emitted stored
    credential ciphertext.

    So this seeds real material and reads the export back.
    """
    from core.privacy.export import REDACTED_COLUMNS, export_subject_data

    a, _ = two_tenants
    # High-entropy bytes, not a readable marker: `credential_is_not_obvious_plaintext`
    # refuses a credential column that looks like text, which is a good
    # constraint and one this fixture has to respect. The assertion below is
    # about the COLUMN being absent, so the value never needs to be recognisable.
    marker = bytes(range(256)) * 2
    await admin.execute(
        "INSERT INTO connector_accounts (tenant_id, user_id, connector, "
        "                                provider_account, status, "
        "                                credential_ciphertext, "
        "                                credential_nonce, encryption_key_id) "
        "VALUES ($1, $2, 'gmail', $5, 'connected', "
        "        $3, $4, 'kek-p1205')",
        a["tenant_id"], a["user_id"], marker, b"nonce-p1205",
        "p1205-probe@example.invalid")

    try:
        result = await export_subject_data(_principal(a), scope="account")
        assert result.ok, result.error

        accounts = [t for t in result.tables if t.table == "connector_accounts"]
        assert accounts and sum(t.count for t in accounts) >= 1, (
            "the seeded connector account did not appear in the export at all, "
            "so this test cannot say anything about redaction"
        )

        rows = [r for t in accounts for r in t.rows]
        for column in REDACTED_COLUMNS["connector_accounts"]:
            assert all(column not in r for r in rows), (
                f"{column!r} was exported. It is stored credential material for "
                f"an account at another company, and an export is a file the "
                f"user downloads and keeps."
            )
        # …and the useful half survived, or the redaction is just a deletion.
        assert any(r.get("provider_account") == "p1205-probe@example.invalid"
                   for r in rows), (
            "the connector name was dropped too. Withholding which accounts "
            "somebody connected is not what the redaction is for."
        )
    finally:
        await admin.execute(
            "DELETE FROM connector_accounts "
            " WHERE provider_account = 'p1205-probe@example.invalid'")


# ── The route, where "the asking principal" is decided ──────────────────────


@pytest.fixture
def client_for(pool):
    """An ASGI client whose principal is one named identity.

    `_principal` — the privacy module's own dependency — is overridden, and
    nothing below it is. This file's subject is what the export CONTAINS for a
    given caller; P12-02's `test_p1202_workspace_chain.py` is where the
    unfaked identity chain is proved and stays the place for it.

    `httpx.ASGITransport` rather than `TestClient`: the latter drives the app on
    its own event loop, and the pool these tests install belongs to this one —
    which surfaces as `InterfaceError: another operation is in progress`, an
    error that looks like a concurrency bug in the code under test and is not.
    """
    from httpx import ASGITransport, AsyncClient

    from api.routes.privacy import _principal as principal_dependency
    from app.factory import create_app

    def build(principal):
        app = create_app()

        async def _p():
            return principal

        app.dependency_overrides[principal_dependency] = _p
        return AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://p1205.invalid")

    return build


async def test_the_route_exports_the_caller_and_not_the_other_tenant(
        pool, admin, two_tenants, client_for):
    """Acceptance criterion 6, at the surface a person actually reaches."""
    a, b = two_tenants
    await _conversation(admin, a, "A-ONLY-SECRET")
    await _conversation(admin, b, "B-ONLY-SECRET")

    async with client_for(_principal(b)) as client:
        response = await client.get("/api/v1/privacy/export?scope=conversations")

    assert response.status_code == 200, response.text
    assert "A-ONLY-SECRET" not in response.text, (
        "the export route served another tenant's conversation")
    assert "B-ONLY-SECRET" in response.text, (
        "the route returned nothing of the caller's own, so the assertion "
        "above would pass on an endpoint that exports nothing at all")

    body = response.json()
    assert body["ok"] is True
    assert body["workspaces_read"] >= 1
    assert {t["table"] for t in body["tables"]} == {"messages", "conversations"}
    assert all(t["boundary"] == "workspace" for t in body["tables"])

    await admin.execute("DELETE FROM export_requests WHERE tenant_id = ANY($1)",
                        [a["tenant_id"], b["tenant_id"]])


async def test_the_route_refuses_a_scope_it_does_not_recognise(
        pool, admin, two_tenants, client_for):
    """422, and no export. A silently-empty answer to an unknown scope would be
    indistinguishable from an account with no data of that kind."""
    a, _ = two_tenants

    async with client_for(_principal(a)) as client:
        response = await client.get("/api/v1/privacy/export?scope=everything-ish")

    assert response.status_code == 422, response.text
    assert "scope must be one of" in response.text
