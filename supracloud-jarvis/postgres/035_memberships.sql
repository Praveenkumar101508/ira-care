-- =============================================================================
-- 035_memberships.sql — Phase 3B Batch 0C
--
-- The permanent authorisation relationship, and the compatibility path that
-- retires `delegated_users` without deleting it.
--
--     users → tenant_memberships → tenants/organisations → workspaces
--
-- ── Evolving, not replacing ──────────────────────────────────────────────────
--
-- `023_tenancy.sql` already created `tenant_memberships` with `tenant_id`,
-- `user_id`, `role`, `created_at`, `revoked_at`, forced RLS and a tenant policy.
-- The shape the brief asks for is that table plus lifecycle fields, so this
-- migration adds columns rather than creating a second membership table. Two
-- tables meaning "membership" is how an authorisation check ends up reading the
-- wrong one.
--
-- ── Why `status` when `revoked_at` exists ────────────────────────────────────
--
-- `revoked_at IS NULL` expresses two states. Real membership has at least four:
-- invited (no access yet), active, suspended (access denied, membership kept),
-- removed. Encoding suspension as "revoked" loses the distinction between *this
-- person is temporarily blocked* and *this person is no longer a member*, and
-- the two have different reinstatement semantics.
--
-- `status` is derived from `revoked_at` on backfill so the existing column stays
-- truthful, and a CHECK keeps the pair consistent from here on. `revoked_at`
-- remains for compatibility with existing queries — this is an expand, and no
-- reader is broken by it.
--
-- ── Access denial is a property of the row, not of the reader ────────────────
--
-- `membership_grants_access` is a generated column, so "does this membership
-- grant access?" is answered by the database in one place rather than by every
-- call site remembering to check both `status` and `revoked_at`. A call site
-- that forgets is the entire class of bug this replaces.
--
-- ── delegated_users ──────────────────────────────────────────────────────────
--
-- Legacy compatibility only (CLOUD-ADR-0006). It is NOT deleted here, NOT
-- migrated here, and must not be extended as the permanent authorisation model.
-- It gains `user_id` and `tenant_id` so it can be read alongside the new model
-- during the transition, resolved strictly through `legacy_identity_map` —
-- a row whose username did not resolve gets NO user_id and is reported by the
-- quarantine tooling rather than being attached to somebody.
--
-- Rollback: rollback/035_memberships_down.sql
-- ── No psql metacommands in this file ────────────────────────────────────────
--
-- The UUIDs below are written as literals rather than `\set` variables, and
-- that is deliberate. `utils/migrations.py` — the durable upgrade path — applies
-- migrations through **asyncpg**, which is a PostgreSQL protocol driver and not
-- psql: it rejects `\set` with `syntax error at or near "\"`.
--
-- Migrations 023 and 031 use `\set` and are therefore rejected by that runner
-- (recorded as R-45). They apply only because docker-entrypoint-initdb.d runs
-- them through psql on a brand-new volume. This file must not inherit that.
--
-- =============================================================================


-- ─────────────────────────────────────────────────────────────────────────────
-- 0. The backfill window — why FORCE is briefly lifted
--
--    **A data migration cannot read the rows it is migrating while FORCE ROW
--    LEVEL SECURITY applies to the role running it.**
--
--    `FORCE ROW LEVEL SECURITY` means the table OWNER is subject to the policy
--    like anybody else. Migrations run as the owner and bind no `app.tenant_id`
--    — they are not acting for a tenant, they are acting on all of them. So
--    every policy predicate evaluates `tenant_id = NULL`, which is NULL rather
--    than true, and the statement matches **zero rows**.
--
--    An UPDATE that matches zero rows does not fail. It reports success. The
--    first symptom is the statement AFTER it:
--
--        ERROR: column "status" of relation "tenant_memberships"
--               contains null values
--
--    which is `SET NOT NULL` failing on rows the backfill was supposed to have
--    filled. That is exactly how this was found — and it was found only after
--    the verification database stopped running migrations as a superuser.
--    A superuser bypasses RLS entirely, so under the original harness this
--    migration passed while being unable to migrate any real data.
--
--    A per-tenant loop does not work either: the query that would discover the
--    tenants is itself filtered to nothing.
--
--    So FORCE is lifted for the backfill and restored at the end of the file.
--    Deliberately `NO FORCE` and **not** `DISABLE`:
--
--      * RLS stays ENABLED throughout, so the application role is never exempt
--        for even an instant — only the owner is, and only inside this
--        migration;
--      * `DISABLE` would expose every row to every role, and
--        `test_migration_hygiene.py` correctly refuses it in an auto-run
--        migration;
--      * the whole file runs in one transaction, so a failure rolls the
--        exemption back with everything else.
--
--    `test_no_migration_leaves_force_row_level_security_off` asserts every
--    `NO FORCE` in this directory has a matching `FORCE` in the same file.
--
--    **This exposes a deployment requirement that was previously implicit, and
--    it is not a comfortable one.** `utils/migrations.py` runs the chain
--    through the ordinary application pool, so the application role must own
--    these tables to perform DDL at all — and an owner can lift FORCE whenever
--    it likes. FORCE therefore constrains the application's *queries*, not a
--    compromised application role. Splitting the migration role from the
--    runtime role is the fix, and it is a deployment change rather than a
--    migration: recorded as **R-50** and **D-18**.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE tenant_memberships NO FORCE ROW LEVEL SECURITY;
ALTER TABLE workspaces         NO FORCE ROW LEVEL SECURITY;
ALTER TABLE organisations      NO FORCE ROW LEVEL SECURITY;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Membership lifecycle
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE tenant_memberships ADD COLUMN IF NOT EXISTS status       TEXT;
ALTER TABLE tenant_memberships ADD COLUMN IF NOT EXISTS invited_by   UUID;
ALTER TABLE tenant_memberships ADD COLUMN IF NOT EXISTS joined_at    TIMESTAMPTZ;
ALTER TABLE tenant_memberships ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ;
ALTER TABLE tenant_memberships ADD COLUMN IF NOT EXISTS removed_at   TIMESTAMPTZ;
ALTER TABLE tenant_memberships ADD COLUMN IF NOT EXISTS updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Derive status from the existing revoked_at so no row is left unknown.
UPDATE tenant_memberships
   SET status = CASE WHEN revoked_at IS NOT NULL THEN 'removed' ELSE 'active' END
 WHERE status IS NULL;

UPDATE tenant_memberships
   SET joined_at = created_at
 WHERE joined_at IS NULL AND status = 'active';

UPDATE tenant_memberships
   SET removed_at = revoked_at
 WHERE removed_at IS NULL AND revoked_at IS NOT NULL;

ALTER TABLE tenant_memberships ALTER COLUMN status SET DEFAULT 'invited';
ALTER TABLE tenant_memberships ALTER COLUMN status SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'tenant_memberships_status_check') THEN
        ALTER TABLE tenant_memberships ADD CONSTRAINT tenant_memberships_status_check
            CHECK (status IN ('invited', 'active', 'suspended', 'removed'));
    END IF;

    -- Roles are validated, not free text. An unrecognised role string in an
    -- authorisation check is indistinguishable from a typo, and a typo that
    -- silently grants nothing is as bad as one that silently grants everything.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'tenant_memberships_role_check') THEN
        ALTER TABLE tenant_memberships ADD CONSTRAINT tenant_memberships_role_check
            CHECK (role IN ('primary_owner', 'owner', 'admin', 'member',
                            'viewer', 'billing_admin', 'security_admin'));
    END IF;

    -- status and revoked_at must agree. Without this they drift the first time
    -- somebody updates one and not the other.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'tenant_memberships_status_revoked_agree') THEN
        ALTER TABLE tenant_memberships ADD CONSTRAINT tenant_memberships_status_revoked_agree
            CHECK ((status = 'removed') = (revoked_at IS NOT NULL));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'tenant_memberships_invited_by_fk') THEN
        ALTER TABLE tenant_memberships ADD CONSTRAINT tenant_memberships_invited_by_fk
            FOREIGN KEY (invited_by) REFERENCES users(id) ON DELETE SET NULL;
    END IF;
END
$$;

-- The single place "does this membership grant access?" is answered.
ALTER TABLE tenant_memberships
    ADD COLUMN IF NOT EXISTS membership_grants_access BOOLEAN
    GENERATED ALWAYS AS (status = 'active' AND revoked_at IS NULL) STORED;

COMMENT ON COLUMN tenant_memberships.membership_grants_access IS
    'Generated. The ONLY correct access predicate. Checking status or '
    'revoked_at individually is how a suspended member keeps access.';

CREATE INDEX IF NOT EXISTS idx_tenant_memberships_user_active
    ON tenant_memberships (user_id) WHERE membership_grants_access;

-- The primary owner is unique per tenant, and only among memberships that
-- actually grant access — a removed former owner must not block a new one.
CREATE UNIQUE INDEX IF NOT EXISTS tenant_memberships_one_primary_owner
    ON tenant_memberships (tenant_id)
    WHERE role = 'primary_owner' AND membership_grants_access;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. workspace_memberships — workspace access is not implied by tenant access
--
--    A tenant may hold "Engineering" and "Legal"; membership of the tenant does
--    not mean membership of both. Modelled now so Batch 5's content policies
--    have something to check.
-- ─────────────────────────────────────────────────────────────────────────────

-- Created inside the backfill window, so section 3 can populate it. FORCE is
-- applied to it in section 5 with the others.
CREATE TABLE IF NOT EXISTS workspace_memberships (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id)     ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(id)  ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES users(id)       ON DELETE CASCADE,
    role         TEXT NOT NULL DEFAULT 'member'
                 CHECK (role IN ('workspace_admin', 'member', 'viewer')),
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('invited', 'active', 'suspended', 'removed')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    removed_at   TIMESTAMPTZ,
    UNIQUE (workspace_id, user_id)
);

-- A workspace membership cannot span tenants: the workspace and the membership
-- must agree on which tenant they belong to, enforced by a composite FK rather
-- than by a query that has to remember.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'workspace_memberships_same_tenant') THEN
        ALTER TABLE workspaces
            ADD CONSTRAINT workspaces_tenant_id_id_key UNIQUE (tenant_id, id);
        ALTER TABLE workspace_memberships
            ADD CONSTRAINT workspace_memberships_same_tenant
            FOREIGN KEY (tenant_id, workspace_id)
            REFERENCES workspaces (tenant_id, id) ON DELETE CASCADE;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_workspace_memberships_user
    ON workspace_memberships (user_id, workspace_id);

-- Section 3 seeds this table. On a re-run its FORCE is already in place from the
-- previous run, so it joins the backfill window here — after the CREATE, which
-- is why it is not in section 0 with the others. Restored in section 5.
ALTER TABLE workspace_memberships NO FORCE ROW LEVEL SECURITY;
ALTER TABLE delegated_users       NO FORCE ROW LEVEL SECURITY;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. The migration owner membership
--
--    023 already created it. This confirms the lifecycle fields rather than
--    inserting a second row.
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE tenant_memberships
   SET status = 'active', joined_at = COALESCE(joined_at, created_at)
 WHERE tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid
   AND role = 'primary_owner'
   AND revoked_at IS NULL;

-- The owner joins the default workspace as its administrator.
INSERT INTO workspace_memberships (tenant_id, workspace_id, user_id, role, status)
SELECT m.tenant_id, w.id, m.user_id, 'workspace_admin', 'active'
FROM tenant_memberships m
JOIN workspaces w ON w.tenant_id = m.tenant_id AND w.is_default
WHERE m.role = 'primary_owner' AND m.membership_grants_access
ON CONFLICT (workspace_id, user_id) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. delegated_users — compatibility columns only
--
--    Resolution goes strictly through legacy_identity_map. A username that did
--    not resolve gets NO user_id: the row stays, unattached, and the quarantine
--    tooling reports it. Attaching it to "the first user" or "the owner" would
--    be a silent, permanent, invisible mistake.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE delegated_users ADD COLUMN IF NOT EXISTS user_id   UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE delegated_users ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE delegated_users ADD COLUMN IF NOT EXISTS deprecated_at TIMESTAMPTZ;

UPDATE delegated_users d
   SET user_id = m.user_id
  FROM legacy_identity_map m
 WHERE m.legacy_username = d.username
   AND m.resolution = 'MAPPED'
   AND d.user_id IS NULL;

UPDATE delegated_users
   SET tenant_id = 'e91aceeb-89fb-5ad2-b43c-4eee2bad079c'::uuid
 WHERE tenant_id IS NULL;

-- Every resolvable delegated user becomes a real membership. Unresolved ones
-- are skipped, by construction — the join simply does not match.
INSERT INTO tenant_memberships (tenant_id, user_id, role, status, joined_at)
SELECT d.tenant_id,
       d.user_id,
       CASE
           WHEN d.is_primary_owner THEN 'primary_owner'
           WHEN d.role = 'admin'   THEN 'admin'
           WHEN d.role = 'viewer'  THEN 'viewer'
           ELSE 'member'
       END,
       CASE WHEN d.active THEN 'active' ELSE 'suspended' END,
       d.created_at
FROM delegated_users d
WHERE d.user_id IS NOT NULL AND d.tenant_id IS NOT NULL
ON CONFLICT (tenant_id, user_id) DO NOTHING;

COMMENT ON TABLE delegated_users IS
    'LEGACY COMPATIBILITY ONLY (CLOUD-ADR-0006). Superseded by '
    'tenant_memberships. Do not extend. Retired in Batch 10 once every reader '
    'has moved.';
COMMENT ON COLUMN delegated_users.user_id IS
    'Resolved through legacy_identity_map only. NULL means the username did not '
    'resolve deterministically — see the quarantine report. Never guessed.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. RLS
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE workspace_memberships
    ALTER COLUMN tenant_id SET DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid;
ALTER TABLE delegated_users
    ALTER COLUMN tenant_id SET DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid;

-- ── Closing the backfill window ──────────────────────────────────────────────
--
-- Every table whose FORCE was lifted in section 0 is restored here, together
-- with the two tables this migration created. After this statement the owner is
-- subject to these policies again, exactly as the application role always was.
ALTER TABLE tenant_memberships FORCE ROW LEVEL SECURITY;
ALTER TABLE workspaces         FORCE ROW LEVEL SECURITY;
ALTER TABLE organisations      FORCE ROW LEVEL SECURITY;

ALTER TABLE workspace_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_memberships FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON workspace_memberships;
CREATE POLICY tenant_isolation ON workspace_memberships
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE delegated_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE delegated_users FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON delegated_users;
CREATE POLICY tenant_isolation ON delegated_users
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP TRIGGER IF EXISTS trg_workspace_memberships_updated_at ON workspace_memberships;
CREATE TRIGGER trg_workspace_memberships_updated_at
    BEFORE UPDATE ON workspace_memberships
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

DROP TRIGGER IF EXISTS trg_tenant_memberships_updated_at ON tenant_memberships;
CREATE TRIGGER trg_tenant_memberships_updated_at
    BEFORE UPDATE ON tenant_memberships
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
