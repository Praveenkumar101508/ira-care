"""095 gives Group C an owner column and attributes rows only where that is certain.

ADR-0010: "delete everything" and "export everything" reach only the person's
own rows. Group C's five tables named nobody until 095 gave them
`owner_user_id` (Q3). Group B's `created_by` was never recorded before ADR-0010
Group B. So every existing row of both groups has no owner, and erasure keeps
it (Q4). The owner decided on 2026-10-20 that 095 attributes those rows where
there is only one person they can belong to: a tenant with exactly one
membership row.

What matters here:

  * a single-member tenant's rows, in all sixteen tables, gain that member;
  * a tenant with a second membership row, even a suspended one, gains
    nothing;
  * a row that already names someone is not touched;
  * the rollback clears exactly the Group B authors 095 wrote, keeps one a
    writer recorded, and drops the columns and the record;
  * the RLS window closes, the runtime roles cannot read the record, and a
    second run changes nothing.

── How these run without damaging anything ─────────────────────────────────

As in `test_migration_094_remembered_facts_into_vault.py`: the real migration
and rollback files run inside a transaction that is always rolled back, with
their own BEGIN/COMMIT stripped so an inner COMMIT cannot commit the outer
transaction. `test_both_files_are_wrapped_in_a_transaction` asserts the pair is
in each file. Both run as `ira_schema_owner`, the role `scripts/migrate.py`
uses: a superuser bypasses RLS and would pass a 095 whose RLS window was
missing.

The database under test already has 095, so each test first runs the rollback
to reach the state before it, seeds rows in that shape, and then runs 095.
"""
from __future__ import annotations

import os
import pathlib
import re
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not ADMIN_DSN:
    pytest.skip("IRA_TEST_ADMIN_DSN not set", allow_module_level=True)

POSTGRES = pathlib.Path(__file__).resolve().parents[3] / "postgres"
MIGRATION = POSTGRES / "095_owners_and_single_user_backfill.sql"
ROLLBACK = POSTGRES / "rollback" / "095_owners_and_single_user_backfill_down.sql"

#: Group B: (table, the column that identifies the row, column -> value).
#: In a value, {n} is a uniquifier; "@later" a time; "@entities" the same
#: tenant's entity. `created_by` is left NULL, as every row was before ADR-0010.
GROUP_B: list[tuple[str, str, dict]] = [
    ("projects", "id", {"id": "@uuid", "name": "p", "slug": "{n}"}),
    ("agents", "id", {"name": "a-{n}"}),
    ("decisions", "id", {"summary": "d", "review_at": "@later"}),
    ("calendar_events", "id", {"external_id": "{n}", "title": "c", "start_at": "@later", "end_at": "@later"}),
    ("reminders", "id", {"title": "r", "remind_at": "@later"}),
    ("entities", "id", {"type": "person", "name": "e-{n}"}),
    ("edges", "id", {"src_entity_id": "@entities", "dst_entity_id": "@entities", "relation": "knows"}),
    ("business_events", "id", {"event_type": "order", "title": "b"}),
    ("strategy_predictions", "id", {"question": "q", "option_name": "o", "success_probability": 0.5, "risk": 0.1, "effort": 0.1, "utility": 0.3}),
    ("strategy_calibration", "domain", {"domain": "d-{n}"}),
    ("model_performance", "id", {"model_name": "m", "latency_ms": 1}),
]
#: Group C, in the shape before 095: no owner column.
GROUP_C: list[tuple[str, str, dict]] = [
    ("tasks", "id", {"title": "t"}),
    ("people", "id", {"id": "@uuid", "person_name": "p", "relationship": "friend"}),
    ("notifications", "id", {"category": "c", "title": "n", "body": "b"}),
    ("briefings", "id", {"content": "b"}),
    ("heartbeat_surfaced", "id", {"item_key": "k-{n}", "kind": "k"}),
]
#: Every table 095 lifts FORCE RLS on, and must force again.
WINDOW = ["tenant_memberships", "deletion_requests", "tenant_invitations", "delegated_users",
          *(t for t, _, _ in GROUP_B), *(t for t, _, _ in GROUP_C)]


def _body(path: pathlib.Path) -> str:
    """The script with its outermost BEGIN/COMMIT removed (see the module doc)."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?im)^BEGIN;\s*$", "", text, count=1)
    text = re.sub(r"(?im)^COMMIT;\s*$", "", text)
    return text


async def _run_as_schema_owner(conn, path: pathlib.Path) -> None:
    await conn.execute("SET LOCAL ROLE ira_schema_owner")
    try:
        await conn.execute(_body(path))
    finally:
        await conn.execute("RESET ROLE")


@pytest.fixture
async def db():
    """A connection inside a transaction that is always rolled back."""
    conn = await asyncpg.connect(ADMIN_DSN)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


@pytest.fixture
async def world(db):
    """The state before 095, with two tenants holding one row in each of the
    sixteen tables:

      * `single`: one membership row, Alice;
      * `two`: Bob, active, and Carol, suspended.

    Yields (db, {tenant label: tenant id}, {person: user id}, {(label, table): key}).
    """
    await _run_as_schema_owner(db, ROLLBACK)
    await db.execute("SET CONSTRAINTS ALL IMMEDIATE")

    people = {who: uuid.uuid4() for who in ("alice", "bob", "carol")}
    for who, uid in people.items():
        await db.execute(
            "INSERT INTO users (id,email,display_name,auth_provider,external_subject,status) "
            "VALUES ($1,NULL,'R','password',$2,'active')", uid, f"{who}-{uid.hex[:8]}")

    tenants: dict[str, uuid.UUID] = {}
    workspaces: dict[str, uuid.UUID] = {}
    for label, members in (("single", [("alice", "active")]),
                           ("two", [("bob", "active"), ("carol", "suspended")])):
        tid, wid = uuid.uuid4(), uuid.uuid4()
        slug = f"m095-{label}-{tid.hex[:8]}"
        await db.execute("INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
                         tid, f"095 {label}", slug)
        org = await db.fetchval(
            "INSERT INTO organisations (tenant_id,name,slug) VALUES ($1,'O',$2) RETURNING id",
            tid, slug)
        await db.execute(
            "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default) "
            "VALUES ($1,$2,$3,'W',$4,TRUE)", wid, tid, org, slug)
        for who, status in members:
            await db.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
                "VALUES ($1,$2,'member',$3)", tid, people[who], status)
        tenants[label], workspaces[label] = tid, wid

    columns: dict[str, set] = {}
    for r in await db.fetch("SELECT table_name, column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public'"):
        columns.setdefault(r["table_name"], set()).add(r["column_name"])

    keys: dict[tuple[str, str], object] = {}
    later = await db.fetchval("SELECT now() + interval '1 day'")
    for n, (table, key, spec) in enumerate(GROUP_B + GROUP_C):
        for label, tid in tenants.items():
            values: dict[str, object] = {}
            for column, template in spec.items():
                if template == "@uuid":
                    values[column] = uuid.uuid4()
                elif template == "@later":
                    values[column] = later
                elif template == "@entities":
                    values[column] = keys[(label, "entities")]
                elif isinstance(template, str):
                    values[column] = template.format(n=f"{n}-{tid.hex[:8]}")
                else:
                    values[column] = template
            values["tenant_id"] = tid
            if "workspace_id" in columns[table]:
                values["workspace_id"] = workspaces[label]
            names = list(values)
            keys[(label, table)] = await db.fetchval(
                f'INSERT INTO "{table}" ({", ".join(names)}) '  # noqa: S608
                f'VALUES ({", ".join(f"${i + 1}" for i in range(len(names)))}) RETURNING {key}',
                *values.values())
    yield db, tenants, people, keys


def _person_column(table: str) -> str:
    return "owner_user_id" if table in {t for t, _, _ in GROUP_C} else "created_by"


async def _who(db, tenants, keys, label: str, table: str):
    key = next(k for t, k, _ in GROUP_B + GROUP_C if t == table)
    return await db.fetchval(
        f'SELECT {_person_column(table)} FROM "{table}" '  # noqa: S608
        f"WHERE tenant_id = $1 AND {key} = $2", tenants[label], keys[(label, table)])


# ── The files ────────────────────────────────────────────────────────────────


def test_both_files_are_wrapped_in_a_transaction():
    for path in (MIGRATION, ROLLBACK):
        text = path.read_text(encoding="utf-8")
        assert re.search(r"(?im)^BEGIN;\s*$", text), f"{path.name} has no BEGIN"
        assert re.search(r"(?im)^COMMIT;\s*$", text), f"{path.name} has no COMMIT"


# ── The backfill ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("table", [t for t, _, _ in GROUP_B + GROUP_C])
async def test_a_single_member_tenants_rows_gain_that_member(world, table):
    db, tenants, people, keys = world
    await _run_as_schema_owner(db, MIGRATION)

    assert await _who(db, tenants, keys, "single", table) == people["alice"]


@pytest.mark.parametrize("table", [t for t, _, _ in GROUP_B + GROUP_C])
async def test_a_tenant_with_a_second_member_gains_nothing(world, table):
    """Carol is suspended, and still counts: her rows could be among these."""
    db, tenants, people, keys = world
    await _run_as_schema_owner(db, MIGRATION)

    assert await _who(db, tenants, keys, "two", table) is None


# ── One membership row is not proof of one person ────────────────────────────
#
# Erasing an account deletes its membership row, so a tenant that had two
# people can be left with one. These are the records erasure keeps that show
# someone else was there; any of them leaves the tenant's rows unattributed.

TABLES = [t for t, _, _ in GROUP_B + GROUP_C]


async def _attributed(db, tenants, keys, label: str) -> set[str]:
    return {t for t in TABLES if await _who(db, tenants, keys, label, t) is not None}


async def _account_erasure(db, tenant, user):
    await db.execute(
        "INSERT INTO deletion_requests (tenant_id, user_id, scope, status) "
        "VALUES ($1, $2, 'account', 'completed')", tenant, user)


async def _accepted_invitation(db, tenant, inviter, accepted_by):
    await db.execute(
        "INSERT INTO tenant_invitations (tenant_id, email, token_hash, invited_by, expires_at, "
        "accepted_at, accepted_by) VALUES ($1, $2, $3, $4, now() + interval '1 day', now(), $5)",
        tenant, f"{uuid.uuid4().hex}@example.invalid", uuid.uuid4().hex * 2, inviter, accepted_by)


async def _legacy_delegated_user(db, tenant, user, *, primary: bool = False):
    # Closed to new rows since 092; a row from before then is what this models.
    await db.execute("ALTER TABLE delegated_users DISABLE TRIGGER delegated_users_closed")
    await db.execute(
        "INSERT INTO delegated_users (id, username, password_hash, role, is_primary_owner, "
        "user_id, tenant_id) VALUES ($1, $2, 'x', 'trusted_user', $3, $4, $5)",
        uuid.uuid4(), f"legacy-{uuid.uuid4().hex[:8]}", primary, user, tenant)
    await db.execute("ALTER TABLE delegated_users ENABLE TRIGGER delegated_users_closed")


@pytest.mark.parametrize("trace", ["erased-account", "erased-account-no-user",
                                   "accepted-invitation", "legacy-delegated-user",
                                   "legacy-delegated-user-no-tenant"])
async def test_a_trace_of_another_person_leaves_a_single_member_tenant_alone(world, trace):
    """Alice is the only member left, but the tenant's records show someone
    else had access. Rows they may have written must not become Alice's."""
    db, tenants, people, keys = world
    tenant = tenants["single"]
    if trace == "erased-account":
        await _account_erasure(db, tenant, people["carol"])
    elif trace == "erased-account-no-user":
        await _account_erasure(db, tenant, None)
    elif trace == "accepted-invitation":
        await _accepted_invitation(db, tenant, people["alice"], people["carol"])
    elif trace == "legacy-delegated-user":
        await _legacy_delegated_user(db, tenant, None)
    else:
        await _legacy_delegated_user(db, None, None)
    await _run_as_schema_owner(db, MIGRATION)

    assert await _attributed(db, tenants, keys, "single") == set(), (
        f"095 attributed rows to Alice despite {trace}")


async def test_the_sole_members_own_records_do_not_block_the_backfill(world):
    """Alice's own erasure request, an invitation she accepted, and the primary
    owner's legacy row name no one else."""
    db, tenants, people, keys = world
    tenant = tenants["single"]
    await _account_erasure(db, tenant, people["alice"])
    await _accepted_invitation(db, tenant, people["bob"], people["alice"])
    await _legacy_delegated_user(db, tenant, None, primary=True)
    await db.execute(
        "INSERT INTO deletion_requests (tenant_id, user_id, scope, status) "
        "VALUES ($1, $2, 'everything', 'completed')", tenant, people["carol"])
    await _run_as_schema_owner(db, MIGRATION)

    assert await _attributed(db, tenants, keys, "single") == set(TABLES)


async def test_a_row_that_already_names_someone_is_left_alone(world):
    db, tenants, people, keys = world
    await db.execute("UPDATE projects SET created_by = $1 WHERE id = $2",
                     people["bob"], keys[("single", "projects")])
    await _run_as_schema_owner(db, MIGRATION)

    assert await _who(db, tenants, keys, "single", "projects") == people["bob"]
    assert not await db.fetchval(
        "SELECT count(*) FROM attribution_backfill_095 WHERE table_name = 'projects' "
        "AND row_key = $1", str(keys[("single", "projects")]))


async def test_the_record_lists_exactly_the_group_b_rows_attributed(world):
    db, tenants, people, keys = world
    await _run_as_schema_owner(db, MIGRATION)

    recorded = {(r["table_name"], r["row_key"], r["attributed_to"]) for r in await db.fetch(
        "SELECT * FROM attribution_backfill_095 WHERE attributed_to = $1", people["alice"])}
    expected = set()
    for table, _, _ in GROUP_B:
        key = keys[("single", table)]
        row_key = f"{tenants['single']}/{key}" if table == "strategy_calibration" else str(key)
        expected.add((table, row_key, people["alice"]))
    assert recorded == expected


# ── The rollback ─────────────────────────────────────────────────────────────


async def test_the_rollback_clears_exactly_what_095_attributed(world):
    """A backfilled author goes back to NULL; one a writer recorded after 095
    stays; the owner columns and the record are gone."""
    db, tenants, people, keys = world
    await _run_as_schema_owner(db, MIGRATION)
    recorded_by_writer = await db.fetchval(
        "INSERT INTO projects (id, name, slug, tenant_id, workspace_id, created_by) "
        "SELECT gen_random_uuid(), 'w', 'written-' || $2::text, $1, w.id, $3 "
        "  FROM workspaces w WHERE w.tenant_id = $1 RETURNING id",
        tenants["single"], tenants["single"].hex[:8], people["alice"])

    await _run_as_schema_owner(db, ROLLBACK)

    for table, _, _ in GROUP_B:
        assert await _who(db, tenants, keys, "single", table) is None, table
    assert await db.fetchval("SELECT created_by FROM projects WHERE id = $1",
                             recorded_by_writer) == people["alice"]
    assert await db.fetchval("SELECT to_regclass('public.attribution_backfill_095')") is None
    assert not await db.fetchval(
        "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
        "AND column_name = 'owner_user_id' AND table_name = ANY($1::text[])",
        [t for t, _, _ in GROUP_C])


async def test_deleting_a_user_keeps_their_rows_without_an_owner(world):
    """ON DELETE SET NULL, like Group B's `created_by`: removing an account must
    not delete the tenant's rows with it."""
    db, tenants, people, keys = world
    await _run_as_schema_owner(db, MIGRATION)
    await db.execute("DELETE FROM tenant_memberships WHERE user_id = $1", people["alice"])
    await db.execute("DELETE FROM users WHERE id = $1", people["alice"])

    for table, _, _ in GROUP_C:
        assert await db.fetchval(
            f'SELECT count(*) FROM "{table}" WHERE tenant_id = $1', tenants["single"]), table  # noqa: S608
        assert await _who(db, tenants, keys, "single", table) is None, table


# ── The window and the grants ────────────────────────────────────────────────


async def test_the_rls_window_closes(world):
    db, *_ = world
    await _run_as_schema_owner(db, MIGRATION)

    unforced = await db.fetch(
        "SELECT relname FROM pg_class WHERE relname = ANY($1::text[]) "
        "AND relkind = 'r' AND NOT relforcerowsecurity", WINDOW)
    assert not unforced, [r["relname"] for r in unforced]


async def test_the_runtime_roles_cannot_read_the_record(world):
    db, *_ = world
    await _run_as_schema_owner(db, MIGRATION)

    for role in ("ira_app", "ira_worker"):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await db.fetchval(
                "SELECT has_table_privilege($1, 'attribution_backfill_095', $2)",
                role, privilege), (role, privilege)


async def test_a_second_run_changes_nothing(world):
    db, tenants, people, keys = world
    await _run_as_schema_owner(db, MIGRATION)
    before = await db.fetchval("SELECT count(*) FROM attribution_backfill_095")
    await _run_as_schema_owner(db, MIGRATION)

    assert await db.fetchval("SELECT count(*) FROM attribution_backfill_095") == before
    assert await _who(db, tenants, keys, "single", "tasks") == people["alice"]
