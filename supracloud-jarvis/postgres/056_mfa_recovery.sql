-- =============================================================================
-- 056_mfa_recovery.sql — Phase 2 batch 7, MFA recovery codes
--
-- P2-05 encrypted the second factor and made an undeterminable enrolment refuse
-- the login. Both are correct, and together they mean a user who loses their
-- authenticator is locked out with no way back — the stronger the factor, the
-- worse that gets. Recovery codes are the way back, and they have to be exactly
-- as well protected as the factor they substitute for.
--
-- ── Isolation mirrors user_mfa_keys, deliberately ──────────────────────────
--
-- Four policies, each requiring `app.tenant_id` AND `app.user_id` — the same
-- pair `053` gave the MFA keys and `036` gave the TOTP secrets. A recovery code
-- IS a second factor: a tenant administrator who could read one could sign in
-- as any member of their tenant, which is precisely the capability 036's
-- user-keyed policy exists to withhold.
--
-- ── Why bcrypt and not SHA-256 ──────────────────────────────────────────────
--
-- `054` and `055` store SHA-256 of their tokens, and both explain why: a
-- 256-bit random token is not guessable, so the only property needed is that
-- the stored form cannot be presented, and a slow hash on an unauthenticated
-- path is a denial-of-service lever.
--
-- A recovery code is different in the one way that matters. It is TYPED BY A
-- HUMAN, so it cannot be 256 bits — this implementation uses ~50 bits, which is
-- ample against online guessing and NOT ample against an offline attack on a
-- stolen table. That is the low-entropy case, and it is what a deliberately
-- slow hash is for. The DoS argument does not apply: verification happens only
-- after a correct password, behind the login rate limit.
--
-- The CHECK enforces the shape rather than the choice — `$2` is bcrypt's
-- prefix, so a code stored in the clear cannot be written to this column.
--
-- ── Generations, so regeneration is not a delete-then-insert race ───────────
--
-- Regenerating invalidates every earlier code. Modelled as a monotonic
-- `generation` per user rather than by deleting rows, because a crash between
-- the delete and the insert would leave an account with NO recovery codes and
-- no record that it ever had any. The consuming query requires the current
-- generation, so superseded rows are inert the moment a new set is written,
-- whether or not the old rows are ever cleaned up.
--
-- Rollback: rollback/056_mfa_recovery_down.sql
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS mfa_recovery_codes (
    id          uuid        PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- Derived from the bound transaction, never passed.
    tenant_id   uuid        NOT NULL
                DEFAULT (NULLIF(current_setting('app.tenant_id', true), ''))::uuid
                REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash   text        NOT NULL,
    generation  integer     NOT NULL DEFAULT 1,
    consumed_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT NOW()
);

ALTER TABLE mfa_recovery_codes DROP CONSTRAINT IF EXISTS mfa_recovery_codes_hash_is_not_plaintext;
-- bcrypt output starts `$2`. A recovery code written in the clear is not a
-- weaker hash — it is the whole secret, so this makes it unrepresentable.
ALTER TABLE mfa_recovery_codes ADD CONSTRAINT mfa_recovery_codes_hash_is_not_plaintext
    CHECK (code_hash LIKE '$2%' AND length(code_hash) >= 50);

ALTER TABLE mfa_recovery_codes DROP CONSTRAINT IF EXISTS mfa_recovery_codes_generation_positive;
ALTER TABLE mfa_recovery_codes ADD CONSTRAINT mfa_recovery_codes_generation_positive
    CHECK (generation >= 1);

-- Generated, so "is this code still usable" is the database's answer — the same
-- reasoning that made `membership_grants_access` generated in 040.
ALTER TABLE mfa_recovery_codes DROP COLUMN IF EXISTS code_is_unused;
ALTER TABLE mfa_recovery_codes
    ADD COLUMN code_is_unused boolean
    GENERATED ALWAYS AS (consumed_at IS NULL) STORED;

CREATE INDEX IF NOT EXISTS mfa_recovery_codes_user_idx
    ON mfa_recovery_codes (user_id, generation) WHERE code_is_unused;

ALTER TABLE mfa_recovery_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE mfa_recovery_codes FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS mfa_recovery_code_select ON mfa_recovery_codes;
DROP POLICY IF EXISTS mfa_recovery_code_insert ON mfa_recovery_codes;
DROP POLICY IF EXISTS mfa_recovery_code_update ON mfa_recovery_codes;
DROP POLICY IF EXISTS mfa_recovery_code_delete ON mfa_recovery_codes;

-- BOTH predicates on every policy. The tenant alone would let a tenant admin
-- read a member's recovery codes, which is the 036 defect; the user alone would
-- leave the row reachable from a connection bound to another tenant if a user
-- id were ever reused across one.
CREATE POLICY mfa_recovery_code_select ON mfa_recovery_codes FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid);

CREATE POLICY mfa_recovery_code_insert ON mfa_recovery_codes FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid);

CREATE POLICY mfa_recovery_code_update ON mfa_recovery_codes FOR UPDATE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid);

CREATE POLICY mfa_recovery_code_delete ON mfa_recovery_codes FOR DELETE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
       AND user_id   = NULLIF(current_setting('app.user_id',   true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON mfa_recovery_codes TO ira_app;

-- REQUIRED. Default privileges grant every new table to ira_worker, so this is
-- opt-OUT — 054 found that by asserting the end state, not by reading the
-- provisioning script. A background job has no business holding a second factor.
REVOKE ALL ON mfa_recovery_codes FROM ira_worker;

DO $$
DECLARE
    n integer;
BEGIN
    IF to_regclass('public.mfa_recovery_codes') IS NULL THEN
        RAISE EXCEPTION '056: mfa_recovery_codes was not created';
    END IF;
    IF NOT (SELECT relforcerowsecurity FROM pg_class
             WHERE oid = 'public.mfa_recovery_codes'::regclass) THEN
        RAISE EXCEPTION '056: mfa_recovery_codes is not FORCE ROW LEVEL SECURITY';
    END IF;

    SELECT count(*) INTO n FROM pg_policy
     WHERE polrelid = 'public.mfa_recovery_codes'::regclass;
    IF n <> 4 THEN
        RAISE EXCEPTION
            '056: mfa_recovery_codes has % policies, expected exactly 4. '
            'Permissive policies combine with OR, so a fifth can only widen '
            'access to second-factor material.', n;
    END IF;

    -- EVERY policy must constrain BOTH. One that named only the tenant would
    -- hand every tenant administrator their members'' recovery codes, and would
    -- look like protection while doing it.
    SELECT count(*) INTO n FROM pg_policy
     WHERE polrelid = 'public.mfa_recovery_codes'::regclass
       AND (coalesce(pg_get_expr(polqual, polrelid), '')
         || coalesce(pg_get_expr(polwithcheck, polrelid), '') NOT LIKE '%app.user_id%'
        OR  coalesce(pg_get_expr(polqual, polrelid), '')
         || coalesce(pg_get_expr(polwithcheck, polrelid), '') NOT LIKE '%app.tenant_id%');
    IF n <> 0 THEN
        RAISE EXCEPTION
            '056: % mfa_recovery_codes policy(ies) do not constrain BOTH '
            'app.user_id and app.tenant_id', n;
    END IF;

    IF has_table_privilege('ira_worker', 'mfa_recovery_codes', 'SELECT') THEN
        RAISE EXCEPTION
            '056: ira_worker can read mfa_recovery_codes. Default privileges '
            'grant every new table to the worker, so the REVOKE is required.';
    END IF;
    IF NOT has_table_privilege('ira_app', 'mfa_recovery_codes', 'SELECT') THEN
        RAISE EXCEPTION '056: ira_app cannot read mfa_recovery_codes';
    END IF;

    -- The plaintext guard, asserted against the constraint rather than by
    -- attempting an INSERT: this script binds no GUCs, so the INSERT policy
    -- would deny first and the assertion would pass with the CHECK deleted.
    -- That exact mistake was made in 055 and caught by its first run.
    IF (SELECT pg_get_constraintdef(oid) FROM pg_constraint
         WHERE conrelid = 'public.mfa_recovery_codes'::regclass
           AND conname = 'mfa_recovery_codes_hash_is_not_plaintext') IS NULL THEN
        RAISE EXCEPTION '056: the plaintext guard is missing';
    END IF;
END $$;

COMMIT;
