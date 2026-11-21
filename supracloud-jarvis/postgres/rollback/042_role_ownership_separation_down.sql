-- Rollback for 042_role_ownership_separation.sql (Phase 3B-3.5).
--
-- **This rollback re-opens D-18.** It hands ownership of every application
-- object back to the runtime role, which means the runtime can again disable
-- forced RLS, drop policies and change ownership. It exists because a rollback
-- that cannot be performed is not a rollback, not because reverting is a
-- reasonable operational state.
--
-- Run it only to unblock a failed deployment, and re-apply 042 immediately
-- afterwards. `role_separation_status` reads non-zero for as long as this state
-- persists, and the runtime startup validation refuses readiness in staging and
-- production while it does — so a forgotten rollback surfaces as an outage
-- rather than as a silent downgrade. That is deliberate.
--
-- Roles are NOT dropped. Dropping a role that owns objects fails, and dropping
-- one that does not is still a cluster-level change this script has no business
-- making on its own.

DROP VIEW IF EXISTS role_separation_status;

-- Ownership back to the role the migration runner authenticates as. Extension
-- objects are skipped for the same reason 042 skips them.
DO $$
DECLARE
    obj RECORD;
    target text := current_user;
BEGIN
    FOR obj IN
        SELECT c.relname, c.relkind
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('r','v','m','S','p')
           AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                             AND d.objid = c.oid AND d.deptype = 'e')
           -- Identity and `serial` sequences follow their table's owner and
           -- cannot be reassigned separately — PostgreSQL refuses with
           --   ERROR: cannot change owner of sequence "trust_ledger_seq_seq"
           -- 042 skips them for this reason and this file did not, so the
           -- rollback failed on the first such sequence it reached. The
           -- table ALTER above moves them, so skipping is not a gap.
           AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                             AND d.objid = c.oid AND d.deptype IN ('a', 'i'))
    LOOP
        CASE obj.relkind
            WHEN 'r', 'p' THEN EXECUTE format('ALTER TABLE %I OWNER TO %I', obj.relname, target);
            WHEN 'v' THEN EXECUTE format('ALTER VIEW %I OWNER TO %I', obj.relname, target);
            WHEN 'm' THEN EXECUTE format('ALTER MATERIALIZED VIEW %I OWNER TO %I', obj.relname, target);
            WHEN 'S' THEN EXECUTE format('ALTER SEQUENCE %I OWNER TO %I', obj.relname, target);
        END CASE;
    END LOOP;

    FOR obj IN
        SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args, p.prokind
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public'
           AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass
                             AND d.objid = p.oid AND d.deptype = 'e')
    LOOP
        IF obj.prokind = 'p' THEN
            EXECUTE format('ALTER PROCEDURE public.%I(%s) OWNER TO %I', obj.proname, obj.args, target);
        ELSE
            EXECUTE format('ALTER FUNCTION public.%I(%s) OWNER TO %I', obj.proname, obj.args, target);
        END IF;
    END LOOP;
END
$$;

ALTER DEFAULT PRIVILEGES FOR ROLE ira_schema_owner IN SCHEMA public
    REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM ira_app, ira_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE ira_schema_owner IN SCHEMA public
    REVOKE USAGE, SELECT ON SEQUENCES FROM ira_app, ira_worker;

-- A rolled-back database still needs a usable runtime role, so the DML grants
-- are restored here. They are deliberately NOT `GRANT ALL`, which is what this
-- file used to do.
--
-- `GRANT ALL` includes TRUNCATE, and 042 grants only the four DML privileges
-- and never revokes a surplus one from `ira_app` — so after down-then-up the
-- runtime role kept TRUNCATE on every table, while a database built straight
-- from the chain denies it. The recomposed schema was therefore NOT equivalent
-- to the fresh one, and the difference ran in the widening direction, which is
-- the direction that matters. The CI step now asserts the equivalence.
--
-- Nothing is lost by narrowing: the blanket grant restored a *harness* artefact
-- (the workflow's own `GRANT ALL` line, since removed) rather than any state
-- the schema ever defined. PostgreSQL grants nothing on tables to PUBLIC by
-- default, so pre-042 there was no schema-level grant to put back.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ira_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ira_app;
