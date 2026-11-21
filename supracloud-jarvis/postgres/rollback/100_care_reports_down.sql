-- Rollback of 100_care_reports.sql: drops the reports table. Reports are
-- derived from the other care tables and can be regenerated.
BEGIN;
DROP TABLE IF EXISTS public.care_reports;
DELETE FROM schema_migrations WHERE name = '100_care_reports.sql';
COMMIT;
