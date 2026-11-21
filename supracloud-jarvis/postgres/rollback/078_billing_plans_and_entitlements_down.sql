-- =============================================================================
-- rollback/078_billing_plans_and_entitlements_down.sql
--
-- Drops the Phase 11 commercial layer.
--
-- ── Read this before running it ────────────────────────────────────────────
--
-- Reversing this on an empty deployment is harmless: the tables carry no seeded
-- plans and no seeded prices, so there is nothing to lose.
--
-- Once a tenant is SUBSCRIBED it is not harmless, and this refuses. A
-- subscription is the record of a commercial relationship — what the customer
-- agreed to, when it started, which period they are in and whether they
-- cancelled. Dropping the table does not "undo" that relationship; it deletes
-- the only record of it while the relationship continues to exist in the world.
--
-- Prices are the same argument twice over. `billing_plan_prices` is append-only
-- history precisely so an invoice issued last quarter stays explicable, and
-- dropping it makes every past invoice unreconcilable while leaving the invoices
-- themselves in place.
--
-- So: reversible while unused, refusing once used. Same shape as 051, 055 and
-- 056 — the refusal names what exists and how to proceed deliberately.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_subs   BIGINT := 0;
    n_prices BIGINT := 0;
BEGIN
    IF to_regclass('public.tenant_subscriptions') IS NOT NULL THEN
        -- Counted as the schema owner, which FORCE RLS would otherwise filter
        -- to zero — the failure mode that makes a guard read "nothing here" on
        -- a table full of rows (finding class 6).
        EXECUTE 'ALTER TABLE tenant_subscriptions NO FORCE ROW LEVEL SECURITY';
        SELECT count(*) INTO n_subs FROM tenant_subscriptions;
        EXECUTE 'ALTER TABLE tenant_subscriptions FORCE ROW LEVEL SECURITY';
    END IF;

    IF to_regclass('public.billing_plan_prices') IS NOT NULL THEN
        SELECT count(*) INTO n_prices FROM billing_plan_prices;
    END IF;

    IF n_subs > 0 OR n_prices > 0 THEN
        RAISE EXCEPTION
            '078-down REFUSING: % subscription(s) and % price row(s) exist. '
            'A subscription is the record of a commercial relationship and a '
            'price row is what makes an already-issued invoice explicable; '
            'dropping either deletes the record while the obligation it '
            'describes continues to exist.',
            n_subs, n_prices
        USING HINT =
            'Export both first: SELECT * FROM tenant_subscriptions; SELECT * '
            'FROM billing_plan_prices. If a tenant should stop being billed, '
            'cancel the subscription (status=''canceled'' with canceled_at) '
            'rather than deleting the row — "never subscribed" and "cancelled" '
            'must not look identical.';
    END IF;
END $$;

DROP TABLE IF EXISTS tenant_entitlement_overrides;
DROP TABLE IF EXISTS tenant_subscriptions;
DROP TABLE IF EXISTS plan_entitlements;
DROP TABLE IF EXISTS billing_plan_prices;
DROP TABLE IF EXISTS billing_plans;

COMMIT;
