-- Rollback of 105_care_onboarding.sql.
BEGIN;
DELETE FROM plan_entitlements WHERE plan_id IN ('care_family', 'care_organisation');
DELETE FROM billing_plans WHERE plan_id IN ('care_family', 'care_organisation');
DROP INDEX IF EXISTS idx_care_circle_members_invitation;
ALTER TABLE care_circle_members DROP COLUMN IF EXISTS invitation_id, DROP COLUMN IF EXISTS invited_email;
COMMIT;
