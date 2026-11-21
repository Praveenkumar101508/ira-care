-- =============================================================================
-- rollback/059_external_identities_down.sql — reverse 059_external_identities.sql
--
-- Refuses while any account depends on federated sign-in to get in at all.
--
-- ── Why that is the line, and not "any link" ────────────────────────────────
--
-- Dropping this table does not weaken anything: it removes a way IN, which is
-- the safe direction. The loss is availability, and it is total for exactly one
-- group — accounts whose ONLY sign-in method is an external identity. They have
-- no password row in `user_credentials`, so after this script they cannot
-- authenticate by any means, and no self-service flow can help them: password
-- reset needs a verified address AND a credential row to replace, and the
-- account has neither. That is a permanent lockout requiring operator repair.
--
-- An account that ALSO has a password is a different case entirely. It loses a
-- convenience and keeps its way in, so those links are not a reason to refuse —
-- refusing on them would make this script unusable on any deployment that has
-- ever offered federated sign-in.
--
-- Counted with FORCE lifted, because this script binds no GUCs and this table's
-- policies key on the identity or the user — so an unbound connection matches
-- ZERO rows, always. The P2-05 shape: the guard would report "nobody depends on
-- this" on every deployment in existence, including one where every account is
-- federated.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_stranded bigint;
BEGIN
    IF to_regclass('public.external_identities') IS NULL THEN
        RETURN;   -- already reversed
    END IF;

    EXECUTE 'ALTER TABLE external_identities NO FORCE ROW LEVEL SECURITY';
    SELECT count(DISTINCT e.user_id) INTO n_stranded
      FROM external_identities e
      JOIN users u ON u.id = e.user_id
     WHERE u.deleted_at IS NULL
       AND u.status = 'active'
       AND NOT EXISTS (
           SELECT 1 FROM user_credentials c WHERE c.user_id = e.user_id);
    EXECUTE 'ALTER TABLE external_identities FORCE ROW LEVEL SECURITY';

    IF n_stranded > 0 THEN
        RAISE EXCEPTION
            '059-down REFUSING: % active account(s) can only sign in through '
            'an external provider.', n_stranded
            USING HINT =
                'They hold no password, so dropping this table locks them out '
                'permanently and no self-service flow can recover them — a '
                'password reset needs both a verified address and a credential '
                'row to replace, and these accounts have neither. Accounts that '
                'also have a password are not affected and do not block this. '
                'Give the stranded accounts a credential first, or delete them '
                'deliberately, and record which.';
    END IF;
END $$;

DROP TRIGGER IF EXISTS external_identity_pin_key_trg ON external_identities;
DROP FUNCTION IF EXISTS external_identity_pin_key();

DROP POLICY IF EXISTS external_identity_delete ON external_identities;
DROP POLICY IF EXISTS external_identity_update ON external_identities;
DROP POLICY IF EXISTS external_identity_insert ON external_identities;
DROP POLICY IF EXISTS external_identity_select ON external_identities;

DROP INDEX IF EXISTS external_identities_user_idx;
DROP INDEX IF EXISTS external_identities_one_per_issuer_per_user;
DROP INDEX IF EXISTS external_identities_issuer_subject_key;

DROP TABLE IF EXISTS external_identities;

DO $$
BEGIN
    IF to_regclass('public.external_identities') IS NOT NULL THEN
        RAISE EXCEPTION '059-down: external_identities still exists';
    END IF;
    -- 059 creates one table and one function. Nothing else may have gone with
    -- them — least of all the credential table, which is what the accounts
    -- surviving this rollback are relying on.
    IF to_regclass('public.user_credentials') IS NULL THEN
        RAISE EXCEPTION
            '059-down destroyed user_credentials. Every account that survives '
            'this rollback signs in with a password.';
    END IF;
    IF to_regclass('public.users') IS NULL THEN
        RAISE EXCEPTION '059-down destroyed users';
    END IF;
    IF to_regclass('public.password_reset_tokens') IS NULL THEN
        RAISE EXCEPTION '059-down destroyed password_reset_tokens';
    END IF;
END $$;

COMMIT;
