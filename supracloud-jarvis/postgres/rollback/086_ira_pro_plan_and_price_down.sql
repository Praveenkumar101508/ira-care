-- =============================================================================
-- 086_ira_pro_plan_and_price_down.sql — reverse of 086
--
-- Removes the IRA Pro price, entitlements and plan, returning the deployment to
-- the state every phase before this one was in: **no price row, therefore
-- `PRICE_NOT_CONFIGURED`, never free.**
--
-- ── This refuses while anyone is subscribed, and that is the point ──────────
--
-- `tenant_subscriptions.plan_id` REFERENCES `billing_plans(plan_id)` with no
-- cascade, so deleting a plan somebody is on would fail on the foreign key with
-- a message about a constraint rather than about the actual problem. The
-- explicit check below says the actual problem: reversing this migration while
-- a tenant holds a live IRA Pro subscription would leave that subscription
-- unexplainable — the row would name a plan that no longer exists, and no
-- invoice issued under it could be reconstructed.
--
-- Cancel or migrate those subscriptions first. That is a commercial act, not a
-- schema one, which is why this file will not do it silently.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    live INTEGER;
BEGIN
    SELECT count(*) INTO live
      FROM tenant_subscriptions
     WHERE plan_id = 'ira_pro' AND status <> 'canceled';

    IF live > 0 THEN
        RAISE EXCEPTION
            'Refusing rollback: % tenant(s) hold a live ira_pro subscription. '
            'Removing the plan would leave those rows naming a plan that does '
            'not exist, and no invoice issued under it could be reconstructed. '
            'Cancel or migrate them first — a commercial act, not a schema one.',
            live;
    END IF;
END $$;

-- Children first: prices and entitlements both reference the plan.
DELETE FROM billing_plan_prices
 WHERE plan_id = 'ira_pro'
   AND currency = 'GBP'
   AND recurrence = 'monthly'
   AND effective_from = TIMESTAMPTZ '2026-08-15 00:00:00+00';

DELETE FROM plan_entitlements WHERE plan_id = 'ira_pro';

DELETE FROM billing_plans WHERE plan_id = 'ira_pro';

COMMIT;
