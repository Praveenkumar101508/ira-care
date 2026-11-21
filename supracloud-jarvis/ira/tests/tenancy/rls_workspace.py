"""Bind a connection to a tenant AND a workspace, for the RLS suites.

`049` made the seventeen workspace-scoped tables require both settings:

    tenant_id    = current_setting('app.tenant_id')
AND workspace_id = current_setting('app.workspace_id')

Before it, binding the tenant alone was enough to read and write them. After
it, a tenant-only connection reads zero rows and cannot insert at all — which
is the point, and which is also why every RLS fixture that wrote content
through a tenant-only binding stopped working on the same day.

That fallout is not a problem with those tests. They exist to prove **tenant**
isolation, and tenant isolation is still exactly what they prove; they simply
now have to say which of the tenant's workspaces they are operating in, the
same as any other caller. This module is the one place that gets written, so
the five suites do not each grow their own slightly different version.

── Why the workspace is created here rather than provisioned ────────────────

`provision_tenant_substrate` is the real path and these fixtures cannot use it:
`039` revoked EXECUTE from PUBLIC and granted it to `ira_schema_owner` and
`ira_migrator` only, while these suites connect as `ira_app` — deliberately,
because "RLS keeps the other tenant out" is a claim about a non-superuser
connection and proving it on a privileged one would prove nothing.

So the rows are created directly, through the same policies everything else
goes through. That is not a bypass: an `ira_app` connection bound to a tenant
may create that tenant's own organisation and workspace, and if it could not,
this helper would fail rather than quietly reach around the boundary.
"""
from __future__ import annotations

import uuid

#: Deterministic per tenant, so a fixture that runs twice against the same
#: database converges instead of accumulating workspaces — and so `039`'s
#: "exactly one active default workspace per tenant" trigger is not tripped by
#: a second fixture inserting a second default.
_NAMESPACE = uuid.UUID("6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35")


def default_workspace_id(tenant: uuid.UUID | str) -> uuid.UUID:
    """The id `provision_tenant_substrate` would give this tenant's default.

    Same namespace and same name, so a fixture-created workspace and a
    provisioned one are the same row rather than two rows racing the
    one-default-per-tenant trigger.
    """
    return uuid.uuid5(_NAMESPACE, f"workspace:default:{tenant}")


def organisation_id(tenant: uuid.UUID | str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"organisation:{tenant}")


def ensure_workspace(cur, tenant: uuid.UUID | str) -> uuid.UUID:
    """Create (idempotently) and return this tenant's default workspace.

    `cur` is a psycopg2 cursor whose connection is already bound to `tenant` —
    the inserts go through `workspaces`' and `organisations`' tenant policies,
    so an unbound cursor gets a policy violation rather than a row in the wrong
    tenant.
    """
    # Look before inserting. `provision_tenant_substrate` may already have run
    # for this tenant in another suite, and 046 made the workspace slug unique
    # per tenant — so an `ON CONFLICT (id)` insert of the deterministic id
    # collides on `workspaces_tenant_slug_key` instead and raises. Reading first
    # is also the honest shape: the question is "does this tenant have a default
    # workspace", not "can I create one".
    cur.execute(
        "SELECT id FROM workspaces WHERE tenant_id = %s AND is_default AND status = 'active'",
        (str(tenant),),
    )
    existing = cur.fetchone()
    if existing is not None:
        return existing[0]

    org = organisation_id(tenant)
    ws = default_workspace_id(tenant)
    cur.execute(
        "INSERT INTO organisations (id, tenant_id, name) VALUES (%s, %s, 'Fixture') "
        "ON CONFLICT DO NOTHING",
        (str(org), str(tenant)),
    )
    # Read the id back rather than assuming ours won: `organisations` is unique
    # per tenant, so an earlier fixture's row is the one that exists.
    cur.execute("SELECT id FROM organisations WHERE tenant_id = %s", (str(tenant),))
    org = cur.fetchone()[0]
    # No conflict target: the row can collide on its id OR on (tenant_id, slug),
    # and either means somebody else got there first, which is fine.
    cur.execute(
        "INSERT INTO workspaces (id, tenant_id, organisation_id, name, slug, "
        "                        is_default, status) "
        "VALUES (%s, %s, %s, 'Default', 'default', TRUE, 'active') "
        "ON CONFLICT DO NOTHING",
        (str(ws), str(tenant), str(org)),
    )
    cur.execute(
        "SELECT id FROM workspaces WHERE tenant_id = %s AND is_default AND status = 'active'",
        (str(tenant),),
    )
    row = cur.fetchone()
    assert row is not None, (
        f"tenant {tenant} has no default workspace after the fixture tried to "
        f"create one. Every workspace-scoped write below would fail a policy "
        f"check with no obvious cause, so this stops here instead."
    )
    return row[0]


def bind(cur, tenant: uuid.UUID | str, workspace: uuid.UUID | str | None = None):
    """Bind `cur`'s session to a tenant and a workspace.

    `false` for the `is_local` argument, matching what these suites already do:
    they run with `autocommit = True`, where a transaction-local setting would
    be discarded before the next statement.
    """
    cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))
    if workspace is None:
        workspace = ensure_workspace(cur, tenant)
    cur.execute("SELECT set_config('app.workspace_id', %s, false)", (str(workspace),))
    return workspace


def unbind(cur) -> None:
    """Clear both settings.

    Both, deliberately. A test that cleared only the tenant would still be
    carrying a workspace, and "unbound" would mean something different from
    what it says — which is precisely the confusion the fail-closed direction is
    supposed to remove.
    """
    cur.execute("SELECT set_config('app.tenant_id', '', false)")
    cur.execute("SELECT set_config('app.workspace_id', '', false)")


__all__ = [
    "bind",
    "unbind",
    "ensure_workspace",
    "default_workspace_id",
    "organisation_id",
]
