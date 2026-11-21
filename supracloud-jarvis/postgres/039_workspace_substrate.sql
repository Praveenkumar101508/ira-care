-- =============================================================================
-- 039_workspace_substrate.sql — Phase 3B Batch 2
--
-- Completes the workspace substrate: every tenant gets an organisation and
-- exactly one active default workspace, the workspace lifecycle becomes
-- self-consistent, and the invariant is verifiable rather than assumed.
--
-- ── What Batch 0 left unfinished ─────────────────────────────────────────────
--
-- `033` created `organisations` and `workspaces` and attached one of each to
-- the single seeded tenant, by fixed UUID. That is correct for the install this
-- branch started from and insufficient for a multi-tenant one: a tenant created
-- by any other route has no organisation and no workspace, and nothing detects
-- it. Batch 5 will point content at `workspace_id`, and content whose workspace
-- does not exist is not a migration problem — it is data loss.
--
-- So this migration does three things: it backfills every tenant that lacks
-- substrate, it gives the platform a single provisioning entry point so a new
-- tenant cannot be created without one, and it adds a view that answers "is any
-- tenant missing substrate" as a query rather than as a belief.
--
-- ── On R-40, honestly ────────────────────────────────────────────────────────
--
-- R-40 warns that a default-workspace backfill "collapses any pre-existing
-- structure into one workspace per tenant". That risk is **not** realised here,
-- and the reason is worth stating rather than leaving to inference: no content
-- table carries `workspace_id` yet, so there is no pre-existing structure to
-- collapse. This migration creates containers and moves nothing.
--
-- R-40 becomes live in Batch 5, which assigns existing content to a workspace.
-- That is where a real grouping — if a customer ever had one — would be
-- flattened, and that is where the risk has to be answered. Creating the
-- default workspace now does not pre-judge it: a tenant may hold any number of
-- workspaces, and `is_default` marks where unassigned content lands, not the
-- only place content may live.
--
-- ── Determinism ──────────────────────────────────────────────────────────────
--
-- Backfilled identifiers are UUIDv5 over `IRA_UUID_NAMESPACE`, so re-running
-- produces the same ids and a second run inserts nothing. The private tenant's
-- organisation and workspace keep the fixed UUIDs `033` gave them — the
-- `WHERE NOT EXISTS` guards below skip any tenant that already has substrate,
-- so this migration never creates a second one or renames the first.
--
-- Rollback: rollback/039_workspace_substrate_down.sql
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 0. Backfill window
--
--    See `035_memberships.sql` section 0 for the full reasoning. In short: this
--    migration binds no `app.tenant_id` because it acts on every tenant, so
--    under FORCE ROW LEVEL SECURITY every statement below would match zero rows
--    — silently — and the verification view would then report a substrate that
--    was never created. FORCE is restored in section 7.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE organisations NO FORCE ROW LEVEL SECURITY;
ALTER TABLE workspaces    NO FORCE ROW LEVEL SECURITY;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Workspace lifecycle constraints
--
--    `033` gave `workspaces` a `status` and an `archived_at` with nothing
--    keeping them in step, and an `is_default` that an archive could leave
--    pointing at a dead workspace.
-- ─────────────────────────────────────────────────────────────────────────────

-- Repair before constraining: a row already inconsistent would make the
-- constraint unaddable, and failing here with "check constraint is violated by
-- some row" tells an operator nothing about which invariant was wrong.
UPDATE workspaces SET archived_at = NOW()
 WHERE status = 'archived' AND archived_at IS NULL;
UPDATE workspaces SET archived_at = NULL
 WHERE status = 'active' AND archived_at IS NOT NULL;
UPDATE workspaces SET is_default = FALSE
 WHERE is_default AND status = 'archived';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'workspaces_archived_at_agrees') THEN
        ALTER TABLE workspaces ADD CONSTRAINT workspaces_archived_at_agrees
            CHECK ((status = 'archived') = (archived_at IS NOT NULL));
    END IF;

    -- An archived default is the shape that breaks "where does unassigned
    -- content go". The partial unique index in 033 permits at most one ACTIVE
    -- default, so without this an archived row could keep `is_default` set and
    -- a tenant would appear to have two.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'workspaces_archived_is_not_default') THEN
        ALTER TABLE workspaces ADD CONSTRAINT workspaces_archived_is_not_default
            CHECK (NOT (is_default AND status = 'archived'));
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Substrate backfill — every tenant, not just the seeded one
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO organisations (id, tenant_id, name, slug)
SELECT uuid_generate_v5('6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35'::uuid,
                        'organisation:' || t.id::text),
       t.id,
       t.name,
       NULL                     -- slug is globally unique; never invented here
  FROM tenants t
 WHERE NOT EXISTS (SELECT 1 FROM organisations o WHERE o.tenant_id = t.id);

-- A slug is a customer-facing identifier that may end up in a URL. Deriving one
-- from a tenant name would either collide globally or leak the name into a
-- public identifier. It stays NULL until a customer chooses it.

INSERT INTO workspaces (id, tenant_id, organisation_id, name, slug, is_default, status)
SELECT uuid_generate_v5('6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35'::uuid,
                        'workspace:default:' || t.id::text),
       t.id,
       o.id,
       'Default',
       'default',
       TRUE,
       'active'
  FROM tenants t
  JOIN organisations o ON o.tenant_id = t.id
 WHERE NOT EXISTS (
        SELECT 1 FROM workspaces w
         WHERE w.tenant_id = t.id AND w.is_default AND w.status = 'active');

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. The provisioning entry point
--
--    A tenant created without substrate is a tenant whose first content write
--    has nowhere to land. This function is the one place that creates it, so
--    the platform's tenant-creation path has something to call and cannot get
--    the ordering subtly wrong.
--
--    **It is not a trigger on `tenants`.** A trigger would have to insert into
--    two FORCE-RLS tables from whatever context created the tenant, and that
--    context is a platform one with no `app.tenant_id` bound — so the insert
--    would be refused. Making the trigger set the GUC would clobber the
--    caller's transaction context for every later statement, which is a far
--    worse failure than the one it fixes.
--
--    Tenant creation is a platform operation, like the migration itself. This
--    function belongs to that path and is refused to everyone else.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION provision_tenant_substrate(p_tenant UUID)
RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    v_org       UUID;
    v_workspace UUID;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM tenants WHERE id = p_tenant) THEN
        RAISE EXCEPTION 'provision_tenant_substrate: tenant % does not exist', p_tenant
            USING HINT = 'Create the tenant first. This function does not create one, '
                         'because a typo in a tenant id would otherwise silently '
                         'manufacture a tenant nobody asked for.';
    END IF;

    SELECT id INTO v_org FROM organisations WHERE tenant_id = p_tenant;
    IF v_org IS NULL THEN
        v_org := uuid_generate_v5('6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35'::uuid,
                                  'organisation:' || p_tenant::text);
        INSERT INTO organisations (id, tenant_id, name)
        SELECT v_org, p_tenant, t.name FROM tenants t WHERE t.id = p_tenant
        ON CONFLICT (tenant_id) DO NOTHING;
        SELECT id INTO v_org FROM organisations WHERE tenant_id = p_tenant;
    END IF;

    SELECT id INTO v_workspace
      FROM workspaces
     WHERE tenant_id = p_tenant AND is_default AND status = 'active';
    IF v_workspace IS NULL THEN
        v_workspace := uuid_generate_v5('6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35'::uuid,
                                        'workspace:default:' || p_tenant::text);
        INSERT INTO workspaces (id, tenant_id, organisation_id, name, slug, is_default, status)
        VALUES (v_workspace, p_tenant, v_org, 'Default', 'default', TRUE, 'active')
        ON CONFLICT (id) DO NOTHING;
        SELECT id INTO v_workspace
          FROM workspaces
         WHERE tenant_id = p_tenant AND is_default AND status = 'active';
    END IF;

    RETURN v_workspace;
END
$$;

COMMENT ON FUNCTION provision_tenant_substrate(UUID) IS
    'Idempotent. Creates the organisation and default workspace for a tenant and '
    'returns the default workspace id. Platform operation — EXECUTE is revoked '
    'from PUBLIC. Deliberately not a trigger: see the note in 039.';

-- The application role has no business provisioning a tenant.
REVOKE ALL ON FUNCTION provision_tenant_substrate(UUID) FROM PUBLIC;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. The invariant, enforced where it can be
--
--    `033`'s partial unique index already guarantees AT MOST one active default
--    per tenant. The other half — at least one — cannot be an index, because it
--    is a statement about rows that are absent.
--
--    A DEFERRABLE constraint trigger is the right tool: it is checked at COMMIT,
--    so promoting a new default and archiving the old one in the same
--    transaction is allowed, while ending a transaction with a tenant that has
--    workspaces but no active default is not.
--
--    It deliberately says nothing about a tenant with NO workspaces at all —
--    that is the un-provisioned state, which section 5's view reports and which
--    a constraint here would turn into an error at tenant-creation time, before
--    `provision_tenant_substrate` has had a chance to run.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION workspaces_require_one_default()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_tenant   UUID := COALESCE(NEW.tenant_id, OLD.tenant_id);
    v_total    INTEGER;
    v_defaults INTEGER;
BEGIN
    SELECT count(*), count(*) FILTER (WHERE is_default AND status = 'active')
      INTO v_total, v_defaults
      FROM workspaces WHERE tenant_id = v_tenant;

    -- No workspaces at all is the un-provisioned state, not a violation.
    IF v_total = 0 THEN
        RETURN NULL;
    END IF;

    IF v_defaults <> 1 THEN
        RAISE EXCEPTION
            'tenant % has % active default workspaces; exactly one is required',
            v_tenant, v_defaults
            USING HINT = 'Promote another workspace to default in the same '
                         'transaction. This check is deferred to COMMIT so that '
                         'swapping the default is a single atomic change.';
    END IF;
    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS workspaces_one_default_check ON workspaces;
CREATE CONSTRAINT TRIGGER workspaces_one_default_check
    AFTER INSERT OR UPDATE OR DELETE ON workspaces
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION workspaces_require_one_default();

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Verification view — counts only, and it must read zero
-- ─────────────────────────────────────────────────────────────────────────────

-- **`security_invoker` is load-bearing, not a style choice.** A view defaults to
-- running as its OWNER, and the owner is subject to FORCE ROW LEVEL SECURITY
-- like anybody else. Without this setting the view evaluates with the migration
-- role's (unbound) context, matches zero rows, and reports
-- `contract_ready = true` on a table full of plaintext — to every caller,
-- including a superuser, because the caller's privileges never enter into it.
-- That was the state of this view as originally written; it was found by
-- reading it as a non-superuser owner.
CREATE OR REPLACE VIEW workspace_substrate_status
WITH (security_invoker = true) AS
SELECT
    (SELECT count(*) FROM tenants)                                   AS tenants,
    (SELECT count(*) FROM tenants t
      WHERE NOT EXISTS (SELECT 1 FROM organisations o
                         WHERE o.tenant_id = t.id))                  AS tenants_without_organisation,
    (SELECT count(*) FROM tenants t
      WHERE NOT EXISTS (SELECT 1 FROM workspaces w
                         WHERE w.tenant_id = t.id
                           AND w.is_default AND w.status = 'active')) AS tenants_without_default_workspace,
    (SELECT count(*) FROM (
        SELECT tenant_id FROM organisations GROUP BY tenant_id HAVING count(*) > 1
     ) x)                                                            AS tenants_with_multiple_organisations,
    (SELECT count(*) FROM workspaces
      WHERE is_default AND status = 'archived')                      AS archived_defaults;

COMMENT ON VIEW workspace_substrate_status IS
    'Every column must read 0. A non-zero tenants_without_default_workspace is a '
    'tenant whose first content write in Batch 5 would have nowhere to land.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Convenience: the caller''s default workspace
--
--    Batch 5 will need "which workspace does unassigned content belong to" in
--    several places. One definition, here, rather than the same subquery copied
--    into every migration that needs it.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION default_workspace_id(p_tenant UUID)
RETURNS UUID
LANGUAGE sql
STABLE
AS $$
    SELECT id FROM workspaces
     WHERE tenant_id = p_tenant AND is_default AND status = 'active'
$$;

COMMENT ON FUNCTION default_workspace_id(UUID) IS
    'The tenant''s active default workspace, or NULL if it has none. NULL is a '
    'real answer — an un-provisioned tenant — and callers must treat it as one '
    'rather than as a missing row to invent.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 7. Closing the backfill window
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE organisations FORCE ROW LEVEL SECURITY;
ALTER TABLE workspaces    FORCE ROW LEVEL SECURITY;

COMMENT ON COLUMN workspaces.is_default IS
    'Where unassigned content lands. Exactly one active default per tenant, '
    'enforced by a partial unique index (at most one) and a deferred constraint '
    'trigger (at least one). It does not mean "the only workspace".';
