-- =============================================================================
-- 087_billing_provider_lifecycle_down.sql — reverse of 087
--
-- Drops the checkout-session table and the two subscription columns.
--
-- ── What this loses, said plainly ───────────────────────────────────────────
--
-- `billing_checkout_sessions` is DROPPED, and with it every record that a
-- person tried to pay. Unlike most rollbacks in this repository this one
-- destroys evidence rather than a constraint, so it refuses while any session
-- is still open — an open session is a payment that may yet complete, and a
-- completed-checkout webhook arriving after this ran would have nothing to
-- attribute itself to.
--
-- `provider_subscription_ref` going away means an existing subscription can no
-- longer be matched to the provider's own object. Subscriptions themselves
-- survive; what is lost is the ability to route a provider event to one
-- deterministically, which is the state `080` left the system in.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    open_sessions INTEGER;
BEGIN
    IF to_regclass('public.billing_checkout_sessions') IS NULL THEN
        RETURN;
    END IF;

    EXECUTE 'SELECT count(*) FROM billing_checkout_sessions WHERE status = ''open'''
       INTO open_sessions;

    IF open_sessions > 0 THEN
        RAISE EXCEPTION
            'Refusing rollback: % checkout session(s) are still open. Each is a '
            'payment that may yet complete, and the webhook that completes it '
            'would have nothing to attribute itself to. Let them expire or mark '
            'them expired first.',
            open_sessions;
    END IF;
END $$;

DROP TABLE IF EXISTS billing_checkout_sessions;

ALTER TABLE tenant_subscriptions
    DROP CONSTRAINT IF EXISTS subscription_cancel_flag_sane;

DROP INDEX IF EXISTS tenant_subscriptions_provider_ref;

ALTER TABLE tenant_subscriptions
    DROP COLUMN IF EXISTS provider_subscription_ref,
    DROP COLUMN IF EXISTS cancel_at_period_end;

COMMIT;
