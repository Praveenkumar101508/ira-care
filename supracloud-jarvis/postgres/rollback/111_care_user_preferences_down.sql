-- Rollback of 111_care_user_preferences.sql: appearance only, nothing about care; safe to drop.
BEGIN;
DROP TABLE IF EXISTS care_user_preferences;
COMMIT;
