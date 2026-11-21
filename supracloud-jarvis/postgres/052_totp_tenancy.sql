-- =============================================================================
-- 052_totp_tenancy.sql — P2-05
--
-- `totp_secrets` gains an authoritative tenant, and its row-level security
-- becomes per-command with BOTH predicates required.
--
-- ── What was already true, and must stay true ────────────────────────────────
--
-- 036 gave this table `user_id`, RLS, FORCE RLS and one `FOR ALL` policy:
--
--     user_private  USING (user_id = app.user_id)
--
-- and its header states the reason in terms that still hold:
--
--     "A tenant admin must not be able to read another person's second factor,
--      so the predicate is on `app.user_id`, NOT on `app.tenant_id`. A
--      tenant-only predicate would let any admin in the tenant read every
--      member's TOTP secret, which is exactly the shape of the bug being fixed."
--
-- That is correct and this migration does not touch it. **The tenant predicate
-- is ANDed onto the user predicate, never ORed beside it.**
--
-- This matters more than it looks. PostgreSQL combines multiple *permissive*
-- policies with OR. Adding a separate `tenant_isolation` policy next to
-- `user_private` — the shape `delegated_users` and `user_sessions` use, and the
-- obvious thing to copy — would make a row visible when EITHER predicate holds.
-- Every tenant admin would regain read access to every member's second factor,
-- and the table would look more protected than before while being less. The
-- four policies below are therefore strictly NARROWER than the one they
-- replace: everything visible after this migration was visible before it.
--
-- ── Why a tenant at all, if the user predicate is already stronger ───────────
--
-- Defence in depth, and operations. The user predicate protects the secret from
-- other people; the tenant column is what lets a tenant be exported, erased or
-- billed without a join through `tenant_memberships`, and it is a second
-- independent boundary if `app.user_id` is ever bound without `app.tenant_id`.
-- `delegated_users` and `user_sessions` already carry both columns; this brings
-- `totp_secrets` in line.
--
-- ── Backfill: derived, never guessed ────────────────────────────────────────
--
-- Ownership resolves through `tenant_memberships`, the authoritative mapping.
-- Where that cannot answer, a deployment holding exactly ONE tenant is
-- unambiguous and that tenant is used — a private-edition install, where the
-- answer is not a guess. Anything else REFUSES, and refuses loudly.
--
-- 036 set that precedent for this table and the reasoning is unchanged:
-- guessing an owner for a second-factor secret is the worst possible silent
-- failure. A row attributed to the wrong tenant is an authentication bypass,
-- not a mislabelled record.
--
-- Rollback: rollback/052_totp_tenancy_down.sql
-- =============================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Expand
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE totp_secrets ADD COLUMN IF NOT EXISTS tenant_id uuid;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Backfill
--
--    FORCE is lifted for the backfill and restored in section 5. This is 036's
--    lesson and 045's defect: a migration binds no `app.user_id`, so under
--    FORCE RLS the existing `user_private` policy evaluates to NULL for every
--    row and an UPDATE matches ZERO of them — silently, reporting success.
--    Section 3 asserts the backfill actually happened, so a regression here
--    fails loudly rather than shipping a NULL column.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE totp_secrets NO FORCE ROW LEVEL SECURITY;

-- Authoritative: an active membership names the tenant this user belongs to.
UPDATE totp_secrets t
   SET tenant_id = m.tenant_id
  FROM tenant_memberships m
 WHERE m.user_id = t.user_id
   AND m.revoked_at IS NULL
   AND t.tenant_id IS NULL;

-- Unambiguous: a deployment with exactly one tenant has exactly one answer.
-- This is the private edition, and it is a derivation rather than a guess.
DO $$
DECLARE
    only_tenant uuid;
    n_tenants   bigint;
BEGIN
    SELECT count(*) INTO n_tenants FROM tenants;
    IF n_tenants = 1 THEN
        SELECT id INTO only_tenant FROM tenants;
        UPDATE totp_secrets SET tenant_id = only_tenant WHERE tenant_id IS NULL;
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Refuse rather than guess
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    orphans bigint;
BEGIN
    SELECT count(*) INTO orphans FROM totp_secrets WHERE tenant_id IS NULL;
    IF orphans > 0 THEN
        RAISE EXCEPTION
            '052 REFUSING: % TOTP row(s) have no derivable tenant.', orphans
            USING HINT =
                'These rows have no active tenant_memberships entry for their '
                'user_id, and the deployment holds more than one tenant, so '
                'there is no unambiguous answer. Attributing a second factor to '
                'the wrong tenant is an authentication bypass, not a '
                'mislabelled record. Resolve the memberships for these users '
                '(or revoke the stale enrolments) and re-run. Query them with: '
                'SELECT id, username, user_id FROM totp_secrets '
                'WHERE tenant_id IS NULL;';
    END IF;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Constrain
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE totp_secrets ALTER COLUMN tenant_id SET NOT NULL;

ALTER TABLE totp_secrets
    DROP CONSTRAINT IF EXISTS totp_secrets_tenant_id_fkey;
ALTER TABLE totp_secrets
    ADD CONSTRAINT totp_secrets_tenant_id_fkey
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_totp_secrets_tenant ON totp_secrets (tenant_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Row-level security — USER_PRIVATE, now also tenant-bound
--
--    Per-command rather than FOR ALL, because the four are worth stating
--    separately, and every one of them carries BOTH predicates.
--
--    Read the AND in each policy as load-bearing. Splitting these into a
--    user-scoped policy and a tenant-scoped policy would OR them, and the OR is
--    the vulnerability: `tenant_id = app.tenant_id` alone is true for every
--    member of the tenant, which is precisely the tenant-admin read that 036
--    exists to prevent.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE totp_secrets ENABLE ROW LEVEL SECURITY;
ALTER TABLE totp_secrets FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS user_private          ON totp_secrets;
DROP POLICY IF EXISTS user_private_select   ON totp_secrets;
DROP POLICY IF EXISTS user_private_insert   ON totp_secrets;
DROP POLICY IF EXISTS user_private_update   ON totp_secrets;
DROP POLICY IF EXISTS user_private_delete   ON totp_secrets;

CREATE POLICY user_private_select ON totp_secrets FOR SELECT
    USING (
        user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid
    AND tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    );

CREATE POLICY user_private_insert ON totp_secrets FOR INSERT
    WITH CHECK (
        user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid
    AND tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    );

CREATE POLICY user_private_update ON totp_secrets FOR UPDATE
    USING (
        user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid
    AND tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    )
    WITH CHECK (
        user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid
    AND tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    );

CREATE POLICY user_private_delete ON totp_secrets FOR DELETE
    USING (
        user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid
    AND tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    );

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Assert the end state
--
--    A migration that reported success while leaving RLS off, or leaving a
--    permissive tenant-only policy behind, would be worse than one that failed.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    n_policies bigint;
    forced     boolean;
BEGIN
    SELECT relforcerowsecurity INTO forced
      FROM pg_class WHERE oid = 'public.totp_secrets'::regclass;
    IF NOT forced THEN
        RAISE EXCEPTION '052 FAILED: FORCE ROW LEVEL SECURITY is not set.';
    END IF;

    SELECT count(*) INTO n_policies
      FROM pg_policy WHERE polrelid = 'public.totp_secrets'::regclass;
    IF n_policies <> 4 THEN
        RAISE EXCEPTION
            '052 FAILED: expected exactly 4 policies on totp_secrets, found %.',
            n_policies
            USING HINT =
                'An extra policy is not additional protection — permissive '
                'policies combine with OR, so a fifth policy can only WIDEN '
                'access. If one is genuinely needed, it must AND both '
                'predicates like the four here.';
    END IF;

    -- Every policy must mention both GUCs. A policy that named only one would
    -- be the OR-widening this migration's header warns about.
    IF EXISTS (
        SELECT 1 FROM pg_policy
         WHERE polrelid = 'public.totp_secrets'::regclass
           AND NOT (
                coalesce(pg_get_expr(polqual, polrelid), '') ||
                coalesce(pg_get_expr(polwithcheck, polrelid), '')
               ) LIKE '%app.user_id%'
    ) THEN
        RAISE EXCEPTION
            '052 FAILED: a policy on totp_secrets does not constrain app.user_id.';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_policy
         WHERE polrelid = 'public.totp_secrets'::regclass
           AND NOT (
                coalesce(pg_get_expr(polqual, polrelid), '') ||
                coalesce(pg_get_expr(polwithcheck, polrelid), '')
               ) LIKE '%app.tenant_id%'
    ) THEN
        RAISE EXCEPTION
            '052 FAILED: a policy on totp_secrets does not constrain app.tenant_id.';
    END IF;
END
$$;

COMMIT;
