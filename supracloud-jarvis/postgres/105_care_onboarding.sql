-- =============================================================================
-- 105_care_onboarding.sql — IRA CARE: a care organisation can start on its own
--
--   care_circle_members
--     invitation_id   the tenant invitation (tenant_invitations) that will link
--                     this member to an account when it is accepted
--     invited_email   the address the invitation went to (the same as the
--                     invitation's; kept here so the circle shows who is pending)
--   billing_plans / plan_entitlements
--     care_family       one person, a circle of up to 6
--     care_organisation up to 50 people, a circle of up to 20 each
--     No prices are set: an unpriced plan is PRICE_NOT_CONFIGURED, never free, and
--     the payment provider is off unless configured (core/billing/provider.py).
--
-- No new table, so no new RLS. Rollback: rollback/105_care_onboarding_down.sql
-- =============================================================================

BEGIN;

ALTER TABLE care_circle_members
    ADD COLUMN IF NOT EXISTS invitation_id UUID,
    ADD COLUMN IF NOT EXISTS invited_email TEXT NOT NULL DEFAULT '' CHECK (length(invited_email) <= 254);
CREATE INDEX IF NOT EXISTS idx_care_circle_members_invitation ON care_circle_members (invitation_id) WHERE invitation_id IS NOT NULL;

INSERT INTO billing_plans (plan_id, display_name, description, active) VALUES
    ('care_family', 'IRA CARE Family', 'One person looked after, a care circle of up to six, the watch app, the family app.', TRUE),
    ('care_organisation', 'IRA CARE Organisation', 'Up to fifty people, a care circle of up to twenty each, the carer dashboard, reports and the escalation desk.', TRUE)
ON CONFLICT (plan_id) DO NOTHING;

INSERT INTO plan_entitlements (plan_id, key, limit_value) VALUES
    ('care_family', 'care_people_max', 1),
    ('care_family', 'care_circle_members_max', 6),
    ('care_organisation', 'care_people_max', 50),
    ('care_organisation', 'care_circle_members_max', 20)
ON CONFLICT DO NOTHING;

COMMIT;
