-- =============================================================================
-- 042_role_ownership_separation.sql — Phase 3B-3.5 (closes D-18, R-50)
--
-- Moves every application object to `ira_schema_owner` and reduces the runtime
-- roles to explicit DML.
--
-- ── What this closes ─────────────────────────────────────────────────────────
--
-- Until now the runtime role owned the tables, because `utils/migrations.py`
-- applied the chain through the ordinary application pool and DDL requires
-- ownership. An owner can `ALTER TABLE … NO FORCE ROW LEVEL SECURITY`, drop a
-- policy, or change ownership — so `FORCE ROW LEVEL SECURITY` constrained the
-- application's *queries* and not the application's *role*. Every isolation
-- claim on this branch rested on that distinction without stating it.
--
-- After this migration the runtime role owns nothing, so the same statements
-- fail with `must be owner of table`.
--
-- ── Ordering ─────────────────────────────────────────────────────────────────
--
-- Ownership first, then revokes, then grants, then default privileges. The
-- reverse order would briefly leave objects owned by the runtime role AND
-- stripped of the grants it needs, which is an outage in the middle of a
-- security fix.
--
-- Requires `provisioning/001_database_roles.sql` to have run. This file does
-- not create roles: creating one needs `CREATEROLE`, which is precisely the
-- privilege D-18 takes away from the migration path.
--
-- Rollback: rollback/042_role_ownership_separation_down.sql
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 0. Preconditions, stated loudly rather than skipped quietly
--
--    A migration that silently no-ops when the roles are absent is how a
--    security control comes to be believed in and not present. This raises.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    missing text[];
BEGIN
    SELECT array_agg(r) INTO missing FROM unnest(ARRAY[
        'ira_schema_owner', 'ira_migrator', 'ira_app', 'ira_worker'
    ]) AS r WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r);

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'role(s) % do not exist', missing
            USING HINT = 'Run postgres/provisioning/001_database_roles.sql first, '
                         'as a DBA. This migration deliberately does not create '
                         'roles: that needs CREATEROLE, which is the privilege '
                         'D-18 removes from the migration path.';
    END IF;

    -- The control being installed is worthless if the roles it names can
    -- bypass it anyway. Checked here so a mis-provisioned cluster fails at
    -- migration time rather than at audit time.
    IF EXISTS (SELECT 1 FROM pg_roles
                WHERE rolname IN ('ira_app', 'ira_worker')
                  AND (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb)) THEN
        RAISE EXCEPTION 'ira_app or ira_worker holds a privilege that defeats this migration'
            USING HINT = 'Neither may be SUPERUSER, BYPASSRLS, CREATEROLE or '
                         'CREATEDB. Re-run the provisioning script.';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles
                WHERE rolname = 'ira_schema_owner' AND (rolsuper OR rolbypassrls OR rolcanlogin)) THEN
        RAISE EXCEPTION 'ira_schema_owner must be NOLOGIN, NOSUPERUSER and NOBYPASSRLS';
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Ownership transfer
--
--    Every table, sequence, view and function in `public` that is not owned by
--    an extension moves to `ira_schema_owner`.
--
--    Extension-owned objects are skipped. `pg_depend` with `deptype = 'e'` is
--    the authoritative test — reassigning an extension's objects breaks
--    `DROP EXTENSION` and, for `vector`, the operator classes the HNSW indexes
--    depend on. `REASSIGN OWNED` would take them too, which is why it is not
--    used here.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    obj RECORD;
BEGIN
    -- Tables, views, materialised views and sequences.
    FOR obj IN
        SELECT c.oid, c.relname, c.relkind
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relkind IN ('r', 'v', 'm', 'S', 'p')
           AND pg_get_userbyid(c.relowner) <> 'ira_schema_owner'
           AND NOT EXISTS (SELECT 1 FROM pg_depend d
                            WHERE d.classid = 'pg_class'::regclass
                              AND d.objid = c.oid AND d.deptype = 'e')
           -- Sequences owned BY a table column (`serial`, identity) follow
           -- their table's owner automatically, and PostgreSQL refuses to
           -- reassign them separately:
           --   ERROR: cannot change owner of sequence "…_id_seq"
           --   DETAIL: Sequence is linked to table "…"
           -- Skipping them is not a gap — the table ALTER below moves them.
           AND NOT EXISTS (SELECT 1 FROM pg_depend d
                            WHERE d.classid = 'pg_class'::regclass
                              AND d.objid = c.oid AND d.deptype IN ('a', 'i'))
         ORDER BY c.relkind, c.relname
    LOOP
        CASE obj.relkind
            WHEN 'r', 'p' THEN
                EXECUTE format('ALTER TABLE %I OWNER TO ira_schema_owner', obj.relname);
            WHEN 'v' THEN
                EXECUTE format('ALTER VIEW %I OWNER TO ira_schema_owner', obj.relname);
            WHEN 'm' THEN
                EXECUTE format('ALTER MATERIALIZED VIEW %I OWNER TO ira_schema_owner', obj.relname);
            WHEN 'S' THEN
                EXECUTE format('ALTER SEQUENCE %I OWNER TO ira_schema_owner', obj.relname);
        END CASE;
    END LOOP;

    -- Functions and procedures.
    FOR obj IN
        SELECT p.oid, p.proname, pg_get_function_identity_arguments(p.oid) AS args, p.prokind
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public'
           AND pg_get_userbyid(p.proowner) <> 'ira_schema_owner'
           AND NOT EXISTS (SELECT 1 FROM pg_depend d
                            WHERE d.classid = 'pg_proc'::regclass
                              AND d.objid = p.oid AND d.deptype = 'e')
    LOOP
        IF obj.prokind = 'p' THEN
            EXECUTE format('ALTER PROCEDURE public.%I(%s) OWNER TO ira_schema_owner',
                           obj.proname, obj.args);
        ELSE
            EXECUTE format('ALTER FUNCTION public.%I(%s) OWNER TO ira_schema_owner',
                           obj.proname, obj.args);
        END IF;
    END LOOP;
END
$$;

-- The schema itself is NOT reassigned here. `public` is owned by
-- `pg_database_owner`, and changing that needs authority the migration role
-- deliberately does not have:
--
--     ERROR: must be owner of schema public
--
-- It is done by `provisioning/001_database_roles.sql`, which runs as a DBA.
-- Section 0 does not check for it, because a database where the schema is still
-- owned by `pg_database_owner` is merely un-hardened rather than broken — the
-- object-level grants below are what actually restrict the runtime, and
-- `role_separation_status` reports the schema owner separately.

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Revoke the unsafe defaults
--
--    PostgreSQL grants `CREATE` and `USAGE` on `public` to `PUBLIC` on older
--    clusters, and `EXECUTE` on every new function to `PUBLIC` always. Both are
--    ambient authority: they apply to roles nobody thought about, including
--    roles created after this migration ran.
-- ─────────────────────────────────────────────────────────────────────────────

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;

-- USAGE on the schema is NOT revoked from PUBLIC. It confers only the right to
-- look up a name, and every grant below would need it re-granted individually;
-- removing it also breaks the `vector` extension's operator lookup. The
-- protection is the object-level grants, not name resolution.

-- ── Extension functions get their EXECUTE back ───────────────────────────────
--
-- `REVOKE ALL ON ALL FUNCTIONS` does not distinguish our functions from an
-- extension's. `uuid-ossp` and `vector` install theirs into `public`, so the
-- revoke above takes `uuid_generate_v4` away from every role — and since half
-- the schema uses it as a column DEFAULT, the symptom is
--
--     ERROR: permission denied for function uuid_generate_v4
--
-- on ordinary INSERTs, which reads as a broken application rather than as a
-- privilege change. Restoring them keeps the revoke aimed at what it was for:
-- ambient EXECUTE on OUR functions.
--
-- Restored to PUBLIC rather than to the runtime roles specifically, because
-- that is the state the extension installed and expects; these are pure
-- functions over their arguments with no access to application data.
DO $$
DECLARE
    fn RECORD;
BEGIN
    FOR fn IN
        SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public'
           AND EXISTS (SELECT 1 FROM pg_depend d
                        WHERE d.classid = 'pg_proc'::regclass
                          AND d.objid = p.oid AND d.deptype = 'e')
    LOOP
        EXECUTE format('GRANT EXECUTE ON FUNCTION public.%I(%s) TO PUBLIC',
                       fn.proname, fn.args);
    END LOOP;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Runtime grants — explicit, and DML only
--
--    `GRANT ALL` is what this migration exists to undo. It includes TRUNCATE
--    (which bypasses row-level security entirely — a tenant-scoped DELETE is
--    filtered, a TRUNCATE empties the table) and REFERENCES (which lets a role
--    pin rows it cannot read by pointing a foreign key at them).
-- ─────────────────────────────────────────────────────────────────────────────

GRANT USAGE ON SCHEMA public TO ira_app, ira_worker, ira_migrator;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
    TO ira_app, ira_worker;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ira_app, ira_worker;

-- ── Function EXECUTE: allow-by-default for the runtime, with a deny list ─────
--
-- The first version of this granted three functions by name. It broke ordinary
-- INSERTs, because CHECK constraints and triggers execute as the CALLING role:
--
--     ERROR: permission denied for function credential_looks_like_plaintext
--     ERROR: permission denied for function realtime_metrics_are_measurements
--
-- Those are constraint helpers. The runtime never calls them directly and
-- cannot write a row without them. Enumerating such functions by hand is a
-- standing promise to break production the next time a migration adds a
-- trigger, and the failure lands on an unrelated feature.
--
-- So the shape is: **PUBLIC keeps nothing** — that revoke above is the one that
-- removes ambient authority — and the runtime roles get EXECUTE on the schema's
-- own functions except an explicit, reviewed deny list.
--
-- The deny list holds functions that are platform OPERATIONS rather than schema
-- machinery. It is short on purpose; anything added to it needs a reason in
-- `PHASE_3B_SECURITY_DEFINER_REVIEW.md`.
DO $$
DECLARE
    fn RECORD;
    denied CONSTANT text[] := ARRAY['provision_tenant_substrate'];
BEGIN
    FOR fn IN
        SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public'
           AND NOT (p.proname = ANY (denied))
           AND NOT EXISTS (SELECT 1 FROM pg_depend d
                            WHERE d.classid = 'pg_proc'::regclass
                              AND d.objid = p.oid AND d.deptype = 'e')
    LOOP
        EXECUTE format('GRANT EXECUTE ON FUNCTION public.%I(%s) TO ira_app, ira_worker',
                       fn.proname, fn.args);
    END LOOP;
END
$$;

-- `provision_tenant_substrate` writes organisations and workspaces for an
-- arbitrary tenant id. That is a platform operation, and the runtime holding it
-- would make "which tenant am I acting for" a parameter rather than a context.
REVOKE ALL ON FUNCTION provision_tenant_substrate(UUID) FROM PUBLIC, ira_app, ira_worker;

-- `schema_migrations` is readable by the runtime — startup validation has to
-- check the migration level — and writable by nobody but the migrator. A
-- runtime that could INSERT here could mark an unapplied migration as applied
-- and silence its own readiness check.
--
-- Conditional because the table is created by the migration RUNNER, not by this
-- chain: on the `docker-entrypoint-initdb.d` path it does not exist yet. The
-- runner creates it as `ira_schema_owner` and re-applies these grants itself,
-- so the protection holds on both paths rather than only on the one that
-- happens to have run first.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'schema_migrations') THEN
        REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM ira_app, ira_worker;
        GRANT  SELECT                  ON schema_migrations TO ira_app, ira_worker;
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Default privileges
--
--    Without these, the NEXT migration's table is owned by `ira_schema_owner`
--    with no grant to anybody, and the application gets `permission denied` on
--    a table it can see in the schema. That failure looks like a bug in the
--    feature rather than a missing grant, so it is worth pre-empting.
--
--    `FOR ROLE ira_schema_owner` matters: default privileges attach to the
--    CREATING role, and after section 1 that is the schema owner, not whoever
--    is running the migration.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER DEFAULT PRIVILEGES FOR ROLE ira_schema_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ira_app, ira_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE ira_schema_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO ira_app, ira_worker;

-- Functions are NOT given a default grant. PostgreSQL's own default is EXECUTE
-- to PUBLIC, so the revoke below is what actually removes ambient authority,
-- and adding a default grant back would reintroduce it under a different name.
ALTER DEFAULT PRIVILEGES FOR ROLE ira_schema_owner IN SCHEMA public
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. `provision_tenant_substrate` — made callable without a bypass
--
--    It inserts into `organisations` and `workspaces`, both FORCE RLS. As
--    written it could only succeed for a role that bypasses RLS, which after
--    this migration is nobody — so it was, in practice, uncallable.
--
--    It now binds `app.tenant_id` to the tenant being provisioned and restores
--    the previous value before returning, so the inserts satisfy their own
--    WITH CHECK without anything being exempted. `set_config(..., true)` is
--    transaction-local, and the restore means a caller's context survives the
--    call — a function that silently rebound the caller's tenant would be a far
--    worse bug than the one it fixes.
--
--    It stays SECURITY INVOKER. A DEFINER version owned by `ira_schema_owner`
--    would still be subject to FORCE, so it would buy nothing and cost the
--    reviewer a bypass to reason about.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION provision_tenant_substrate(p_tenant UUID)
RETURNS UUID
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    v_org       UUID;
    v_workspace UUID;
    v_previous  TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM tenants WHERE id = p_tenant) THEN
        RAISE EXCEPTION 'provision_tenant_substrate: tenant % does not exist', p_tenant
            USING HINT = 'Create the tenant first. This function does not create one, '
                         'because a typo in a tenant id would otherwise silently '
                         'manufacture a tenant nobody asked for.';
    END IF;

    v_previous := current_setting('app.tenant_id', true);
    PERFORM set_config('app.tenant_id', p_tenant::text, true);

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

    -- Restore, including the "was unset" case: `set_config` to '' is what the
    -- rest of this schema treats as unbound, via NULLIF(..., '').
    PERFORM set_config('app.tenant_id', COALESCE(v_previous, ''), true);
    RETURN v_workspace;
END
$$;

ALTER FUNCTION provision_tenant_substrate(UUID) OWNER TO ira_schema_owner;
REVOKE ALL ON FUNCTION provision_tenant_substrate(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION provision_tenant_substrate(UUID) TO ira_migrator;

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Pin search_path on every migration-managed function
--
--    A function with a mutable `search_path` can be made to call a different
--    `uuid_generate_v5` — or a different `tenants` — by a caller who can create
--    objects in a schema earlier in the path. Section 2 removed `CREATE` on
--    `public` from PUBLIC, which closes the obvious route; pinning the path
--    closes it independently, so neither has to hold alone.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    fn RECORD;
BEGIN
    FOR fn IN
        SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public'
           AND p.prokind IN ('f', 'p')
           AND (p.proconfig IS NULL
                OR NOT EXISTS (SELECT 1 FROM unnest(p.proconfig) c
                                WHERE c LIKE 'search_path=%'))
           AND NOT EXISTS (SELECT 1 FROM pg_depend d
                            WHERE d.classid = 'pg_proc'::regclass
                              AND d.objid = p.oid AND d.deptype = 'e')
    LOOP
        EXECUTE format('ALTER FUNCTION public.%I(%s) SET search_path = public, pg_temp',
                       fn.proname, fn.args);
    END LOOP;
END
$$;

-- Applied to EVERY migration-managed function, not just the ones this phase
-- wrote. Twelve pre-existing trigger functions were unpinned — including
-- `trust_ledger_guard` and `tenant_data_keys_shred_only`, which enforce the
-- append-only and crypto-shred rules. Pinning the ones added recently and
-- leaving those would have been the least useful half of the change.

-- ─────────────────────────────────────────────────────────────────────────────
-- 6a. Two tables that enable row-level security without forcing it
--
--    `008_business_data.sql` enables RLS on `investors` and `client_agents` and
--    never forces it, so the table owner is exempt from their policies. That
--    predates this branch and was harmless while the owner and the runtime were
--    the same role holding the same access — nobody noticed because there was
--    nothing to notice.
--
--    After this migration the owner is `ira_schema_owner`, which nothing
--    authenticates as, so the practical exposure is already small. Forcing them
--    costs nothing and removes the exception: `scripts/migrate.py verify` and
--    the runtime's own startup check both treat "enabled but not forced" as an
--    unclosed backfill window, and an intended exception that looks identical to
--    a defect trains people to ignore the alarm.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE investors     FORCE ROW LEVEL SECURITY;
ALTER TABLE client_agents FORCE ROW LEVEL SECURITY;

-- ─────────────────────────────────────────────────────────────────────────────
-- 7. Verification view — must read all-clear
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW role_separation_status
WITH (security_invoker = true) AS
SELECT
    (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relkind IN ('r','v','m','S','p')
        AND pg_get_userbyid(c.relowner) IN ('ira_app', 'ira_worker'))   AS objects_owned_by_runtime,
    (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relkind = 'r'
        AND pg_get_userbyid(c.relowner) <> 'ira_schema_owner'
        AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                          AND d.objid = c.oid AND d.deptype = 'e'))     AS tables_not_owned_by_schema_owner,
    (SELECT count(*) FROM information_schema.role_table_grants
      WHERE table_schema = 'public' AND grantee = 'PUBLIC')             AS public_table_grants,
    (SELECT count(*) FROM pg_roles
      WHERE rolname IN ('ira_app','ira_worker') AND (rolsuper OR rolbypassrls)) AS runtime_roles_that_bypass,
    (SELECT count(*) FROM information_schema.role_table_grants
      WHERE table_schema = 'public' AND grantee IN ('ira_app','ira_worker')
        AND privilege_type IN ('TRUNCATE','REFERENCES','TRIGGER'))      AS runtime_roles_with_ddl_adjacent_grants;

COMMENT ON VIEW role_separation_status IS
    'Every column must read 0. Non-zero objects_owned_by_runtime means the '
    'runtime can ALTER TABLE its own tables, which is D-18 reopened.';
