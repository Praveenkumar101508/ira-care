-- =============================================================================
-- 087_billing_provider_lifecycle.sql — what a payment provider needs to drive
--                                       a subscription, and nothing more
--
-- `080` gave payment events an idempotent front door: signature verified,
-- recorded exactly once, attributed to a tenant or kept as `unattributed`.
-- Nothing consumed the result. An event arrived, was written down, and the
-- tenant's subscription did not move.
--
-- This adds the three things the applier needs and stops there.
--
-- ── 1. The provider's own handle on the subscription ────────────────────────
--
-- A provider sends `customer.subscription.updated` naming ITS subscription id,
-- not ours. Without somewhere to put that, the only route from an event to a
-- row is "the tenant's one live subscription" — which is true today and is a
-- guess, not a key. A tenant with a cancelled subscription and a new one would
-- resolve to whichever the query happened to return.
--
-- UNIQUE, because two tenants sharing one provider subscription is not a state
-- this system should be able to represent. Partial, because a subscription
-- created before any provider existed legitimately has none.
--
-- ── 2. cancel_at_period_end ─────────────────────────────────────────────────
--
-- Cancellation has two meanings and they are not the same product behaviour:
--
--   * cancel NOW              — access stops, `status='canceled'`
--   * cancel AT PERIOD END    — paid until `current_period_end`, then stops
--
-- `078` can only express the first. Collapsing the second into it takes access
-- away from somebody who has paid for the rest of the month; leaving it out
-- entirely means a user who cancels sees no acknowledgement and cancels again.
--
-- The flag is deliberately NOT part of `status`: the subscription really is
-- still `active`, and every entitlement check must keep saying so until the
-- period ends. What changes is what happens next, and that is a different fact.
--
-- ── 3. Checkout sessions ────────────────────────────────────────────────────
--
-- A checkout session is a real object with a lifetime, created before any money
-- moves and resolved afterwards by a webhook. Without a record of it:
--
--   * a completed-checkout event cannot be tied back to the tenant that started
--     it, except through the provider's customer mapping — which for a NEW
--     customer does not exist yet, because the customer is created BY the
--     checkout;
--   * a user who abandons checkout leaves no trace, so nobody can tell "never
--     tried" from "tried and failed".
--
-- `tenant_id` is recorded at creation from the VERIFIED principal, never from
-- anything the client sent. That is what makes the later attribution safe.
-- =============================================================================

BEGIN;

-- ── 1 and 2: on the subscription ───────────────────────────────────────────

ALTER TABLE tenant_subscriptions
    ADD COLUMN IF NOT EXISTS provider_subscription_ref TEXT
        CHECK (provider_subscription_ref IS NULL
               OR length(provider_subscription_ref) BETWEEN 1 AND 200),
    ADD COLUMN IF NOT EXISTS cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN tenant_subscriptions.provider_subscription_ref IS
    'The PROVIDER''s id for this subscription. NULL for a subscription created '
    'without a provider. Unique where present: two tenants sharing one provider '
    'subscription is not a representable state.';

COMMENT ON COLUMN tenant_subscriptions.cancel_at_period_end IS
    'The user has asked to stop at the end of the paid period. The subscription '
    'is STILL active and every entitlement check must keep saying so until '
    'current_period_end. Deliberately not a status: what changed is what '
    'happens next, not what is true now.';

CREATE UNIQUE INDEX IF NOT EXISTS tenant_subscriptions_provider_ref
    ON tenant_subscriptions (provider_subscription_ref)
    WHERE provider_subscription_ref IS NOT NULL;

-- A cancelled subscription cannot also be "going to cancel". Tied as a shape
-- rather than left to the writer, the same way `078` ties canceled_at.
ALTER TABLE tenant_subscriptions
    DROP CONSTRAINT IF EXISTS subscription_cancel_flag_sane;
ALTER TABLE tenant_subscriptions
    ADD CONSTRAINT subscription_cancel_flag_sane
        CHECK (NOT (status = 'canceled' AND cancel_at_period_end));

-- ── 3: checkout sessions ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS billing_checkout_sessions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    plan_id         TEXT NOT NULL REFERENCES billing_plans(plan_id),
    -- The provider's session id. NOT NULL: a session we did not get an id for
    -- was not created, and recording it would be recording a thing that does
    -- not exist.
    provider        TEXT NOT NULL CHECK (provider ~ '^[a-z0-9_]{2,40}$'),
    provider_session_ref TEXT NOT NULL
        CHECK (length(provider_session_ref) BETWEEN 1 AND 200),
    -- What was quoted, captured at creation. If the price changes between the
    -- session opening and the payment completing, this is what the user was
    -- shown — and what any dispute is about.
    currency        TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    unit_amount_minor BIGINT NOT NULL CHECK (unit_amount_minor >= 0),
    recurrence      TEXT NOT NULL CHECK (recurrence IN ('monthly', 'yearly')),
    status          TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'completed', 'expired')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,

    CONSTRAINT checkout_completed_agree
        CHECK ((status = 'completed') = (completed_at IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS billing_checkout_provider_ref
    ON billing_checkout_sessions (provider, provider_session_ref);

CREATE INDEX IF NOT EXISTS billing_checkout_tenant
    ON billing_checkout_sessions (tenant_id, status);

COMMENT ON TABLE billing_checkout_sessions IS
    'Checkout attempts. tenant_id is recorded from the VERIFIED principal at '
    'creation, never from the request body — it is what lets a completed '
    'checkout be attributed to the tenant that started it, including for a NEW '
    'customer the provider has not seen before.';

-- FORCE RLS, like every other tenant-scoped table here. FORCE rather than
-- ENABLE because the schema owner is otherwise exempt, and `042`'s blanket
-- GRANT means the runtime role is not the only one that reaches this.
ALTER TABLE billing_checkout_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_checkout_sessions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS billing_checkout_sessions_select ON billing_checkout_sessions;
CREATE POLICY billing_checkout_sessions_select ON billing_checkout_sessions
    FOR SELECT
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS billing_checkout_sessions_insert ON billing_checkout_sessions;
CREATE POLICY billing_checkout_sessions_insert ON billing_checkout_sessions
    FOR INSERT
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS billing_checkout_sessions_update ON billing_checkout_sessions;
CREATE POLICY billing_checkout_sessions_update ON billing_checkout_sessions
    FOR UPDATE
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- No DELETE policy, deliberately. A checkout attempt is evidence that somebody
-- tried to pay; removing it is not a tenant's operation.

-- `042`'s default privileges already hand DML on every new table to `ira_app`
-- and `ira_worker`, so the REVOKE below takes something away that would
-- otherwise be there — the same reasoning `078` §7 writes out rather than
-- assumes. A checkout attempt is evidence somebody tried to pay; deleting it is
-- not a runtime operation, and TRUNCATE never is.
GRANT SELECT, INSERT, UPDATE ON billing_checkout_sessions TO ira_app, ira_worker;
REVOKE DELETE, TRUNCATE ON billing_checkout_sessions FROM ira_app, ira_worker;

COMMIT;
