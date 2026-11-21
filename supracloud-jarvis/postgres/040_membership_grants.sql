-- =============================================================================
-- 040_membership_grants.sql — Phase 3B Batch 3
--
-- Completes memberships: workspace access becomes derivable and revocable,
-- workspace roles map to capabilities as data rather than as strings compared
-- in code, a person can enumerate their own memberships, and `delegated_users`
-- becomes a closed legacy table rather than a live parallel authorisation model.
--
-- ── The hole Batch 0 left ────────────────────────────────────────────────────
--
-- `035` created `workspace_memberships` with nothing tying it to
-- `tenant_memberships`. So:
--
--   * a workspace membership could exist for a user with no membership of the
--     tenant that owns the workspace;
--   * removing somebody from a tenant left every workspace membership intact,
--     and workspace access is what Batch 5's content policies will read.
--
-- "Removed from the organisation but still in the Engineering workspace" is not
-- a stale row. It is continued access after revocation, and it would have been
-- introduced by the very batch that added memberships.
--
-- Two mechanisms, because one is not enough. A composite foreign key makes the
-- orphan unrepresentable; a trigger makes revocation propagate. The key cannot
-- express the second (a membership row still exists when it stops granting
-- access) and the trigger cannot express the first (it never fires for a row
-- inserted with no parent).
--
-- ── `membership_grants_access` and its counterpart ───────────────────────────
--
-- `035` gave `tenant_memberships` a generated `membership_grants_access`
-- because checking `status` or `revoked_at` individually is how a suspended
-- member keeps access. `workspace_memberships` shipped without the equivalent,
-- which left exactly that trap open one level down. It gets one here.
--
-- Rollback: rollback/040_membership_grants_down.sql
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 0. Backfill window — see `035_memberships.sql` section 0
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE workspace_memberships NO FORCE ROW LEVEL SECURITY;
ALTER TABLE tenant_memberships    NO FORCE ROW LEVEL SECURITY;
ALTER TABLE delegated_users       NO FORCE ROW LEVEL SECURITY;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Workspace membership lifecycle
-- ─────────────────────────────────────────────────────────────────────────────

-- Repair before constraining, so the CHECK below cannot fail on legacy rows.
UPDATE workspace_memberships SET removed_at = NOW()
 WHERE status = 'removed' AND removed_at IS NULL;
UPDATE workspace_memberships SET removed_at = NULL
 WHERE status <> 'removed' AND removed_at IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'workspace_memberships_status_removed_agree') THEN
        ALTER TABLE workspace_memberships
            ADD CONSTRAINT workspace_memberships_status_removed_agree
            CHECK ((status = 'removed') = (removed_at IS NOT NULL));
    END IF;
END
$$;

ALTER TABLE workspace_memberships
    ADD COLUMN IF NOT EXISTS workspace_grants_access BOOLEAN
    GENERATED ALWAYS AS (status = 'active' AND removed_at IS NULL) STORED;

COMMENT ON COLUMN workspace_memberships.workspace_grants_access IS
    'Generated. The ONLY correct workspace access predicate. Reading status or '
    'removed_at separately is how a suspended member keeps access — the same '
    'trap tenant_memberships.membership_grants_access exists to close.';

CREATE INDEX IF NOT EXISTS idx_workspace_memberships_active
    ON workspace_memberships (user_id, workspace_id) WHERE workspace_grants_access;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. A workspace membership requires a tenant membership
--
--    Orphans are cleared first — there are none in a database built by this
--    chain, but a composite key cannot be added over a row that violates it,
--    and failing here with a foreign-key error names the constraint rather than
--    the problem.
-- ─────────────────────────────────────────────────────────────────────────────

DELETE FROM workspace_memberships wm
 WHERE NOT EXISTS (
        SELECT 1 FROM tenant_memberships tm
         WHERE tm.tenant_id = wm.tenant_id AND tm.user_id = wm.user_id);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'workspace_memberships_requires_tenant_membership') THEN
        ALTER TABLE workspace_memberships
            ADD CONSTRAINT workspace_memberships_requires_tenant_membership
            FOREIGN KEY (tenant_id, user_id)
            REFERENCES tenant_memberships (tenant_id, user_id) ON DELETE CASCADE;
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Revocation propagates
--
--    The foreign key handles deletion. It cannot handle the case that actually
--    happens: the tenant membership row STAYS and stops granting access. A
--    suspended or removed tenant member keeps every workspace membership unless
--    something acts on the transition.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION tenant_membership_revocation_cascade()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    -- Only on the transition to "no longer grants access". Firing on every
    -- update would re-remove already-removed rows and churn updated_at.
    IF OLD.membership_grants_access AND NOT NEW.membership_grants_access THEN
        UPDATE workspace_memberships
           SET status = 'removed', removed_at = NOW()
         WHERE tenant_id = NEW.tenant_id
           AND user_id   = NEW.user_id
           AND status <> 'removed';
    END IF;

    -- Deliberately NOT symmetric. Restoring a tenant membership does not
    -- restore workspace access: which workspaces somebody should rejoin is a
    -- decision, and silently returning them to every workspace they were once
    -- in is how a re-hired contractor regains access to a project they were
    -- removed from for cause.
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_tenant_membership_revocation ON tenant_memberships;
CREATE TRIGGER trg_tenant_membership_revocation
    AFTER UPDATE ON tenant_memberships
    FOR EACH ROW EXECUTE FUNCTION tenant_membership_revocation_cascade();

COMMENT ON FUNCTION tenant_membership_revocation_cascade() IS
    'Revocation propagates down; restoration does not propagate back. '
    'Asymmetric on purpose — see the comment in the function body.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Workspace grants — role to capability, as data
--
--    `035` validated workspace roles with a CHECK, which stops a typo becoming
--    a role. It does not answer "what may a viewer do", and the answer today
--    would be a set of string comparisons spread across application code.
--
--    A reference table makes the mapping inspectable, testable and reviewable
--    in one place. It is REFERENCE_DATA: platform-owned, identical for every
--    tenant, and NOT tenant-writable — a tenant that could grant its own roles
--    extra capabilities would be granting itself privilege.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS workspace_role_capabilities (
    role       TEXT NOT NULL
               CHECK (role IN ('workspace_admin', 'member', 'viewer')),
    capability TEXT NOT NULL
               CHECK (capability IN (
                   'workspace.read',
                   'workspace.write',
                   'workspace.manage_members',
                   'workspace.manage_settings',
                   'workspace.delete_content')),
    PRIMARY KEY (role, capability)
);

-- The seed joins the backfill window here rather than in section 0, because the
-- table does not exist until the statement above. On a re-run its read-only
-- policy is already in place from the previous run, and a read-only policy
-- refuses this INSERT — `ON CONFLICT DO NOTHING` does not help, because
-- PostgreSQL checks the INSERT policy against the proposed row before conflict
-- arbitration. FORCE is restored in section 5 with the rest.
ALTER TABLE workspace_role_capabilities NO FORCE ROW LEVEL SECURITY;

INSERT INTO workspace_role_capabilities (role, capability) VALUES
    ('viewer',          'workspace.read'),
    ('member',          'workspace.read'),
    ('member',          'workspace.write'),
    ('workspace_admin', 'workspace.read'),
    ('workspace_admin', 'workspace.write'),
    ('workspace_admin', 'workspace.manage_members'),
    ('workspace_admin', 'workspace.manage_settings'),
    ('workspace_admin', 'workspace.delete_content')
ON CONFLICT (role, capability) DO NOTHING;

-- `viewer` deliberately has no write and no delete. `member` deliberately has
-- no delete_content: removing another person's work is an administrative act,
-- and the difference between "I can add" and "I can remove what you added" is
-- the difference customers actually complain about.

COMMENT ON TABLE workspace_role_capabilities IS
    'REFERENCE_DATA. Platform-owned, identical for every tenant, never '
    'tenant-writable — a tenant able to add capabilities to its own roles is a '
    'tenant able to grant itself privilege.';

CREATE OR REPLACE FUNCTION workspace_role_allows(p_role TEXT, p_capability TEXT)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1 FROM workspace_role_capabilities
         WHERE role = p_role AND capability = p_capability)
$$;

COMMENT ON FUNCTION workspace_role_allows(TEXT, TEXT) IS
    'Fails closed: an unknown role or an unknown capability returns false, '
    'because a missing row is not permission.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Membership policies — a person may read their own memberships
--
--    Both tables were tenant-scoped only. That makes "which tenants do I belong
--    to?" unanswerable: the question has to be asked BEFORE a tenant is bound,
--    and a tenant-only predicate returns nothing when none is.
--
--    The consequence is not theoretical. It is the query a tenant picker runs
--    at sign-in, and without it the only way to answer would be to read the
--    membership table with no policy at all.
--
--    Split per command, as `038` had to be. The SELECT arm admits the subject;
--    INSERT, UPDATE and DELETE stay tenant-scoped, because reading your own
--    membership is a right and editing it is not — a member who could update
--    their own row could set their role to `owner`.
--
--    An `ALL` policy could not express this: PostgreSQL checks an UPDATE's new
--    row against a FOR ALL policy's USING clause, so the subject arm would
--    silently become a write permission too. `038` documents that trap in full.
-- ─────────────────────────────────────────────────────────────────────────────

DROP POLICY IF EXISTS tenant_isolation           ON tenant_memberships;
DROP POLICY IF EXISTS member_or_tenant_select    ON tenant_memberships;
DROP POLICY IF EXISTS tenant_scoped_insert       ON tenant_memberships;
DROP POLICY IF EXISTS tenant_scoped_update       ON tenant_memberships;
DROP POLICY IF EXISTS tenant_scoped_delete       ON tenant_memberships;

CREATE POLICY member_or_tenant_select ON tenant_memberships FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
           OR user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY tenant_scoped_insert ON tenant_memberships FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY tenant_scoped_update ON tenant_memberships FOR UPDATE
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY tenant_scoped_delete ON tenant_memberships FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS tenant_isolation        ON workspace_memberships;
DROP POLICY IF EXISTS member_or_tenant_select ON workspace_memberships;
DROP POLICY IF EXISTS tenant_scoped_insert    ON workspace_memberships;
DROP POLICY IF EXISTS tenant_scoped_update    ON workspace_memberships;
DROP POLICY IF EXISTS tenant_scoped_delete    ON workspace_memberships;

CREATE POLICY member_or_tenant_select ON workspace_memberships FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
           OR user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY tenant_scoped_insert ON workspace_memberships FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY tenant_scoped_update ON workspace_memberships FOR UPDATE
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY tenant_scoped_delete ON workspace_memberships FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Reference data: readable by everyone, writable by nobody through the
-- application role. RLS with a read-only policy rather than a REVOKE, because
-- the documented provisioning step ends with GRANT ALL ON ALL TABLES and would
-- hand a revoke straight back (R-47).
ALTER TABLE workspace_role_capabilities ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_role_capabilities FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS reference_read_only ON workspace_role_capabilities;
CREATE POLICY reference_read_only ON workspace_role_capabilities FOR SELECT USING (TRUE);
-- No INSERT, UPDATE or DELETE policy: writes match no policy and are refused.

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. tenant_directory — the read path R-46 asked for
--
--    R-46: a tenant able to read the `tenants` registry learns every other
--    customer's name and slug. The base-table grant cannot be withdrawn yet —
--    twelve test modules and `data.create_tenant` still insert tenants through
--    the application role — so this migration delivers the READ path and the
--    grant change follows in a later batch, with those call sites.
--
--    Stated plainly because a half-closed control is worth less than a clearly
--    described one: **R-46 is not closed here.** This view is what makes
--    closing it possible without breaking sign-in.
-- ─────────────────────────────────────────────────────────────────────────────

-- **`security_invoker` is load-bearing, not a style choice.** A view defaults to
-- running as its OWNER, and the owner is subject to FORCE ROW LEVEL SECURITY
-- like anybody else. Without this setting the view evaluates with the migration
-- role's (unbound) context, matches zero rows, and reports
-- an empty directory — to every caller,
-- including a superuser, because the caller's privileges never enter into it.
-- That was the state of this view as originally written; it was found by
-- reading it as a non-superuser owner.
CREATE OR REPLACE VIEW tenant_directory
WITH (security_invoker = true) AS
SELECT t.id AS tenant_id,
       t.name,
       t.slug,
       m.role,
       m.status,
       m.membership_grants_access
  FROM tenant_memberships m
  JOIN tenants t ON t.id = m.tenant_id
 WHERE m.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid;

COMMENT ON VIEW tenant_directory IS
    'The tenants the CURRENT USER belongs to. The read path for a sign-in '
    'tenant picker, which must work before any tenant is bound. Rows come from '
    'the caller''s own memberships, so it cannot enumerate the registry.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 7. delegated_users — closed to new rows
--
--    `035` backfilled it into memberships and marked it legacy in a COMMENT. A
--    comment does not stop a call site adding row 51 next week, and every row
--    added after the backfill is a principal that exists in the old model and
--    not the new one.
--
--    INSERT is refused; UPDATE and DELETE stay open, because existing rows
--    still need to be deactivated and eventually removed during the transition.
--    Batch 10 drops the table.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION delegated_users_refuse_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'delegated_users is closed: no new delegated user may be created'
        USING HINT = 'Create a user and a tenant_membership instead. This table '
                     'is legacy compatibility for rows that predate memberships '
                     'and is dropped in Batch 10.';
END
$$;

DROP TRIGGER IF EXISTS delegated_users_closed ON delegated_users;
CREATE TRIGGER delegated_users_closed
    BEFORE INSERT ON delegated_users
    FOR EACH ROW EXECUTE FUNCTION delegated_users_refuse_insert();

CREATE OR REPLACE VIEW delegated_user_migration_status
WITH (security_invoker = true) AS
SELECT
    count(*)                                                    AS total_rows,
    count(*) FILTER (WHERE user_id IS NULL)                     AS unresolved,
    count(*) FILTER (WHERE user_id IS NOT NULL)                 AS resolved,
    count(*) FILTER (WHERE user_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM tenant_memberships m
         WHERE m.tenant_id = d.tenant_id AND m.user_id = d.user_id)) AS resolved_without_membership,
    (count(*) FILTER (WHERE user_id IS NULL) = 0)               AS ready_to_retire
FROM delegated_users d;

COMMENT ON VIEW delegated_user_migration_status IS
    'Gates the Batch 10 drop. ready_to_retire is false while any row has an '
    'unresolved owner — dropping the table then would discard a principal '
    'nobody has mapped.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 8. Closing the backfill window
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE workspace_memberships FORCE ROW LEVEL SECURITY;
ALTER TABLE tenant_memberships    FORCE ROW LEVEL SECURITY;
ALTER TABLE delegated_users       FORCE ROW LEVEL SECURITY;
