"""One person's "delete everything" and "export everything" reach their own rows.

Measured on 20 October (`docs/testing/evidence/everything-scope-2026-10-20/`):
one member's `erase_postgres(scope="everything")` deleted a colleague's row in
all 29 tables it reaches beyond conversations, messages and memories, and
reported success; their export handed over the colleague's row in 28. Those
tables' policies check only the tenant and workspace.

ADR-0010, accepted by the owner on 2026-10-20, narrows them in three steps.
This file covers all three groups, through `core.privacy.erasure.PERSONAL`:
Group A, the tables with a column that names the person; Group B, the tables
that record only who created a row (`created_by`), whose rows are their
author's (Q2); and Group C, the tables that named nobody until migration 095
gave them `owner_user_id` (Q3). A row with no author or owner is kept, and
erasure reports how many (Q4). It also holds the
line for the rest: a table that names a person and is not narrowed fails
`test_every_table_that_names_a_person_is_narrowed`.

Real PostgreSQL, real RLS, as `ira_app`. Rows are seeded through the admin DSN.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import uuid
from typing import Literal

import pytest

for _k, _v in {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-test",
    "REDIS_PASSWORD": "ci-test",
    "VLLM_API_KEY": "ci-test",
}.items():
    os.environ.setdefault(_k, _v)

# `core.privacy.retention` reaches `memory.store`, which imports
# `memory.embeddings`, which imports `SentenceTransformer` by name. conftest
# stubs the package without that attribute; nothing here embeds anything.
_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    setattr(_st, "SentenceTransformer", object)
    setattr(_st, "CrossEncoder", object)

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

TENANT = uuid.uuid4()
WORKSPACE = uuid.uuid4()
SLUG = f"evp-{TENANT.hex[:8]}"
PEOPLE = {"alice": uuid.uuid4(), "bob": uuid.uuid4()}


def _name(who: str) -> str:
    """The account name, kept in `users.external_subject` as a private install does."""
    return f"{who}-{SLUG}"


#: ADR-0010 Group A, one row per person: (table, the column that identifies the
#: row, column -> value). In a value, {who} is "alice"/"bob", {name} the
#: account name, {uid} the users.id and {n} a uniquifier; "@table" is the same
#: person's row in an earlier table. tenant_id and workspace_id are added when
#: the table has them.
GROUP_A: list[tuple[str, str, dict]] = [
    ("privacy_envelopes", "id", {"kind": "owner_private", "name": "{who} envelope", "owner_id": "{name}"}),
    ("envelope_members", "granted_by", {"envelope_id": "@privacy_envelopes", "user_id": "{name}", "granted_by": "{who} grant {n}"}),
    ("commitments", "id", {"user_id": "{name}", "statement": "{who} commitment"}),
    ("lifeos_events", "seq", {"user_id": "{name}", "kind": "{who} event"}),
    ("cognitive_predictions", "prediction_id", {"prediction_id": "{who}-p-{n}", "owner_id": "{name}", "action": "a", "success_probability": 0.5, "risk": 0.1, "uncertainty": 0.1, "reversible": True, "model_version": "v"}),
    ("cognitive_prediction_outcomes", "outcome_id", {"outcome_id": "{who}-o-{n}", "prediction_id": "@cognitive_predictions", "owner_id": "{name}", "succeeded": True}),
    ("cognitive_cycles", "cycle_id", {"cycle_id": "{who}-c-{n}", "owner_id": "{name}", "summary": "s", "percept_event_id": "e"}),
    ("cognitive_events", "event_id", {"event_id": "{who}-e-{n}", "user_id": "{name}", "kind": "k", "source": "s", "summary": "s"}),
    ("cognitive_goals", "goal_id", {"goal_id": "{who}-g-{n}", "owner_id": "{name}", "title": "t", "desired_outcome": "o", "priority": 0.5, "state": "active", "source": "owner"}),
    ("files", "id", {"user_id": "{name}", "filename": "{who} file", "storage_path": "/x/{who}", "sha256": "{n}"}),
    ("realtime_sessions", "id", {"id": "@uuid", "user_id": "{uid}"}),
    ("realtime_events", "id", {"session_id": "@realtime_sessions", "kind": "requested", "at_ms": 0}),
    ("connector_oauth_states", "state", {"state": "{who}-{n}-0123456789abcdef0123456789abcdef", "user_id": "{uid}", "connector": "gmail", "verifier_ciphertext": "@bytes", "encryption_key_id": "k", "verifier_nonce": "@bytes", "code_challenge": "c", "expires_at": "@later"}),
]
GROUP_A_TABLES = {table for table, _, _ in GROUP_A}

#: ADR-0010 Group B, in the same form: the row's author is `created_by`. Each
#: table also gets a row from "nobody", with no author, which erasure keeps.
GROUP_B: list[tuple[str, str, dict]] = [
    ("projects", "id", {"id": "@uuid", "name": "{who} project", "slug": "{who}-{n}", "created_by": "{uid}"}),
    ("agents", "id", {"name": "{who}-agent-{n}", "created_by": "{uid}"}),
    ("decisions", "id", {"summary": "{who} decision", "review_at": "@later", "created_by": "{uid}"}),
    ("calendar_events", "id", {"external_id": "{who}-{n}", "title": "{who} meeting", "start_at": "@later", "end_at": "@later", "created_by": "{uid}"}),
    ("reminders", "id", {"title": "{who} reminder", "remind_at": "@later", "created_by": "{uid}"}),
    ("entities", "id", {"type": "person", "name": "{who} friend {n}", "created_by": "{uid}"}),
    ("edges", "id", {"src_entity_id": "@entities", "dst_entity_id": "@entities", "relation": "knows", "created_by": "{uid}"}),
    ("business_events", "id", {"event_type": "order", "title": "{who} order", "created_by": "{uid}"}),
    ("strategy_predictions", "id", {"question": "{who} question", "option_name": "o", "success_probability": 0.5, "risk": 0.1, "effort": 0.1, "utility": 0.3, "created_by": "{uid}"}),
    ("strategy_calibration", "domain", {"domain": "{who}-{n}", "created_by": "{uid}"}),
    ("model_performance", "id", {"model_name": "{who} model", "latency_ms": 1, "created_by": "{uid}"}),
]
GROUP_B_TABLES = {table for table, _, _ in GROUP_B}

#: ADR-0010 Group C: the owner is `owner_user_id`, added by 095. Each table also
#: gets a row with no owner ("nobody"), which erasure keeps.
GROUP_C: list[tuple[str, str, dict]] = [
    ("tasks", "id", {"title": "{who} task", "owner_user_id": "{uid}"}),
    ("people", "id", {"id": "@uuid", "person_name": "{who} contact", "relationship": "friend", "owner_user_id": "{uid}"}),
    # No workspace, as worker/notifier.py writes them: both erasure passes see
    # such a row, so a count that is not split by pass counts it twice.
    ("notifications", "id", {"category": "c", "title": "{who} notification", "body": "b", "owner_user_id": "{uid}", "workspace_id": None}),
    ("briefings", "id", {"content": "{who} briefing", "owner_user_id": "{uid}"}),
    ("heartbeat_surfaced", "id", {"item_key": "{who}-{n}", "kind": "k", "owner_user_id": "{uid}"}),
]
GROUP_C_TABLES = {table for table, _, _ in GROUP_C}
#: The tables where a row may name nobody, and is then kept.
UNOWNED_KEPT = GROUP_B_TABLES | GROUP_C_TABLES
SEEDS = GROUP_A + GROUP_B + GROUP_C


@pytest.fixture
async def pool():
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db_module._pool = db_module._pool, created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


@pytest.fixture
async def seeded(pool):
    """Alice and Bob in one workspace, one row each in every Group A, B and C
    table, and a row with no author or owner ("nobody") in every Group B and C
    table.
    Yields the admin connection and {(table, who): key}."""
    admin = await asyncpg.connect(ADMIN_DSN)
    keys: dict[tuple[str, str], object] = {}
    try:
        await _wipe(admin)
        await admin.execute(
            "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
            TENANT, f"Everything personal {SLUG}", SLUG)
        org = await admin.fetchval(
            "INSERT INTO organisations (tenant_id,name,slug) VALUES ($1,'O',$2) RETURNING id",
            TENANT, SLUG)
        await admin.execute(
            "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default) "
            "VALUES ($1,$2,$3,'W',$4,TRUE)", WORKSPACE, TENANT, org, SLUG)
        for who, uid in PEOPLE.items():
            await admin.execute(
                "INSERT INTO users (id,email,display_name,auth_provider,external_subject,status) "
                "VALUES ($1,NULL,'R','password',$2,'active')", uid, _name(who))
            await admin.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
                "VALUES ($1,$2,'member','active')", TENANT, uid)
            await admin.execute(
                "INSERT INTO workspace_memberships (tenant_id,workspace_id,user_id,role) "
                "VALUES ($1,$2,$3,'member')", TENANT, WORKSPACE, uid)
        columns: dict[str, dict[str, str]] = {}
        for r in await admin.fetch(
                "SELECT table_name, column_name, udt_name FROM information_schema.columns "
                "WHERE table_schema = 'public'"):
            columns.setdefault(r["table_name"], {})[r["column_name"]] = r["udt_name"]
        now = dt.datetime.now(dt.timezone.utc)
        for n, (table, key, spec) in enumerate(SEEDS):
            authors: dict[str, uuid.UUID | None] = dict(PEOPLE)
            if table in UNOWNED_KEPT:
                authors["nobody"] = None
            for who, uid in authors.items():
                values: dict[str, object] = {}
                for column, template in spec.items():
                    if template == "@uuid":
                        value: object = uuid.uuid4()
                    elif template == "@bytes":
                        value = os.urandom(48)
                    elif template == "@later":
                        value = now + dt.timedelta(hours=1)
                    elif isinstance(template, str) and template.startswith("@"):
                        value = keys[(template[1:], who)]
                    elif template == "{uid}" and uid is None:
                        value = None
                    elif isinstance(template, str):
                        value = template.format(who=who, uid=uid, name=_name(who),
                                                n=f"{n}{SLUG}")
                        if columns[table].get(column) == "uuid":
                            value = uuid.UUID(str(value))
                    else:
                        value = template
                    values[column] = value
                for column, value in (("tenant_id", TENANT), ("workspace_id", WORKSPACE)):
                    if column in columns[table]:
                        values.setdefault(column, value)
                names = list(values)
                keys[(table, who)] = await admin.fetchval(
                    f'INSERT INTO "{table}" ({", ".join(names)}) '  # noqa: S608
                    f'VALUES ({", ".join(f"${i + 1}" for i in range(len(names)))}) '
                    f"RETURNING {key}",
                    *values.values())
        yield admin, keys
    finally:
        await _wipe(admin)
        await admin.close()


async def _wipe(admin):
    # Every Group B table's tenant key is ON DELETE RESTRICT; children first.
    for table, _, _ in reversed(GROUP_B):
        await admin.execute(f'DELETE FROM "{table}" WHERE tenant_id = $1', TENANT)  # noqa: S608
    await admin.execute("DELETE FROM tenants WHERE id = $1", TENANT)
    for uid in PEOPLE.values():
        await admin.execute("DELETE FROM users WHERE id = $1", uid)


def _alice(edition: Literal["private", "cloud"] = "cloud"):
    from core.identity.principal import Principal

    return Principal(user_id=PEOPLE["alice"], tenant_id=TENANT, workspace_id=WORKSPACE,
                     session_id=uuid.uuid4(), edition=edition,
                     username=_name("alice"), is_owner=edition == "private")


async def _present(admin, keys, table: str) -> set[str]:
    """Whose row is still in `table`."""
    key = next(k for t, k, _ in SEEDS if t == table)
    return {who for (t, who), value in keys.items() if t == table and await admin.fetchval(
        f'SELECT count(*) FROM "{table}" WHERE {key} = $1',  # noqa: S608
        value)}


# ── Erasure ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("edition", ["cloud", "private"])
async def test_deleting_everything_spares_a_colleagues_rows(seeded, edition):
    from core.privacy.erasure import erase_postgres

    admin, keys = seeded
    result = await erase_postgres(_alice(edition), scope="everything")

    assert result.ok, result.error
    left = {table: await _present(admin, keys, table)
            for table in GROUP_A_TABLES | UNOWNED_KEPT}
    assert {t for t, who in left.items() if "bob" not in who} == set(), (
        "Alice's erasure deleted Bob's row in these tables")
    assert {t for t, who in left.items() if "alice" in who} == set(), (
        "Alice's erasure left her own row in these tables")
    assert {t for t in UNOWNED_KEPT if "nobody" not in left[t]} == set(), (
        "Alice's erasure deleted a row with no author or owner in these tables")


async def test_erasure_reports_the_rows_it_kept_for_having_no_owner(seeded):
    """ADR-0010 Q2 and Q4: a Group B or C row with no author or owner is kept,
    and the erasure says how many, per table, rather than reporting a clean
    sweep. Each is counted once, `notifications` included, which erasure
    reaches in both passes."""
    from core.privacy.erasure import erase_postgres

    result = await erase_postgres(_alice(), scope="everything")

    assert result.ok, result.error
    assert (f"Kept {len(UNOWNED_KEPT)} row(s) that record no author or owner"
            in result.note), result.note
    for table in UNOWNED_KEPT:
        assert f"{table} 1" in result.note, (table, result.note)


async def test_deleting_memories_alone_touches_none_of_these_tables(seeded):
    from core.privacy.erasure import erase_postgres

    admin, keys = seeded
    await erase_postgres(_alice(), scope="memories")

    for table in GROUP_A_TABLES:
        assert await _present(admin, keys, table) == {"alice", "bob"}, table
    for table in UNOWNED_KEPT:
        assert await _present(admin, keys, table) == {"alice", "bob", "nobody"}, table


# ── Export ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("edition", ["cloud", "private"])
async def test_exporting_everything_holds_only_the_persons_rows(seeded, edition):
    from core.privacy.export import NEVER_EXPORTED, export_subject_data

    admin, keys = seeded
    result = await export_subject_data(_alice(edition), scope="everything")

    assert result.ok, result.error
    exported: dict[str, set] = {}
    for t in result.tables:
        if t.table not in GROUP_A_TABLES | UNOWNED_KEPT:
            continue
        key = next(k for table, k, _ in SEEDS if table == t.table)
        exported.setdefault(t.table, set()).update(str(row.get(key)) for row in t.rows)
    for table in (GROUP_A_TABLES | UNOWNED_KEPT) - NEVER_EXPORTED:
        assert str(keys[(table, "bob")]) not in exported.get(table, set()), (
            f"Alice's export handed over Bob's {table} row")
        assert str(keys[(table, "alice")]) in exported.get(table, set()), (
            f"Alice's export left out her own {table} row")
    for table in UNOWNED_KEPT - NEVER_EXPORTED:
        assert str(keys[(table, "nobody")]) not in exported.get(table, set()), (
            f"Alice's export handed over a {table} row with no author or owner")


# ── The list itself ──────────────────────────────────────────────────────────


async def test_every_table_that_names_a_person_is_narrowed(seeded):
    """A table in the `everything` scope with a person column, its author's
    included, is narrowed (`erasure.PERSONAL`). A new table that names a person
    fails here until it is listed."""
    from core.privacy.erasure import SUBJECT_TABLES
    from core.privacy.erasure import PERSONAL

    admin, _ = seeded
    rows = await admin.fetch(
        "SELECT DISTINCT table_name FROM information_schema.columns "
        " WHERE table_schema = 'public' AND table_name = ANY($1::text[]) "
        "   AND column_name IN ('user_id', 'owner_id', 'owner_user_id', 'created_by')",
        list(SUBJECT_TABLES["everything"]))
    names_a_person = {r["table_name"] for r in rows}
    # conversations and messages are narrowed by their own policies (ADR-0009).
    by_policy = {"conversations", "messages"}
    missing = names_a_person - set(PERSONAL) - by_policy
    assert not missing, f"these tables name a person but are erased workspace-wide: {missing}"
    assert GROUP_A_TABLES | UNOWNED_KEPT <= set(PERSONAL)
