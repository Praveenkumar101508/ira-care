-- =============================================================================
-- provisioning/001_database_roles.sql — Phase 3B-3.5 (D-18, R-50)
--
-- Creates the separated PostgreSQL role model. **Run once, by a DBA or an
-- installer with role-creation authority, BEFORE the migration chain.**
--
-- ── Why this is not a migration ──────────────────────────────────────────────
--
-- Roles are cluster-level objects, and creating one needs `CREATEROLE` or
-- superuser. The whole point of D-18 is that the migration role does **not**
-- hold unrestricted role administration — so a migration that created its own
-- roles would need exactly the privilege this phase exists to take away.
--
-- This directory is deliberately NOT `postgres/`: `docker-entrypoint-initdb.d`
-- and `utils/migrations.py` both read the top level only, so nothing here runs
-- automatically. That is the intent. Creating roles is an operator decision.
--
-- ── The model ────────────────────────────────────────────────────────────────
--
--   ira_schema_owner   owns schemas, tables, sequences, views, policies,
--                      functions. NOLOGIN — nothing authenticates as it.
--   ira_migrator       applies reviewed migrations. LOGIN, and may
--                      `SET ROLE ira_schema_owner`. Separate credential.
--   ira_app            runtime API. LOGIN, owns nothing, DML only.
--   ira_worker         background processing. Same restrictions as ira_app.
--   ira_readonly       optional. NOT created here — see the note at the end.
--
-- ── Passwords ────────────────────────────────────────────────────────────────
--
-- **No password is set by this script, and that is deliberate.** A password in
-- a file in a repository is a credential in a repository. Under `scram-sha-256`
-- or `md5` a LOGIN role with no password cannot authenticate at all, so these
-- roles are inert until an operator sets one — which is the safe default. Local
-- development using `peer` or `trust` works without any.
--
--     ALTER ROLE ira_app      WITH PASSWORD '...';   -- run by the operator
--     ALTER ROLE ira_worker   WITH PASSWORD '...';
--     ALTER ROLE ira_migrator WITH PASSWORD '...';
--
-- The migrator credential must NOT live in ordinary runtime application
-- configuration. See PHASE_3B_MIGRATION_RUNNER_SEPARATION.md.
-- =============================================================================

-- ── Extensions belong here too ───────────────────────────────────────────────
--
-- `init.sql` creates `uuid-ossp`, `pg_trgm` and `vector` with
-- `CREATE EXTENSION IF NOT EXISTS`. Creating an extension needs CREATE on the
-- database, and most extensions install C functions that run inside the server
-- process — so it is a DBA operation, not something the migration role should
-- be able to do. `ira_migrator` deliberately cannot, and the migration command
-- fails with
--
--     permission denied to create extension "pg_trgm"
--
-- if this section has not run. The `IF NOT EXISTS` in `init.sql` then makes
-- those statements no-ops rather than failures.
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

DO $$
BEGIN
    -- ── ira_schema_owner ─────────────────────────────────────────────────────
    -- NOLOGIN is the load-bearing attribute: an owner that cannot authenticate
    -- cannot be reached by a stolen credential, only by a role explicitly
    -- granted membership in it.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ira_schema_owner') THEN
        CREATE ROLE ira_schema_owner
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    ELSE
        ALTER ROLE ira_schema_owner
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;

    -- ── ira_migrator ─────────────────────────────────────────────────────────
    -- NOINHERIT is what makes `SET ROLE` an explicit act rather than an ambient
    -- one. With INHERIT, every migrator connection would silently carry the
    -- schema owner's privileges from the first statement; with NOINHERIT the
    -- migration command has to ask, and the audit trail shows when it did.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ira_migrator') THEN
        CREATE ROLE ira_migrator
            LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    ELSE
        ALTER ROLE ira_migrator
            LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;

    -- ── ira_app ──────────────────────────────────────────────────────────────
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ira_app') THEN
        CREATE ROLE ira_app
            LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    ELSE
        -- An existing ira_app may predate this model with broader attributes.
        ALTER ROLE ira_app
            LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;

    -- ── ira_worker ───────────────────────────────────────────────────────────
    -- Identical restrictions to ira_app. A background job is not a reason for
    -- broader rights; it is a reason for the same rights with a different
    -- credential, so a compromised worker and a compromised API are separately
    -- revocable.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ira_worker') THEN
        CREATE ROLE ira_worker
            LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    ELSE
        ALTER ROLE ira_worker
            LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
END
$$;

-- ── The one membership that exists ───────────────────────────────────────────
--
-- `ira_migrator` may become `ira_schema_owner`. Nothing else may.
--
-- `WITH ADMIN OPTION` is deliberately NOT granted: the migrator can use the
-- role and cannot hand it to anybody else, so a compromised migrator credential
-- cannot widen the blast radius by enrolling `ira_app`.
GRANT ira_schema_owner TO ira_migrator;

-- ira_app and ira_worker are granted membership in NOTHING. Stated as an
-- absence rather than left to inference, because "which roles is the runtime a
-- member of" is the first question an auditor asks and the answer must be
-- "none".

COMMENT ON ROLE ira_schema_owner IS
    'Owns application schemas, tables, sequences, views, policies and functions. '
    'NOLOGIN. Reached only via SET ROLE from ira_migrator.';
COMMENT ON ROLE ira_migrator IS
    'Applies reviewed migrations. NOINHERIT, so SET ROLE ira_schema_owner is an '
    'explicit act. Credential must not appear in runtime application config.';
COMMENT ON ROLE ira_app IS
    'Runtime API role. Owns nothing, holds no DDL, subject to forced RLS.';
COMMENT ON ROLE ira_worker IS
    'Background processing role. Same restrictions as ira_app, separate credential.';

-- ── ira_readonly is deliberately NOT created ─────────────────────────────────
--
-- The brief marks it optional and says not to implement it without a concrete
-- existing use. There is none: no reporting surface, no support console, no
-- analytics consumer. Creating it now would add a credential that grants
-- read access to customer data and that nobody is watching, which is a worse
-- position than not having it.
--
-- When a real use appears it needs its own decision, because "read-only" is not
-- automatically safe: it would still have to be refused TOTP secrets, biometric
-- templates and key material, and that refusal has to be built rather than
-- assumed from the word "read-only".


-- ── Schema access for the roles that need it ─────────────────────────────────
--
-- `ira_schema_owner` must be able to create objects in `public` before `042`
-- transfers ownership of the schema to it; `ira_migrator` needs USAGE to reach
-- them. The runtime roles get theirs from `042` along with their table grants,
-- so they are deliberately absent here.
GRANT ALL   ON SCHEMA public TO ira_schema_owner;
GRANT USAGE ON SCHEMA public TO ira_migrator;

-- Schema ownership moves here rather than in `042`, because `public` is owned
-- by `pg_database_owner` and reassigning it needs DBA authority — precisely the
-- authority the migration role must not have. Attempting it from a migration
-- fails with `must be owner of schema public`.
--
-- Owning the schema is what makes `REVOKE CREATE ON SCHEMA public FROM PUBLIC`
-- in `042` stick, and what stops anything but the schema owner adding objects.
ALTER SCHEMA public OWNER TO ira_schema_owner;
