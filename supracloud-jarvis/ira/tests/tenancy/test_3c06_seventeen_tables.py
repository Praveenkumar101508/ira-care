"""3C-06 — the denial properties, on all seventeen tables rather than one.

Batch 5C proved that workspace A cannot read, insert into, update, delete from
or upsert across into workspace B. Every one of those proofs was written against
**`memory_embeddings`** (plus `edges` for the foreign-key case). Seventeen tables
carry the policies; one was adversarially tested.

That gap is not hypothetical, and `049` does not close it. `049` asserts that
each table has exactly four policies and that both settings appear in each arm —
a statement about `pg_policies`, which is a statement about **what was written
down**. It cannot distinguish a policy that denies from a policy that is
syntactically present and semantically wrong, and a loop over a table list is
exactly the kind of code that silently omits an entry.

So this file re-derives the denials from the outside, per table, through
`ira_app`, using the three-principal matrix:

    A: tenant A, workspace A     writes the row
    B: tenant A, workspace B     the same tenant — only the workspace differs
    C: tenant B, workspace C     a different tenant entirely

`B` is the interesting principal. Everything scoped by tenant lets B through, so
B is what finds a workspace predicate somebody left off one table in a loop.

── On the shape of these tests ─────────────────────────────────────────────

The handle is the row's own primary key, and the update attempted is
`SET workspace_id = <mine>` — the move attack. Both are chosen so the test needs
no per-table knowledge of content columns, which means adding an eighteenth
table to `WORKSPACE_SCOPED` gets it tested rather than getting it a TODO.

Reads that come back empty and writes that raise are different failure modes and
are asserted differently. An UPDATE or DELETE whose `USING` clause matches
nothing reports `UPDATE 0` with no error at all, so those are checked by having
**A read the row back** afterwards — asserting on the command tag would pass
against a policy that let the write through and then rolled it back, and would
also pass against one that never matched anything for any reason.
"""
from __future__ import annotations

import datetime
import os
import uuid
from dataclasses import dataclass, field

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

from tests.tenancy.adversarial import app_pool, matrix  # noqa: E402,F401

from core.identity.db_context import workspace_connection  # noqa: E402

#: The tables carrying `049`'s strict workspace predicate. Kept as a literal so
#: a table dropped from the migration's loop shows up here as a failing test
#: rather than as a shorter list nobody reads.
#:
#: `briefings` is the eighteenth, added by `081` (P12-03 finding G) rather than
#: by `049`. It is adversarially tested here with the other seventeen because
#: its predicate is IDENTICAL to theirs — the distinction that matters to this
#: file is the policy, not which migration wrote it.
WORKSPACE_SCOPED = (
    "agents", "briefings", "calendar_events", "companion_versions",
    "heartbeat_surfaced",
    "commitments", "companions", "conversations", "decisions", "edges",
    "entities", "files", "lifeos_events", "memory_embeddings", "messages",
    "people", "projects", "reminders", "tasks",
    # IRA CARE (099): fifteen more, every one tenant AND workspace scoped.
    "care_people", "care_circle_members", "care_devices", "care_vitals",
    "care_safety_settings", "care_checkins", "care_alerts", "care_alert_attempts",
    "care_acknowledgements", "care_routines", "care_medications",
    "care_medication_events", "care_transcripts", "care_consents", "care_access_log",
    "care_reports", "care_conversation_memory", "care_device_events", "care_push_tokens", "care_push_tickets",
    "care_visits", "care_visit_tasks", "care_communication_profiles", "care_reminders", "care_user_preferences",
)


@dataclass(frozen=True)
class Spec:
    """How to put one minimal row into one table.

    `columns` holds only what the table requires and cannot default.
    `tenant_id` and `workspace_id` are deliberately **absent** from every spec:
    both default from the transaction GUCs, and letting them default is the
    property under test. A spec that named them would be testing that the test
    can write a literal.
    """

    handle: str = "id"
    columns: dict = field(default_factory=dict)
    #: Tables whose rows must exist first, and the column each satisfies.
    parents: tuple = ()


def _t(_run: str) -> str:
    return f"3c06-{_run}"


SPECS: dict[str, Spec] = {
    "agents": Spec(columns={"name": "text"}),
    # `content` is the whole finding: the aggregated summary that was
    # readable by every authenticated user in the deployment.
    "briefings": Spec(columns={"briefing_type": "text", "content": "text"}),
    "heartbeat_surfaced": Spec(handle="id",
                               columns={"item_key": "text", "kind": "text"}),
    "calendar_events": Spec(columns={"title": "text", "start_at": "now",
                                     "end_at": "now"}),
    "commitments": Spec(columns={"user_id": "text", "statement": "text"}),
    "companions": Spec(columns={"user_id": "user"}),
    "companion_versions": Spec(columns={"user_id": "user", "version": "int",
                                        "snapshot": "json"},
                               parents=(("companions", "companion_id"),)),
    "conversations": Spec(columns={"session_id": "text"}),
    "decisions": Spec(columns={"summary": "text", "review_at": "now"}),
    "entities": Spec(columns={"type": "text", "name": "text"}),
    "edges": Spec(columns={"relation": "text"},
                  parents=(("entities", "src_entity_id"),
                           ("entities", "dst_entity_id"))),
    "files": Spec(columns={"user_id": "text", "filename": "text",
                           "storage_path": "text", "sha256": "text"}),
    # No uuid primary key — `seq` is a bigint sequence, so the handle is `kind`,
    # which is required, free-form and unique per run.
    "lifeos_events": Spec(handle="kind",
                          columns={"user_id": "text", "kind": "handle"}),
    "memory_embeddings": Spec(columns={"source_type": "text", "content": "text"}),
    "messages": Spec(columns={"role": "role", "content": "text"},
                     parents=(("conversations", "conversation_id"),)),
    "people": Spec(columns={"person_name": "text", "relationship": "text"}),
    "projects": Spec(columns={"name": "text", "slug": "text"}),
    "reminders": Spec(columns={"title": "text", "remind_at": "now"}),
    "tasks": Spec(columns={"title": "text"}),
    # IRA CARE (099). `literal:` names an enumerated value a CHECK constraint
    # requires; parents are resolved recursively (an attempt needs an alert,
    # which needs a person).
    "care_people": Spec(columns={"display_name": "text"}),
    "care_circle_members": Spec(columns={"display_name": "text", "role": "literal:family"},
                                parents=(("care_people", "person_id"),)),
    "care_devices": Spec(parents=(("care_people", "person_id"),)),
    "care_vitals": Spec(columns={"kind": "literal:steps", "value": "int", "recorded_at": "now"},
                        parents=(("care_people", "person_id"),)),
    "care_safety_settings": Spec(handle="person_id", parents=(("care_people", "person_id"),)),
    "care_checkins": Spec(columns={"scheduled_at": "now", "question_kind": "literal:meal"},
                          parents=(("care_people", "person_id"),)),
    "care_alerts": Spec(columns={"kind": "literal:manual", "severity": "literal:notice"},
                        parents=(("care_people", "person_id"),)),
    "care_alert_attempts": Spec(columns={"tier": "int", "channel": "literal:push",
                                         "outcome": "literal:logged"},
                                parents=(("care_alerts", "alert_id"),)),
    "care_acknowledgements": Spec(columns={"kind": "literal:on_my_way"},
                                  parents=(("care_alerts", "alert_id"),)),
    "care_routines": Spec(columns={"name": "text", "kind": "literal:meal"},
                          parents=(("care_people", "person_id"),)),
    "care_medications": Spec(columns={"name": "text"},
                             parents=(("care_people", "person_id"),)),
    "care_medication_events": Spec(columns={"due_at": "now"},
                                   parents=(("care_medications", "medication_id"),
                                            ("care_people", "person_id"))),
    "care_transcripts": Spec(columns={"speaker": "literal:person", "text": "text"},
                             parents=(("care_people", "person_id"),)),
    "care_consents": Spec(columns={"kind": "literal:data_retention", "granted": "bool",
                                   "granted_by": "literal:person"},
                          parents=(("care_people", "person_id"),)),
    "care_access_log": Spec(columns={"resource_kind": "literal:vitals"}),
    "care_reports": Spec(columns={"kind": "literal:daily_carer", "period_start": "date", "period_end": "date",
                                  "title": "text", "body_text": "text"},
                         parents=(("care_people", "person_id"),)),
    "care_conversation_memory": Spec(columns={"day": "date"}, parents=(("care_people", "person_id"),)),
    "care_device_events": Spec(columns={"idempotency_key": "text", "kind": "literal:help"},
                               parents=(("care_people", "person_id"), ("care_devices", "device_id"))),
    "care_push_tokens": Spec(columns={"user_id": "user", "token": "text"},
                             parents=(("care_people", "person_id"), ("care_circle_members", "member_id"))),
    "care_push_tickets": Spec(parents=(("care_alerts", "alert_id"),)),
    "care_visits": Spec(columns={"planned_start": "now", "planned_end": "later"}, parents=(("care_people", "person_id"),)),
    "care_visit_tasks": Spec(columns={"title": "text"}, parents=(("care_visits", "visit_id"),)),
    "care_communication_profiles": Spec(parents=(("care_people", "person_id"),)),
    "care_reminders": Spec(columns={"due_at": "now", "text": "text"}, parents=(("care_people", "person_id"),)),
    "care_user_preferences": Spec(columns={"user_id": "user", "appearance": "json"}),
}


def _value(kind: str, run: str, handle, actor):
    """One column's value, by the spec's shorthand.

    Text values are unique **per call**, not per run. `entities` has a unique
    index on (scope, type, name) and `edges` needs two of them, so a value that
    was merely unique per run collided with itself. `projects.slug` is unique
    per tenant for the same reason.
    """
    if kind == "text":
        return f"3c06-{run}-{uuid.uuid4().hex[:8]}"
    if kind == "handle":
        return handle
    if kind == "user":
        # A real user row: `companions.user_id` carries a foreign key into
        # `users`, so a fresh UUID is rejected before RLS is ever consulted.
        return actor.user_id
    if kind == "role":
        return "user"  # messages_role_check
    if kind == "uuid":
        return uuid.uuid4()
    if kind == "int":
        return 1
    if kind == "json":
        return "{}"
    if kind == "now":
        return "__now__"
    if kind == "later":
        return "__later__"
    if kind == "bool":
        return True
    if kind == "date":
        return datetime.date(2026, 11, 21)
    if kind.startswith("literal:"):
        return kind.split(":", 1)[1]
    raise AssertionError(f"unknown column kind {kind!r}")


async def _insert(conn, table: str, run: str, actor, *, handle=None, extra=None):
    """Insert one row and return its handle value.

    `workspace_id` is deliberately never passed: it defaults from the bound
    transaction's GUC, and the `WITH CHECK` arm then verifies what the default
    produced. Letting it default is the mechanism Batch 5B built.

    `tenant_id` is passed, because seven of the seventeen require it — those
    tables predate the GUC default and have no default expression for it. It
    makes no difference to what is being proved: the same `WITH CHECK` compares
    whatever arrives against `app.tenant_id`, so a wrong value is refused rather
    than accepted.
    """
    spec = SPECS[table]
    handle = handle if handle is not None else (
        f"3c06-{run}-{uuid.uuid4().hex[:8]}" if spec.handle != "id" else uuid.uuid4())

    cols, vals, params = ["tenant_id"], [], [actor.tenant_id]
    vals.append("$1")
    if spec.handle == "id":
        cols.append("id")
        params.append(handle)
        vals.append(f"${len(params)}")

    for name, kind in spec.columns.items():
        value = _value(kind, run, handle, actor)
        cols.append(name)
        if value == "__now__":
            vals.append("now()")
            continue
        if value == "__later__":
            vals.append("now() + interval '1 hour'")
            continue
        params.append(value)
        vals.append(f"${len(params)}"
                    + ("::jsonb" if kind == "json" else ""))

    for name, value in (extra or {}).items():
        cols.append(name)
        params.append(value)
        vals.append(f"${len(params)}")

    await conn.execute(  # noqa: S608 - table names come from a module constant
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(vals)})",
        *params)
    if spec.handle != "id" and extra and spec.handle in extra:
        # The row is keyed by its parent (care_safety_settings: one per person).
        return extra[spec.handle]
    return handle


async def _insert_with_parents(conn, table: str, run: str, actor, *, extra=None):
    """`_insert`, with the row's parents created first, recursively."""
    spec = SPECS[table]
    extra = dict(extra or {})
    for parent_table, column in spec.parents:
        if column not in extra:
            extra[column] = await _insert_with_parents(conn, parent_table, run, actor)
    return await _insert(conn, table, run, actor, extra=extra)


async def _row_in(actor, table: str, run: str):
    """Create whatever `table` needs, in `actor`'s workspace, and return the
    handle of the row under test."""
    async with workspace_connection(actor.principal()) as conn:
        return await _insert_with_parents(conn, table, run, actor)


async def _visible(actor, table: str, handle) -> list:
    spec = SPECS[table]
    async with workspace_connection(actor.principal()) as conn:
        return await conn.fetch(  # noqa: S608
            f"SELECT {spec.handle} FROM {table} WHERE {spec.handle} = $1", handle)


@pytest.fixture
def run() -> str:
    return uuid.uuid4().hex[:8]


# ─────────────────────────────────────────────────────────────────────────────
# The row exists at all — the control every denial below depends on
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("table", WORKSPACE_SCOPED)
async def test_the_author_can_read_back_what_it_wrote(matrix, app_pool, table, run):
    """Without this, every denial in this file is satisfied by a broken INSERT.

    That is not a theoretical worry: it is precisely what happened in Batch 5C,
    where a 384-wide embedding against a `vector(1024)` column made every insert
    fail inside a non-fatal `except` and the vault looked empty. A suite of
    "nobody can see it" assertions passes beautifully when nothing was ever
    written.
    """
    handle = await _row_in(matrix.a, table, run)

    assert await _visible(matrix.a, table, handle), (
        f"{table}: the author cannot read back its own row, so every denial "
        f"assertion for this table is vacuous")


# ─────────────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("table", WORKSPACE_SCOPED)
async def test_the_neighbouring_workspace_cannot_read_it(matrix, app_pool, table, run):
    """Same tenant, different workspace.

    The one that matters. B shares A's tenant, so every tenant-scoped predicate
    in the database admits B — only the workspace arm refuses. A table whose
    policy lost that arm is invisible to `023`'s tenant tests and to `049`'s
    policy-count assertion, and visible only here.
    """
    handle = await _row_in(matrix.a, table, run)

    assert await _visible(matrix.b, table, handle) == [], (
        f"{table}: workspace B read a row written in workspace A. Both are the "
        f"same tenant, so the workspace arm of this table's SELECT policy is "
        f"missing or not being applied.")


@pytest.mark.parametrize("table", WORKSPACE_SCOPED)
async def test_the_other_tenant_cannot_read_it(matrix, app_pool, table, run):
    """The tenant arm, in case a workspace fix dropped it.

    A workspace id is an unguessable UUIDv4, which is an argument about
    difficulty rather than authorisation. C turns it into a test.
    """
    handle = await _row_in(matrix.a, table, run)

    assert await _visible(matrix.c, table, handle) == [], (
        f"{table}: another tenant read the row")


@pytest.mark.parametrize("table", WORKSPACE_SCOPED)
async def test_an_unbound_connection_reads_nothing(matrix, app_pool, table, run):
    """No GUCs at all: the predicate is NULL, which is not true, so the row is
    denied. Fail-closed by construction rather than by a check somebody wrote —
    but only if `FORCE` is on, which is what this actually verifies."""
    handle = await _row_in(matrix.a, table, run)
    spec = SPECS[table]

    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(  # noqa: S608
            f"SELECT {spec.handle} FROM {table} WHERE {spec.handle} = $1", handle)
    finally:
        await conn.close()

    assert rows == [], f"{table}: an unbound connection read the row"


# ─────────────────────────────────────────────────────────────────────────────
# Write
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("table", WORKSPACE_SCOPED)
async def test_a_workspace_cannot_insert_into_another(matrix, app_pool, table, run):
    """Naming somebody else's workspace on the way in.

    `WITH CHECK` is the only thing between this and a row that its author cannot
    see and its host never asked for. A write fails loudly where a read fails
    silently, so this one asserts on the exception.
    """
    spec = SPECS[table]
    async with workspace_connection(matrix.a.principal()) as conn:
        extra = {"workspace_id": matrix.b.workspace_id}
        for parent_table, column in spec.parents:
            extra[column] = await _insert_with_parents(conn, parent_table, run, matrix.a)

        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await _insert(conn, table, run, matrix.a, extra=extra)


@pytest.mark.parametrize("table", WORKSPACE_SCOPED)
async def test_a_workspace_cannot_update_another_workspaces_row(
    matrix, app_pool, table, run
):
    """Unqualified, and that is the entire point of the test.

    PostgreSQL applies **SELECT** policies to an `UPDATE` or `DELETE` that
    references any column of the table — a `WHERE id = $1` is enough. So a
    qualified statement is filtered by the SELECT policy before the UPDATE
    policy is ever consulted, and reports `UPDATE 0` whether the UPDATE policy
    is correct or absent. It cannot distinguish them.

    An unqualified statement touches only the write policy. It also affects B's
    own rows, which is harmless — the assertion is about A's.
    """
    handle = await _row_in(matrix.a, table, run)

    async with workspace_connection(matrix.b.principal()) as conn:
        try:
            await conn.execute(  # noqa: S608
                f"UPDATE {table} SET workspace_id = $1", matrix.b.workspace_id)
        except asyncpg.exceptions.InsufficientPrivilegeError:
            pass  # refused loudly is a fine answer

    assert await _visible(matrix.a, table, handle), (
        f"{table}: workspace B moved A's row into itself — A can no longer see "
        f"a row it wrote. The UPDATE policy's USING or WITH CHECK is missing "
        f"its workspace arm.")
    assert await _visible(matrix.b, table, handle) == [], (
        f"{table}: workspace B now sees the row it tried to steal")


@pytest.mark.parametrize("table", WORKSPACE_SCOPED)
async def test_a_workspace_cannot_delete_another_workspaces_row(
    matrix, app_pool, table, run
):
    """Unqualified, for the reason given above.

    This is not a hypothetical refinement. Dropping the workspace arm from
    `files`' DELETE policy was caught by **nothing** while this test used
    `WHERE <handle> = $1`, and is caught immediately without it. Batch 5C's
    equivalent test used `WHERE content = ...` and had the same blind spot: it
    proved the SELECT policy twice and the DELETE policy never.
    """
    handle = await _row_in(matrix.a, table, run)

    async with workspace_connection(matrix.b.principal()) as conn:
        try:
            await conn.execute(f"DELETE FROM {table}")  # noqa: S608
        except asyncpg.exceptions.InsufficientPrivilegeError:
            pass  # refused loudly is a fine answer (no DELETE grant at all)

    assert await _visible(matrix.a, table, handle), (
        f"{table}: workspace B deleted a row belonging to workspace A")


@pytest.mark.parametrize("table", WORKSPACE_SCOPED)
async def test_the_other_tenant_cannot_delete_it_either(matrix, app_pool, table, run):
    """The same operation across the tenant boundary.

    Kept separate from the workspace case because they are enforced by different
    arms of the same predicate, and a change satisfying one while breaking the
    other would look like a passing suite.
    """
    handle = await _row_in(matrix.a, table, run)

    async with workspace_connection(matrix.c.principal()) as conn:
        try:
            await conn.execute(f"DELETE FROM {table}")  # noqa: S608
        except asyncpg.exceptions.InsufficientPrivilegeError:
            pass  # refused loudly is a fine answer (no DELETE grant at all)

    assert await _visible(matrix.a, table, handle), (
        f"{table}: another tenant deleted the row")


# ─────────────────────────────────────────────────────────────────────────────
# The list itself
# ─────────────────────────────────────────────────────────────────────────────


#: Tables that carry a `workspace_id` column but deliberately have **no**
#: workspace predicate in any policy, with the reason. Not an allowance list —
#: each entry is a claim, and the test below fails if one stops being true.
#: Tables whose policies DO read `app.workspace_id` but which are not among the
#: seventeen content tables, because their predicate is a different one and the
#: denial properties in this file do not apply to them unchanged.
WORKSPACE_AWARE_BY_DESIGN = {
    # 3C-10. `notifications` is tenant-scoped with an OPTIONAL workspace:
    #   tenant_id = app.tenant_id
    #   AND (workspace_id IS NULL OR workspace_id = app.workspace_id)
    # A tenant-level alert has a NULL workspace and is legitimately visible from
    # every workspace of its tenant, so `test_the_neighbouring_workspace_cannot
    # _read_it` would fail against it — correctly. Its adversarial proof lives
    # in tests/tenancy/test_3c10_unscoped_tables.py, which tests both halves.
    "notifications": "tenant-scoped with an optional workspace — see 3C-10",
    # 089 (F1). The nine evidence tables use the same tenant AND workspace
    # predicate, but the runtime role holds no DELETE on any of them and only
    # column-level UPDATE on some, so the generic DELETE/UPDATE probes here
    # would fail on privilege rather than on isolation. Their read, write,
    # cross-workspace, cross-tenant and unbound-connection denials are proved
    # in tests/tenancy/test_evidence_store.py.
    "evidence_artifacts": "append-only evidence — see test_evidence_store.py",
    "evidence_artifact_versions": "append-only evidence — see test_evidence_store.py",
    "evidence_claims": "append-only evidence — see test_evidence_store.py",
    "evidence_output_spans": "append-only evidence — see test_evidence_store.py",
    "evidence_dependencies": "append-only evidence — see test_evidence_store.py",
    "evidence_patches": "append-only evidence — see test_evidence_store.py",
    "evidence_approvals": "append-only evidence — see test_evidence_store.py",
    "evidence_artifact_access": "append-only evidence — see test_evidence_store.py",
    "evidence_audit_events": "append-only evidence — see test_evidence_store.py",
}

COLUMN_CARRIERS_BY_DESIGN = {
    # The picker reads this table to find out which workspaces exist. A
    # workspace-bound policy here would mean you must already have selected a
    # workspace in order to learn which ones you may select. It is tenant-scoped
    # plus own-user instead, which is the correct boundary for it.
    "workspace_memberships":
        "a workspace cannot gate the table that lists the workspaces",
    # Tenant-scoped audit of biometric checks. Its `workspace_id` is nullable
    # with no default, so nothing populates it — the column is vestigial rather
    # than load-bearing, and pretending otherwise by adding a policy over an
    # always-NULL column would deny everything.
    "biometric_audit":
        "tenant-scoped audit; workspace_id is nullable, defaulted by nothing",
}


async def test_every_workspace_scoped_table_is_covered_here(matrix, app_pool):
    """The list above is a literal, and a literal drifts.

    So it is checked against the database. The criterion is **a policy that
    reads `app.workspace_id`**, not merely a `workspace_id` column: those are
    different claims, and two tables carry the column on purpose without the
    predicate. Using the column as the criterion would force them into a suite
    that would then be asserting the wrong thing about them.

    An eighteenth genuinely workspace-scoped table fails this on the day the
    migration lands, which is the only day anyone would remember why.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        enforcing = {r["relname"] for r in await conn.fetch(
            """
            SELECT DISTINCT c.relname
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
              JOIN pg_policy p ON p.polrelid = c.oid
             WHERE c.relkind = 'r' AND c.relrowsecurity AND c.relforcerowsecurity
               AND (coalesce(pg_get_expr(p.polqual, p.polrelid), '')
                        LIKE '%app.workspace_id%'
                 OR coalesce(pg_get_expr(p.polwithcheck, p.polrelid), '')
                        LIKE '%app.workspace_id%')
            """)}
        carriers = {r["relname"] for r in await conn.fetch(
            """
            SELECT c.relname
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
              JOIN pg_attribute a ON a.attrelid = c.oid
                                 AND a.attname = 'workspace_id'
                                 AND NOT a.attisdropped
             WHERE c.relkind = 'r' AND c.relrowsecurity AND c.relforcerowsecurity
            """)}
    finally:
        await conn.close()

    listed = set(WORKSPACE_SCOPED)

    missing = sorted(enforcing - listed - set(WORKSPACE_AWARE_BY_DESIGN))
    assert not missing, (
        f"these tables enforce a workspace predicate and are not adversarially "
        f"tested: {missing}. Add them to WORKSPACE_SCOPED and give each a Spec — "
        f"or, if their predicate differs from the seventeen's, to "
        f"WORKSPACE_AWARE_BY_DESIGN with a pointer to where they ARE proved.")

    unproved = sorted(set(WORKSPACE_AWARE_BY_DESIGN) - enforcing)
    assert not unproved, (
        f"{unproved} are listed as workspace-aware but no policy on them reads "
        f"app.workspace_id. Either the migration was reverted or the entry is "
        f"stale — an entry claiming a boundary that is not there is worse than "
        f"no entry.")

    stale = sorted(listed - enforcing)
    assert not stale, (
        f"these tables are listed here but no longer have a policy reading "
        f"app.workspace_id: {stale}. If a migration removed the protection, "
        f"that IS the finding — do not delete the entry to make this pass.")

    unexplained = sorted(carriers - enforcing - set(COLUMN_CARRIERS_BY_DESIGN))
    assert not unexplained, (
        f"these tables carry a workspace_id column under forced RLS but no "
        f"policy reads it: {unexplained}. A column nobody checks is not a "
        f"boundary. Either give it a policy or record why it has none in "
        f"COLUMN_CARRIERS_BY_DESIGN.")

    now_enforcing = sorted(set(COLUMN_CARRIERS_BY_DESIGN) & enforcing)
    assert not now_enforcing, (
        f"{now_enforcing} now enforce a workspace predicate, so the reason "
        f"recorded for them in COLUMN_CARRIERS_BY_DESIGN is out of date. Move "
        f"them into WORKSPACE_SCOPED with a Spec.")

    assert listed == set(SPECS), (
        f"WORKSPACE_SCOPED and SPECS disagree: {sorted(listed ^ set(SPECS))}")
