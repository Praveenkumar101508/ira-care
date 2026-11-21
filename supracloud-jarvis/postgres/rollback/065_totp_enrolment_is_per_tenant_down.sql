-- =============================================================================
-- rollback/065_totp_enrolment_is_per_tenant_down.sql
--
-- Restores `036`'s global unique on `user_id`.
--
-- ── It REFUSES rather than choosing an enrolment to destroy ────────────────
--
-- Going backwards narrows the constraint. If any user has enrolled a second
-- factor in more than one tenant since `065` — which is the entire point of
-- `065` — the global index cannot be built, and the only way to make it
-- buildable is to delete somebody's second factor.
--
-- This script will not do that, and it will not do it silently by picking
-- "the oldest" or "the one in the default tenant" either. It reports who is
-- affected by COUNT, names nobody, and stops. Revoking an enrolment is a
-- decision a person makes with the user, not a side effect of a schema change.
--
-- The refusal is what makes this reversible in the only sense that matters: on
-- a database where no user has enrolled twice, it reverses cleanly; on one
-- where somebody has, it fails loudly instead of locking them out.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    affected INTEGER;
BEGIN
    SELECT count(*) INTO affected FROM (
        SELECT user_id FROM totp_secrets
         WHERE user_id IS NOT NULL AND revoked_at IS NULL
         GROUP BY user_id HAVING count(*) > 1
    ) AS multi;

    IF affected > 0 THEN
        RAISE EXCEPTION
            '065-down REFUSING: % user(s) hold a live second factor in more '
            'than one tenant. Restoring the global unique index would require '
            'destroying one enrolment each, locking those people out of an '
            'organisation.', affected
        USING HINT =
            'Revoke the surplus enrolments deliberately, with the users, by '
            'setting revoked_at — then re-run. Do not DELETE the rows: a '
            'revoked enrolment is the audit trail for a second factor that '
            'existed.';
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS totp_secrets_user_key
    ON totp_secrets (user_id)
    WHERE user_id IS NOT NULL AND revoked_at IS NULL;

DROP INDEX IF EXISTS totp_secrets_tenant_user_key;

COMMIT;
