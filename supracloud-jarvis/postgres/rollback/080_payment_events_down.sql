-- =============================================================================
-- rollback/080_payment_events_down.sql
--
-- Drops the payment intake ledger and the customer directory.
--
-- Harmless while nothing has arrived, and REFUSING once any event has been
-- ACTED ON. An event still marked 'received' has changed nothing, so dropping
-- it loses only the fact that a provider spoke. One marked 'applied' is the
-- record of WHY a billing record says what it says, and one marked
-- 'unattributed' is an open question a person still has to answer — a payment
-- arrived for somebody we could not identify, and deleting the table is how
-- that stops being anybody's problem while the money stays received.
--
-- Same shape as 051, 055, 056, 078 and 079: reversible until it matters.
--
-- ── Ordering, which 079 already proved bites ───────────────────────────────
--
-- 080 no longer creates anything that references `tenant_subscriptions` — the
-- directory table that did was removed when the mapping moved to `tenants` —
-- so this rollback does NOT have to precede 078's or 079's on that account.
--
-- It is still peeled first, because it is the newest layer and the cycle peels
-- newest-first as a rule rather than case by case. 079 is why that rule is
-- written down: the dedicated 078 step predated 079, looked untouched by it,
-- and broke because `invoices.subscription_id` had quietly made `rollback/078`
-- non-standalone. CI caught it where reading had not.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    n_acted BIGINT := 0;
BEGIN
    IF to_regclass('public.payment_events') IS NOT NULL THEN
        -- No FORCE RLS toggle is needed here, unlike 079's guard: this table
        -- carries no row policy at all, precisely because intake happens before
        -- a tenant is known. The count is therefore already complete.
        SELECT count(*) INTO n_acted FROM payment_events
         WHERE outcome <> 'received';
    END IF;

    IF n_acted > 0 THEN
        RAISE EXCEPTION
            '080-down REFUSING: % payment event(s) have been acted on or are '
            'awaiting a person. Dropping this table deletes the record of what '
            'a provider told us, while the money it describes has still '
            'changed hands.', n_acted
        USING HINT =
            'Export them first: SELECT * FROM payment_events. An event whose '
            'outcome is ''unattributed'' is an OPEN QUESTION — a payment '
            'arrived for a customer reference matching no tenant — and '
            'it needs answering rather than dropping.';
    END IF;
END $$;

DROP TRIGGER IF EXISTS payment_events_immutable_arrival ON payment_events;
DROP FUNCTION IF EXISTS payment_events_are_arrivals();

DROP TABLE IF EXISTS payment_events;

-- The column 080 added to a table it did not create.
--
-- This loses the mapping between a provider's customers and their tenants. That
-- is acceptable ONLY because the rollback has already refused if anything was
-- ever acted on: a deployment that has processed a payment cannot reach this
-- line.
-- The GUARD COMES OFF FIRST, and this order is not cosmetic.
--
-- The first version of this rollback dropped the column and left the trigger
-- behind. `tenants_provider_customer_guard` then sat on the TENANCY ROOT
-- referring to a column that no longer existed, and every subsequent INSERT or
-- UPDATE on `tenants` failed with:
--
--     record "new" has no field "provider_customer_ref"
--
-- A deployment that rolled 080 back would have found it could not create or
-- modify a tenant at all — the rollback for a billing migration breaking
-- sign-up. Caught by the CI rollback cycle, which re-applies 023 after peeling
-- and hit it immediately; NOT by reading the file, where a missing DROP looks
-- like nothing.
DROP TRIGGER IF EXISTS tenants_provider_customer_guard ON tenants;
DROP FUNCTION IF EXISTS tenants_provider_customer_is_operator_set();

DROP INDEX IF EXISTS tenants_provider_customer;
ALTER TABLE tenants DROP COLUMN IF EXISTS provider_customer_ref;

COMMIT;
