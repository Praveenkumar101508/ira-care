-- =============================================================================
-- rollback/056_mfa_recovery_down.sql — reverse 056_mfa_recovery.sql
--
-- Refuses while any account still holds an UNUSED recovery code.
--
-- Dropping the table does not weaken authentication — it strengthens it, by
-- removing a way in. That is exactly why the refusal is here: the people
-- holding those codes are the ones who have already lost their authenticator,
-- or who are one lost phone away from needing them. Destroying the only way
-- back for an account with an enrolled second factor is a lockout, and a
-- lockout is not a rollback.
--
-- CONSUMED codes are not a reason to refuse. A spent code grants nothing, and
-- the record that it was spent belongs with the audit trail rather than here.
--
-- Counted with FORCE lifted, because this script binds no GUCs and a forced
-- table read on an unbound connection matches ZERO rows silently — the P2-05
-- shape, which would make this guard pass while every live code was destroyed.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_unused bigint;
BEGIN
    IF to_regclass('public.mfa_recovery_codes') IS NULL THEN
        RETURN;
    END IF;

    EXECUTE 'ALTER TABLE mfa_recovery_codes NO FORCE ROW LEVEL SECURITY';
    SELECT count(*) INTO n_unused
      FROM mfa_recovery_codes WHERE consumed_at IS NULL;
    EXECUTE 'ALTER TABLE mfa_recovery_codes FORCE ROW LEVEL SECURITY';

    IF n_unused > 0 THEN
        RAISE EXCEPTION
            '056-down REFUSING: % unused recovery code(s) exist.', n_unused
            USING HINT =
                'Those are the only way back for accounts whose second factor '
                'is lost. Dropping them is a lockout, not a rollback — and it '
                'cannot be undone, because the plaintext was shown once and '
                'never stored. Consumed codes do not block this; unused ones '
                'do. If you genuinely intend it, have those users regenerate '
                'or disable their second factor first.';
    END IF;
END $$;

DROP POLICY IF EXISTS mfa_recovery_code_delete ON mfa_recovery_codes;
DROP POLICY IF EXISTS mfa_recovery_code_update ON mfa_recovery_codes;
DROP POLICY IF EXISTS mfa_recovery_code_insert ON mfa_recovery_codes;
DROP POLICY IF EXISTS mfa_recovery_code_select ON mfa_recovery_codes;

DROP INDEX IF EXISTS mfa_recovery_codes_user_idx;
DROP TABLE IF EXISTS mfa_recovery_codes;

DO $$
BEGIN
    IF to_regclass('public.mfa_recovery_codes') IS NOT NULL THEN
        RAISE EXCEPTION '056-down: mfa_recovery_codes still exists';
    END IF;
    -- 056 creates nothing else. Asserted because a rollback that quietly took
    -- a neighbouring table with it is what this pair exists to make impossible.
    IF to_regclass('public.user_mfa_keys') IS NULL THEN
        RAISE EXCEPTION '056-down destroyed user_mfa_keys';
    END IF;
    IF to_regclass('public.totp_secrets') IS NULL THEN
        RAISE EXCEPTION '056-down destroyed totp_secrets';
    END IF;
END $$;

COMMIT;
