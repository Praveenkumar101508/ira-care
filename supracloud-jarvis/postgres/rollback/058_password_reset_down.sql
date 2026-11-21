-- =============================================================================
-- rollback/058_password_reset_down.sql — reverse 058_password_reset.sql
--
-- Refuses while any UNCONSUMED, UNEXPIRED token exists.
--
-- ── Why an open token is the line ───────────────────────────────────────────
--
-- Unlike `055`, dropping this table destroys no accountability record: a
-- consumed reset token proves nothing that `user_credentials.updated_at` does
-- not already say. The loss is availability, and it lands on exactly the people
-- least able to absorb it — somebody who has lost their password and is holding
-- a link that is about to stop working, with no way to tell that it did.
--
-- They are not locked out for ever; they can request another once the table is
-- back. But between the drop and the re-request the link fails silently, which
-- during an incident is indistinguishable from "the reset flow is broken" and
-- generates exactly the support traffic a rollback is trying to avoid.
--
-- EXPIRED and CONSUMED tokens are not a reason to refuse. Neither grants
-- anything, so keeping the table for their sake would make this script unusable
-- on any deployment that has ever issued a reset.
--
-- Counted with FORCE lifted, because this script binds no GUCs and this table's
-- policies key on POSSESSION of a token hash — so an unbound connection matches
-- ZERO rows, always, by design. That is the P2-05 shape in its purest form
-- here: the guard would report "no open tokens" on every deployment in
-- existence, including one mid-incident with a thousand of them.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_open bigint;
BEGIN
    IF to_regclass('public.password_reset_tokens') IS NULL THEN
        RETURN;   -- already reversed
    END IF;

    EXECUTE 'ALTER TABLE password_reset_tokens NO FORCE ROW LEVEL SECURITY';
    SELECT count(*) INTO n_open
      FROM password_reset_tokens
     WHERE consumed_at IS NULL AND expires_at > NOW();
    EXECUTE 'ALTER TABLE password_reset_tokens FORCE ROW LEVEL SECURITY';

    IF n_open > 0 THEN
        RAISE EXCEPTION
            '058-down REFUSING: % unexpired reset link(s) are outstanding.', n_open
            USING HINT =
                'Each is somebody who has lost their password and is holding a '
                'link that would stop working silently. They are not locked '
                'out permanently — they can request another once this table is '
                'back — but between the drop and the re-request the link fails '
                'in a way indistinguishable from a broken reset flow. Expired '
                'and consumed tokens do not block this; live ones do. Wait for '
                'them to expire, or consume them deliberately and record why.';
    END IF;
END $$;

DROP POLICY IF EXISTS password_reset_token_delete ON password_reset_tokens;
DROP POLICY IF EXISTS password_reset_token_update ON password_reset_tokens;
DROP POLICY IF EXISTS password_reset_token_insert ON password_reset_tokens;
DROP POLICY IF EXISTS password_reset_token_select ON password_reset_tokens;

DROP INDEX IF EXISTS password_reset_tokens_one_open_per_user;
DROP INDEX IF EXISTS password_reset_tokens_token_hash_key;

DROP TABLE IF EXISTS password_reset_tokens;

DO $$
BEGIN
    IF to_regclass('public.password_reset_tokens') IS NOT NULL THEN
        RAISE EXCEPTION '058-down: password_reset_tokens still exists';
    END IF;
    -- 058 creates nothing else, so nothing else may have gone with it. The
    -- credential table especially: this rollback runs on a deployment whose
    -- users still need to sign in.
    IF to_regclass('public.user_credentials') IS NULL THEN
        RAISE EXCEPTION
            '058-down destroyed user_credentials. The reset-token table is the '
            'only thing 058 created.';
    END IF;
    IF to_regclass('public.email_verification_tokens') IS NULL THEN
        RAISE EXCEPTION '058-down destroyed email_verification_tokens';
    END IF;
    IF to_regclass('public.user_sessions') IS NULL THEN
        RAISE EXCEPTION '058-down destroyed user_sessions';
    END IF;
END $$;

COMMIT;
