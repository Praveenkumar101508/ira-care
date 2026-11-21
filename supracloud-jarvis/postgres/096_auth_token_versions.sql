-- =============================================================================
-- 096_auth_token_versions.sql
--
-- Durable "log out everywhere": the per-account token version moves from Redis
-- to PostgreSQL. Owner-approved on 2026-10-23, as written in
-- docs/security/GLOBAL_LOGOUT_REVIEW_PACKAGE_2026-10-21.md (decisions A-F).
--
-- ── The defect ──────────────────────────────────────────────────────────────
--
-- The version lived only in Redis (`ira:token_ver:<username>`), and a missing
-- key read as 0. When Redis lost the key (a restart without persistence, a
-- flush, a restore from an older snapshot), every token issued before "log out
-- everywhere" worked again: up to 30 minutes for an access token, seven days
-- for a refresh token.
--
-- ── What this table guarantees ──────────────────────────────────────────────
--
--   * Keyed by `users.id` with ON DELETE RESTRICT (decision A). The account
--     row cannot be removed while a counter exists, so a name re-used for a
--     new account cannot restart its counter at 0.
--   * Monotonic: a trigger refuses any UPDATE that lowers `version`, so no
--     write, from any role, can revive an old token by lowering the counter.
--   * A row may be deleted only when BOTH its account was erased more than
--     seven days ago (decision D) AND the counter itself has not moved for
--     seven days. Seven days is the longest life of any credential IRA signs
--     (config.MAX_REFRESH_TOKEN_EXPIRE_DAYS refuses a longer refresh token), so
--     every token the counter ever refused has expired, and deleting it, which
--     resets it to 0, revives nothing. `updated_at` is set by the trigger below
--     on every UPDATE, so no role can backdate it; `users.deleted_at` alone
--     could be backdated by anyone who can write `users`.
--   * ira_app may SELECT, INSERT, UPDATE, and DELETE through that policy only:
--     it purges at every start, which every install runs. ira_worker may
--     SELECT and DELETE, for the daily job where a worker runs.
--   * Every policy but the purge keys on `app.user_id`, as user_credentials
--     (054) does. Whoever reads or bumps a counter always knows the account
--     first (the token's `uid`, or the id its subject resolves to), and
--     core/identity/token_versions.py binds it for that one transaction. A
--     query that forgot its WHERE clause still reaches that one account's
--     row, and an unbound connection reaches none. No tenant is involved: the
--     counter is per account, and it is read before any tenant is bound.
--   * The row holds a UUID and an integer: no name, no address.
--
-- ── A deviation from the approved package, and why ─────────────────────────
--
-- The package (§4) wrote USING (true) on read and bump, "as
-- f1_token_revocations is". That table holds token hashes and no owner
-- column; this one holds `user_id`, and
-- tests/readiness/test_cross_tenant_sweep.py refuses any table with an owner
-- column and no database-level isolation. Keying on `app.user_id` is the
-- model every other per-account identity table here already uses, and it is
-- stricter than the package, never looser.
--
-- Second, the package let only ira_worker purge. No shipped launcher connects
-- as ira_worker (start-ira.sh starts no worker; run-local.ps1 runs it with the
-- install's ira_app credentials), so the purge would never run and erased
-- accounts' counters would be kept indefinitely. ira_app now purges at every
-- start, through a policy that also requires the counter to be seven days
-- still, which the package's (deleted_at alone) did not.
--
-- ── What this file does not do ──────────────────────────────────────────────
--
-- Legacy delegated accounts from before 040 may have no `users` row, and so
-- no counter. `private_user_id` has no SQL definition (it exists only in
-- core/identity/principal.py), so the start-up seeder,
-- core/identity/token_versions.seed_from_redis, creates those rows, and copies
-- any surviving Redis counter in with GREATEST, on every start. Until the
-- first start after 096 such an account is refused, never passed.
--
-- Idempotent: IF NOT EXISTS, OR REPLACE and DROP ... IF EXISTS throughout.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.auth_token_versions (
    user_id    uuid        PRIMARY KEY REFERENCES public.users(id) ON DELETE RESTRICT,
    version    bigint      NOT NULL CHECK (version >= 0),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE public.auth_token_versions OWNER TO ira_schema_owner;
REVOKE ALL ON public.auth_token_versions FROM PUBLIC, ira_app, ira_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.auth_token_versions TO ira_app;  -- DELETE: the purge policy only
GRANT SELECT, DELETE ON public.auth_token_versions TO ira_worker;               -- the purge only

ALTER TABLE public.auth_token_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.auth_token_versions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS auth_token_versions_read       ON public.auth_token_versions;
DROP POLICY IF EXISTS auth_token_versions_create     ON public.auth_token_versions;
DROP POLICY IF EXISTS auth_token_versions_bump       ON public.auth_token_versions;
DROP POLICY IF EXISTS auth_token_versions_purge      ON public.auth_token_versions;
DROP POLICY IF EXISTS auth_token_versions_purge_read ON public.auth_token_versions;
CREATE POLICY auth_token_versions_read   ON public.auth_token_versions FOR SELECT
    USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY auth_token_versions_create ON public.auth_token_versions FOR INSERT
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY auth_token_versions_bump   ON public.auth_token_versions FOR UPDATE
    USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
-- The purge: only rows whose account was erased more than seven days ago AND
-- whose counter has not moved for seven days (see the header). A DELETE that
-- names a column in its WHERE clause is held to the SELECT policies as well,
-- so the purge has a read policy of its own with the same predicate; without
-- it the purge would delete nothing.
CREATE POLICY auth_token_versions_purge  ON public.auth_token_versions FOR DELETE TO ira_app, ira_worker
    USING (updated_at < clock_timestamp() - interval '7 days'
           AND EXISTS (SELECT 1 FROM public.users u
                       WHERE u.id = auth_token_versions.user_id
                         AND u.deleted_at IS NOT NULL
                         AND u.deleted_at < clock_timestamp() - interval '7 days'));
CREATE POLICY auth_token_versions_purge_read ON public.auth_token_versions FOR SELECT TO ira_app, ira_worker
    USING (updated_at < clock_timestamp() - interval '7 days'
           AND EXISTS (SELECT 1 FROM public.users u
                       WHERE u.id = auth_token_versions.user_id
                         AND u.deleted_at IS NOT NULL
                         AND u.deleted_at < clock_timestamp() - interval '7 days'));

-- Not SECURITY DEFINER: it only refuses. `search_path` is pinned all the same,
-- with `pg_temp` last, because a temporary schema is writable by any session
-- (as 080 does; tests/tenancy/test_role_separation.py requires it).
CREATE OR REPLACE FUNCTION public.auth_token_versions_monotonic() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_temp
    AS $$
BEGIN
    IF NEW.version < OLD.version THEN
        RAISE EXCEPTION 'auth_token_versions: version may not decrease (% -> %)', OLD.version, NEW.version;
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;
ALTER FUNCTION public.auth_token_versions_monotonic() OWNER TO ira_schema_owner;

DROP TRIGGER IF EXISTS auth_token_versions_monotonic ON public.auth_token_versions;
CREATE TRIGGER auth_token_versions_monotonic BEFORE UPDATE ON public.auth_token_versions
    FOR EACH ROW EXECUTE FUNCTION public.auth_token_versions_monotonic();

COMMIT;
