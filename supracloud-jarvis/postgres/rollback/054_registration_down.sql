-- =============================================================================
-- rollback/054_registration_down.sql
--
-- Reverses 054: drops `user_credentials` and `email_verification_tokens`.
--
-- ── It REFUSES once any account depends on these tables ─────────────────────
--
-- Dropping `user_credentials` deletes the only copy of every registered user's
-- password hash. That is not a rollback — a hash cannot be reconstructed, so
-- every account created through registration becomes permanently unable to
-- authenticate, with no recovery path short of a password reset flow that does
-- not exist yet.
--
-- The guard counts credentials rather than tokens: a verification token is
-- reissuable and losing one costs an email, while losing a credential costs the
-- account. Tokens are dropped without complaint for the same reason.
--
-- The legitimate use is unwinding an upgrade before anyone has registered.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_credentials bigint;
BEGIN
    IF to_regclass('public.user_credentials') IS NOT NULL THEN
        -- FORCE RLS applies to the table owner too, and this script binds no
        -- GUCs — so without lifting it the count is zero and the guard passes
        -- while every credential is about to be destroyed. That is the exact
        -- shape of the P2-05 defect (a forced table read on an unbound
        -- connection returns nothing, silently), so it is lifted deliberately
        -- for the count and restored immediately.
        EXECUTE 'ALTER TABLE user_credentials NO FORCE ROW LEVEL SECURITY';
        SELECT count(*) INTO n_credentials FROM user_credentials;
        EXECUTE 'ALTER TABLE user_credentials FORCE ROW LEVEL SECURITY';

        IF n_credentials > 0 THEN
            RAISE EXCEPTION
                '054-down REFUSING: % registered account(s) hold credentials.',
                n_credentials
                USING HINT =
                    'Dropping user_credentials destroys the only copy of those '
                    'password hashes. They cannot be reconstructed, and there '
                    'is no password-reset flow yet, so every one of those '
                    'accounts would be permanently unable to authenticate. '
                    'This is data loss, not a rollback.';
        END IF;
    END IF;
END
$$;

DROP TABLE IF EXISTS email_verification_tokens;
DROP TABLE IF EXISTS user_credentials;

DO $$
BEGIN
    IF to_regclass('public.user_credentials') IS NOT NULL THEN
        RAISE EXCEPTION '054-down FAILED: user_credentials still exists.';
    END IF;
    IF to_regclass('public.email_verification_tokens') IS NOT NULL THEN
        RAISE EXCEPTION '054-down FAILED: email_verification_tokens still exists.';
    END IF;
END
$$;

COMMIT;
