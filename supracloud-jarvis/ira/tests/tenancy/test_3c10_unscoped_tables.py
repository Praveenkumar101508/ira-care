"""3C-10 — a table with no tenancy is a table nothing is looking at.

3C-03 found that every notification went to one hardcoded Redis channel and that
`/ws/notifications` forwarded it to any client with a decodable token. It fixed
the channel.

The same payload — the same `title` and `body` — is also **persisted**, and read
back by `GET /api/v1/notifications`:

    SELECT id, category, title, body, priority, channels_sent, read, created_at
      FROM notifications
     ORDER BY created_at DESC LIMIT $1

No tenant predicate. No tenant column to write one against. No RLS. The route
authorised with `require_auth` and opened a bare connection, so every
authenticated user in every tenant read every notification in the deployment.

And not only read:

    UPDATE notifications SET read=TRUE WHERE id=$1   -- another tenant's row
    DELETE FROM notifications WHERE read=TRUE        -- every tenant's rows

The DELETE has no predicate beyond `read`. One authenticated call cleared the
deployment.

── Why every guard built so far was blind to it ────────────────────────────

Because they all enumerate tables that already have tenancy:

  * the workspace binding ratchet scans the seventeen workspace-scoped tables;
  * 3C-06's coverage check looks for tables with a `workspace_id` column;
  * the RLS audits enumerate tables that HAVE policies.

A table with none of those is in none of those sets. **A table with no tenancy
is not a table with weak tenancy — it is a table that does not appear in the
inventory at all.** That is why this file's last section is an inventory of
every table with RLS disabled, classified, rather than another check over the
tables already known to be scoped.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys
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

_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    _st.SentenceTransformer = object
    _st.CrossEncoder = object

from tests.tenancy.adversarial import app_pool, matrix  # noqa: E402,F401

from core.identity.db_context import (  # noqa: E402
    tenant_connection,
    workspace_connection,
)

IRA = pathlib.Path(__file__).resolve().parents[2]


async def _notify_row(actor, title: str, *, workspace=None) -> uuid.UUID:
    """One notification row, written through the narrowest binding that works.

    A tenant-level row (NULL workspace) can be written on a tenant binding. A
    workspace-scoped one cannot: `WITH CHECK` requires `workspace_id =
    app.workspace_id`, and a tenant connection binds no workspace — so the
    insert is refused. That is the policy behaving correctly, and it is worth
    naming here because the obvious helper (always use `tenant_connection`)
    fails on exactly one of the two cases and looks like a policy bug.
    """
    notif_id = uuid.uuid4()
    connection = (workspace_connection(actor.principal()) if workspace is not None
                  else tenant_connection(actor.principal()))
    async with connection as conn:
        await conn.execute(
            "INSERT INTO notifications (id, tenant_id, workspace_id, category,"
            "                           title, body) "
            "VALUES ($1, $2, $3, 'briefing', $4, 'the owner''s own content')",
            notif_id, actor.tenant_id, workspace, title)
    return notif_id


async def _visible_to(actor, notif_id) -> list:
    async with workspace_connection(actor.principal()) as conn:
        return await conn.fetch(
            "SELECT title, body FROM notifications WHERE id = $1", notif_id)


# ─────────────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────────────


async def test_another_tenant_cannot_read_a_notification(matrix, app_pool):
    """The finding, in one assertion.

    `body` for a briefing is the owner's own content and for the security
    monitor is their security events. This was returned to every authenticated
    caller in the deployment.
    """
    notif_id = await _notify_row(matrix.a, "A's briefing")

    assert await _visible_to(matrix.c, notif_id) == [], (
        "another tenant read a notification's title and body")


async def test_the_authors_tenant_can_read_it(matrix, app_pool):
    """The control. Without it every denial here is satisfied by a broken
    INSERT — the failure mode Batch 5C hit with a mis-sized embedding."""
    notif_id = await _notify_row(matrix.a, "A's briefing")

    assert await _visible_to(matrix.a, notif_id), (
        "the tenant cannot read back its own notification, so the denials in "
        "this file prove nothing")


async def test_a_tenant_level_notification_is_visible_from_any_workspace(
    matrix, app_pool
):
    """Why the predicate is not the seventeen tables' predicate.

    A billing warning or a security alert belongs to the tenant, not to a
    workspace, so `workspace_id` is NULL. Copying the content tables' predicate
    (`workspace_id = app.workspace_id`) would make every such notification
    permanently invisible, because NULL never equals anything — a bug that
    reads as a stricter, therefore safer, policy.
    """
    notif_id = await _notify_row(matrix.a, "tenant-wide alert", workspace=None)

    assert await _visible_to(matrix.a, notif_id), "invisible from workspace A"
    assert await _visible_to(matrix.b, notif_id), (
        "a tenant-level notification is invisible from the tenant's other "
        "workspace — the policy is treating NULL as a workspace to match")


async def test_a_workspace_scoped_notification_stays_in_its_workspace(
    matrix, app_pool
):
    """And the other half: one that DOES name a workspace is confined to it."""
    notif_id = await _notify_row(
        matrix.a, "workspace-only", workspace=matrix.a.workspace_id)

    assert await _visible_to(matrix.a, notif_id)
    assert await _visible_to(matrix.b, notif_id) == [], (
        "a workspace-scoped notification leaked to the tenant's other workspace")


async def test_an_unbound_connection_reads_nothing(matrix, app_pool):
    """No GUCs: the predicate is NULL, which is not true. Fail-closed by
    construction — but only under FORCE, which is what this verifies."""
    notif_id = await _notify_row(matrix.a, "unreachable")

    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(
            "SELECT title FROM notifications WHERE id = $1", notif_id)
    finally:
        await conn.close()

    assert rows == []


# ─────────────────────────────────────────────────────────────────────────────
# Write — the part that was worse than a read
# ─────────────────────────────────────────────────────────────────────────────


async def test_another_tenant_cannot_mark_it_read(matrix, app_pool):
    """`UPDATE ... WHERE id=$1` took the id straight from the URL.

    Unqualified, per 3C-06: a `WHERE id = $1` is filtered by the SELECT policy
    before the UPDATE policy is consulted, so it would report `UPDATE 0` whether
    the UPDATE policy were correct or absent.
    """
    notif_id = await _notify_row(matrix.a, "still unread")

    async with workspace_connection(matrix.c.principal()) as conn:
        await conn.execute("UPDATE notifications SET read = TRUE")

    async with workspace_connection(matrix.a.principal()) as conn:
        row = await conn.fetchrow(
            "SELECT read FROM notifications WHERE id = $1", notif_id)

    assert row is not None and row["read"] is False, (
        "another tenant marked this notification read")


async def test_another_tenant_cannot_clear_it(matrix, app_pool):
    """The sharpest one.

    `DELETE FROM notifications WHERE read=TRUE` has no predicate beyond `read`.
    On an unbound connection that is every tenant's read notifications, removed
    by one authenticated call to `DELETE /api/v1/notifications/read`.
    """
    notif_id = await _notify_row(matrix.a, "read and kept")
    async with workspace_connection(matrix.a.principal()) as conn:
        await conn.execute("UPDATE notifications SET read = TRUE")

    async with workspace_connection(matrix.c.principal()) as conn:
        await conn.execute("DELETE FROM notifications WHERE read = TRUE")

    assert await _visible_to(matrix.a, notif_id), (
        "another tenant deleted this tenant's notifications")


async def test_the_delete_policy_itself_refuses_another_tenant(matrix, app_pool):
    """Unqualified, and the test above cannot replace it.

    3C-06 established that PostgreSQL applies SELECT policies to a `DELETE` that
    references any column — and the route's statement references `read`. So the
    test above exercises the route faithfully and is filtered by the SELECT
    policy before the DELETE policy is consulted: it passes whether the DELETE
    policy is correct or `USING (true)`.

    Verified, not assumed: replacing the DELETE policy with `USING (true)` was
    caught by **nothing** until this test existed. The route is defended twice
    over, which is good; the second defence still has to be tested on its own.
    """
    notif_id = await _notify_row(matrix.a, "A keeps this")

    async with workspace_connection(matrix.c.principal()) as conn:
        await conn.execute("DELETE FROM notifications")

    assert await _visible_to(matrix.a, notif_id), (
        "the DELETE policy admits another tenant — only the SELECT policy was "
        "standing between a cross-tenant delete and the data")


async def test_a_notification_cannot_be_written_into_another_tenant(
    matrix, app_pool
):
    """`WITH CHECK`, so a caller cannot address a row into somebody else."""
    async with tenant_connection(matrix.a.principal()) as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO notifications (id, tenant_id, category, title, body)"
                " VALUES ($1, $2, 'system', 'planted', 'x')",
                uuid.uuid4(), matrix.c.tenant_id)


async def test_a_notification_cannot_name_another_tenants_workspace(
    matrix, app_pool
):
    """The composite foreign key, not the policy.

    `notifications_workspace_in_tenant_fkey` spans (tenant_id, workspace_id).
    Without it a row could name its own tenant and a workspace belonging to
    another — legal against a single-column FK, and exactly the shape `046` had
    to fix on the content tables.
    """
    async with tenant_connection(matrix.a.principal()) as conn:
        with pytest.raises((asyncpg.exceptions.ForeignKeyViolationError,
                            asyncpg.exceptions.InsufficientPrivilegeError)):
            await conn.execute(
                "INSERT INTO notifications (id, tenant_id, workspace_id,"
                "                           category, title, body) "
                "VALUES ($1, $2, $3, 'system', 'planted', 'x')",
                uuid.uuid4(), matrix.a.tenant_id, matrix.c.workspace_id)


# ─────────────────────────────────────────────────────────────────────────────
# The route reads through a bound connection
# ─────────────────────────────────────────────────────────────────────────────


def test_the_notification_routes_do_not_open_bare_connections():
    """The policies are useless if the handler never binds a tenant.

    A bare `acquire()` here reads as ordinary code — it is what was there, in
    all three handlers — and RLS on an unbound connection denies everything
    rather than leaking, so the symptom would be an empty list, not an error.
    Which is to say: the wrong direction of failure, quietly.
    """
    source = (IRA / "api" / "routes" / "notifications.py").read_text(
        encoding="utf-8")

    assert not re.search(r"(?<![\w.])acquire\(", source), (
        "api/routes/notifications.py opens a bare connection again. The table "
        "carries RLS now, so this does not leak — it silently returns nothing, "
        "which is the failure mode somebody 'fixes' by removing the policy.")
    assert "workspace_connection" in source
    assert source.count("require_workspace_principal") >= 2, (
        "a notification REST handler no longer resolves a principal")


def test_the_writer_binds_a_tenant_too():
    source = (IRA / "worker" / "notifier.py").read_text(encoding="utf-8")
    persist = source[source.index("async def _persist"):]

    assert "tenant_connection" in persist, (
        "worker/notifier.py writes notification rows on an unbound connection")
    assert "EDITION_CLOUD" in persist, (
        "the writer no longer refuses an unattributable notification in cloud. "
        "notify() raises earlier when the channel cannot be named, but that is "
        "one ordering change away from not happening.")


# ─────────────────────────────────────────────────────────────────────────────
# The inventory — the guard that would have caught this
# ─────────────────────────────────────────────────────────────────────────────

#: Every table with RLS **disabled**, and why that is acceptable.
#:
#: This is the set no other guard enumerates, which is how `notifications` — a
#: table of customer content read with no predicate — stayed invisible through
#: Batch 5C and three Phase 3C findings about the very payload it stores.
#:
#: An entry here is a claim that the table holds no customer content. Adding one
#: to silence a failure is the failure.
RLS_DISABLED_BY_DESIGN = {
    # Deployment-wide catalogues and configuration. Read-only reference data
    # with no customer content in it.
    "avatar_catalogue": "static catalogue",
    "personality_presets": "static catalogue",
    "voice_catalogue": "static catalogue",
    "model_providers": "provider catalogue",
    # 067. Platform-authored model fabric configuration: which model
    # versions may serve, and which artefacts may be fetched. No tenant
    # column and no customer content — a tenant-scoped registry would let
    # a tenant register its own model, which is a Phase 6+ capability with
    # a licence and a supply-chain question attached. The boundary here is
    # the GRANT: ira_app reads and cannot write, so the process that
    # CHECKS the allowlist cannot add to it.
    "model_registry": "model fabric configuration (MOD-03)",
    "model_pull_allowlist": "model fabric configuration (MOD-02)",
    # Same class, same boundary, and the GRANT matters more here than on either
    # of the two above: a runtime role that could UPDATE the licence register
    # could record its own approval. 068 revokes INSERT/UPDATE/DELETE
    # explicitly, because 042's ALTER DEFAULT PRIVILEGES would otherwise have
    # granted them — the P5-01 finding, which shipped on 067.
    "model_licence_register": "model licence evidence and determinations (MOD-01)",
    # Phase 7. Neither holds dataset CONTENT — only metadata, digests and the
    # determinations a named human recorded — so neither holds customer data and
    # neither needs a tenant column. The boundary is the GRANT again: ira_app
    # reads, so a process that answers requests cannot certify its own training
    # data or record a check that never ran.
    "training_datasets": "training dataset governance metadata (MOD-04)",
    "dataset_checks": "PII/secret/contamination/licence check records (MOD-04)",
    # Neither holds dataset content or weights — run metadata and digests only.
    # The GRANT is again the boundary: ira_app reads, so a request-handling
    # process cannot approve its own training run (PP-04).
    "model_training_runs": "training run records and their approval (MOD-04)",
    "training_run_datasets": "which governed datasets a run consumes (MOD-04)",
    "realtime_capability_profiles": "capability catalogue",
    "tool_policies": "deployment policy configuration",
    # P10-04. Two rows describing where THIS DEPLOYMENT's Trust Ledger stopped
    # being one global chain, written once at migration time. There is no
    # tenant to scope it to — the boundary is deployment-wide, and a per-tenant
    # copy would let one tenant's answer to "which chain model is this seq in"
    # differ from another's for the same seq. It holds two integers, an era
    # name and a note; no customer content can reach it, because nothing writes
    # it at runtime: `ira_app` holds SELECT and nothing else (077).
    "trust_ledger_chain_eras": "where the ledger chain model changed (P10-04)",
    # ── ADR-0010's backfill record (095) ─────────────────────────────────────
    #
    # The Group B rows 095's single-user backfill attributed, and to whom, so
    # rollback/095 clears exactly those authors. Table names, row keys and user
    # ids: no content. Written once, by the migration; the runtime roles hold no
    # privilege on it (095 revokes them on every run).
    "attribution_backfill_095": "migration bookkeeping for ADR-0010's backfill (095)",
    # ── Phase 11's plan catalogue (078) ──────────────────────────────────────
    #
    # Three deployment-wide tables, unscoped for the same reason
    # `trust_ledger_chain_eras` is: every tenant sees the same answer, and a
    # per-tenant copy would let two tenants disagree about what the product's
    # own plans are.
    #
    # None can hold customer content. `billing_plans` holds a plan id, a display
    # name and a description; `billing_plan_prices` holds a currency, an integer
    # of minor units, a recurrence and who set it; `plan_entitlements` holds a
    # key and an integer limit. All three are length- or pattern-bounded by CHECK
    # constraints so none is somewhere a prompt could sit, and `ira_app` holds
    # SELECT and nothing else on all three (078) — so nothing at runtime can
    # write to them at all.
    #
    # WHO is subscribed is a different question and IS tenant-scoped:
    # `tenant_subscriptions` and `tenant_entitlement_overrides` carry tenant_id
    # under FORCE RLS.
    "billing_plans": "the deployment's plan catalogue (P11-01)",
    "billing_plan_prices": "owner-set prices, append-only (P11-01)",
    "plan_entitlements": "what each plan entitles you to (P11-01)",
    # ── IRA CARE OS releases (120) ───────────────────────────────────────────
    #
    # The catalogue of OS images the update server may offer (E4.d): one per
    # build fingerprint, with its rollback index, payload hash and size, and the
    # rollout percentage. Deployment-wide for the same reason as the plan
    # catalogue above: every paired watch must get the same answer about what a
    # release is, and a per-tenant copy would let two tenants disagree about the
    # firmware itself. No customer content can reach it (every column is a
    # bounded identifier, hash or number). `ira_app` holds SELECT and nothing
    # else (120 revokes the rest); releases are published by the operator script
    # scripts/os_release.py as the migrator. WHICH device asked, and what it was
    # told, is answered under the device's own tenant scope, not here.
    "os_releases": "the deployment's OS image catalogue for over-the-air updates (E4.d)",
    # ── Phase 11's payment intake (080) ─────────────────────────────────────
    #
    # `payment_events` is unscoped BY NECESSITY, and this is the one entry in
    # this register where that phrase is load-bearing rather than a formality.
    #
    # A webhook is an unauthenticated HTTP request from the internet. When the
    # bytes land, NOTHING has established which tenant they concern — the
    # provider's customer reference has not been resolved and may resolve to
    # nobody. Under FORCE RLS an INSERT with `app.tenant_id` unset does not
    # error; it is REFUSED BY THE POLICY. A tenant-scoped intake table would
    # therefore silently drop exactly the events that most need investigating.
    #
    # It carries NO tenant_id column, which is what makes it honestly unscoped
    # rather than a scoped table with its protection missing. The table can hold
    # a provider's payload, so it is not "somewhere no customer content can
    # reach" the way `trust_ledger_chain_eras` is — but every column is
    # length- or pattern-bounded, no row exists unless its signature verified
    # first, and `ira_app` cannot DELETE (080's trigger refuses outright).
    #
    # The MAPPING is not here and is not a table: `tenants.provider_customer_ref`
    # is a column on the tenancy root, guarded by a trigger so the runtime can
    # read it before scope exists but cannot write it for anybody.
    "payment_events": "inbound payment webhook intake, before a tenant is known (P11-04)",
    "schema_migrations": "migration bookkeeping",
    "checkpoint_migrations": "LangGraph's schema versions for its checkpoint tables (097)",
    # The tenancy roots themselves. `tenants` cannot be tenant-scoped without
    # becoming unreadable, and `users` is keyed by membership, not by tenant.
    "tenants": "the tenancy root",
    "users": "identities, scoped through the membership tables",
    # ── Known-unscoped, holding owner content. Recorded, not accepted. ───────
    # These are 3C-10's remaining work. Each is reachable only through an
    # owner-gated route, and `is_owner` refuses in cloud (3C-04), so none is
    # reachable by a second tenant today. That is a mitigation, not a boundary,
    # and the count below must fall rather than rise.
    # `briefings` LEFT this list in `081` (P12-03 finding G): it now carries
    # tenant_id and workspace_id, both NOT NULL, with FORCE RLS and the strict
    # `049` predicate.
    #
    # Worth recording why it should not have been on the list under this
    # heading's stated mitigation. The comment above says each of these is
    # "reachable only through an owner-gated route, and `is_owner` refuses in
    # cloud (3C-04), so none is reachable by a second tenant today". That was
    # NOT true of briefings: `GET /api/v1/briefing/latest` took `require_auth`
    # alone, the router is mounted with no dependencies, and no owner gate
    # existed anywhere on the path. The debt was recorded WITH a mitigation
    # that did not hold for this table.
    #
    # The seven below still carry that claim. P12-03 did not re-verify it for
    # them, and one counter-example is reason enough to check the rest rather
    # than assume — recorded as follow-on work, not as a passing remark.
    "command_runs": "OWNER CONTENT — owner-gated; needs tenancy (3C-10 follow-up)",
    "access_audit_log": "OWNER CONTENT — owner-gated; needs tenancy (3C-10 follow-up)",
    "security_events": "OWNER CONTENT — owner-gated; needs tenancy (3C-10 follow-up)",
    "owner_profile": "OWNER CONTENT — owner-gated; needs tenancy (3C-10 follow-up)",
    "monitor_state": "OWNER CONTENT — owner-gated; needs tenancy (3C-10 follow-up)",
    # `heartbeat_surfaced` LEFT this list in `082` (P12-03b, finding H): it now
    # carries tenant_id and workspace_id with FORCE RLS and the strict
    # predicate, and its uniqueness is scoped rather than global.
    "connector_scopes": "OWNER CONTENT — owner-gated; needs tenancy (3C-10 follow-up)",
    # ── Customer content behind a server-built key, not a policy (097). ──────
    # The three exceptions to this register's rule above, said plainly: these
    # DO hold customer content. They are LangGraph's conversation-graph state
    # (message history, injected vault memories), and LangGraph issues its own
    # queries without session variables, so a policy keyed on `app.*` would hide
    # every row from it. What separates one person's rows from another's is the
    # thread_id, which the server builds as `ira:t:{tenant}:w:{workspace}:u:{user}:s:`
    # before the client's suffix (3C-01, ADR-0009); erasure deletes by it.
    #
    # They existed before 097 wherever LangGraph's own `setup()` could run; 097
    # moved their creation into the chain, which is what made them visible here.
    # They are kept apart from OWNER CONTENT, which means "owner-gated", and
    # counted by their own ratchet below, so neither number can hide the other.
    "checkpoints": "CUSTOMER CONTENT — server-built thread_id prefix, no RLS possible (3C-01, 097)",
    "checkpoint_blobs": "CUSTOMER CONTENT — server-built thread_id prefix, no RLS possible (3C-01, 097)",
    "checkpoint_writes": "CUSTOMER CONTENT — server-built thread_id prefix, no RLS possible (3C-01, 097)",
}

#: How many unscoped tables still hold owner content. A ratchet: it comes down
#: as each is given tenancy, and it must never go up.
MAX_UNSCOPED_CONTENT = 6

#: How many tables hold customer content isolated only by the server-built
#: thread_id prefix. LangGraph's three (097). A ratchet of its own: it must never
#: go up, and it comes down only if their isolation moves into the database.
MAX_PREFIX_ISOLATED_CONTENT = 3


async def test_every_table_without_rls_is_accounted_for(matrix, app_pool):
    """The inventory nothing else keeps.

    A new table with no RLS fails this on the day it is created, which is the
    only day anyone can cheaply say whether it holds customer content.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        rows = await conn.fetch(
            """
            SELECT c.relname
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
             WHERE c.relkind = 'r' AND NOT c.relrowsecurity
             ORDER BY 1
            """)
    finally:
        await conn.close()

    unscoped = {r["relname"] for r in rows}
    listed = set(RLS_DISABLED_BY_DESIGN)

    new = sorted(unscoped - listed)
    assert not new, (
        f"these tables have no Row-Level Security and are not in the "
        f"inventory: {new}.\n\n"
        f"Decide what each holds. If it is customer content it needs a tenant "
        f"and a policy — `notifications` sat here holding notification titles "
        f"and bodies, readable by every tenant, through all of Batch 5C. Adding "
        f"an entry to silence this is the failure it exists to prevent.")

    fixed = sorted(listed - unscoped)
    assert not fixed, (
        f"{fixed} now have RLS — remove them from RLS_DISABLED_BY_DESIGN and "
        f"lower MAX_UNSCOPED_CONTENT so the inventory keeps meaning something.")


def test_the_unscoped_content_count_only_falls():
    """A ratchet, like the workspace binding ratchet before it.

    Eight tables still hold owner content with no tenancy. Each is behind an
    owner gate that refuses in cloud, so none is reachable by a second tenant
    today — a mitigation, not a boundary. This is what stops that number
    drifting upward while the mitigation is mistaken for a fix.
    """
    content = [t for t, why in RLS_DISABLED_BY_DESIGN.items()
               if why.startswith("OWNER CONTENT")]

    assert len(content) <= MAX_UNSCOPED_CONTENT, (
        f"{len(content)} unscoped tables now hold owner content, above the "
        f"ceiling of {MAX_UNSCOPED_CONTENT}: {sorted(content)}. The ceiling "
        f"comes down as each is given tenancy; it does not go up.")


def test_the_prefix_isolated_content_count_only_falls():
    """The CUSTOMER CONTENT entries are a stated exception, so they are counted.

    A fourth table filed under that label, to quiet the inventory test above,
    fails here; so does an entry for a table whose isolation is not the
    3C-01 thread prefix.
    """
    content = sorted(t for t, why in RLS_DISABLED_BY_DESIGN.items()
                     if why.startswith("CUSTOMER CONTENT"))

    assert len(content) <= MAX_PREFIX_ISOLATED_CONTENT, (
        f"{len(content)} tables now hold customer content isolated only by a key "
        f"prefix, above the ceiling of {MAX_PREFIX_ISOLATED_CONTENT}: {content}. "
        f"A new table holding customer content needs a tenant and a policy.")
    assert set(content) <= {"checkpoints", "checkpoint_blobs", "checkpoint_writes"}, (
        f"{content}: only LangGraph's checkpoint tables are isolated by the 3C-01 "
        f"thread prefix; anything else under this label is unprotected content.")


def test_notifications_is_no_longer_in_the_inventory():
    """The finding, closed, stated where a reader will look for it."""
    assert "notifications" not in RLS_DISABLED_BY_DESIGN, (
        "notifications is back in the unscoped inventory — 051 has been "
        "reverted or never applied")
