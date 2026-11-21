-- Rollback for 034_identity_mapping.sql (Phase 3B Batch 0B).
--
-- Users created by 034's backfill are removed; users that predate it — notably
-- the owner 023 seeded — are kept. `migrated_from` is what distinguishes them,
-- which is why 034 records it.
--
-- Refuses if any Batch 1 table still references a mapped user, because dropping
-- the map while 036/037 depend on its resolutions would leave those tables with
-- user_ids nothing can explain.

-- The guard is written to survive being run AFTER 036/037 have already been
-- rolled back, which is the normal reverse order. Those rollbacks drop the
-- `user_id` columns this check reads, so the check must ask whether the column
-- exists before querying it — otherwise a correct reverse-order rollback fails
-- on a guard that was meant to protect it.
DO $$
DECLARE referencing INTEGER := 0;
        n INTEGER;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'totp_secrets' AND column_name = 'user_id') THEN
        EXECUTE 'SELECT count(*) FROM totp_secrets WHERE user_id IS NOT NULL' INTO n;
        referencing := referencing + n;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'voice_profiles' AND column_name = 'user_id') THEN
        EXECUTE 'SELECT count(*) FROM voice_profiles WHERE user_id IS NOT NULL' INTO n;
        referencing := referencing + n;
    END IF;
    IF referencing > 0 THEN
        RAISE EXCEPTION
            'Refusing rollback: % security-secret row(s) reference a mapped '
            'user. Roll back 036 and 037 first.', referencing;
    END IF;
END
$$;

DROP TABLE IF EXISTS legacy_identity_map;

DELETE FROM users WHERE migrated_from = 'delegated_users';

DROP INDEX IF EXISTS users_verified_email_key;
DROP INDEX IF EXISTS users_external_identity_key;
DROP INDEX IF EXISTS users_legacy_username_key;

ALTER TABLE users DROP COLUMN IF EXISTS migrated_from;
ALTER TABLE users DROP COLUMN IF EXISTS legacy_username;
ALTER TABLE users DROP COLUMN IF EXISTS normalised_email;

DROP FUNCTION IF EXISTS normalise_email(TEXT);
