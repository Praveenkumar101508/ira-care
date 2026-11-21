-- =============================================================================
-- 078_billing_plans_and_entitlements.sql — Phase 11, batch P11-01.
--
-- The commercial layer ABOVE metering: what a tenant is subscribed to, what
-- that entitles them to, and what it costs — where "what it costs" is a fact
-- the owner sets and this migration deliberately does not.
--
-- ── Why a price is a ROW and not a COLUMN ──────────────────────────────────
--
-- `docs/cloud/COST_AND_CAPACITY_REGISTER.md` names "Setting subscription
-- prices" as a Phase 11 action that stops for the owner, and states plainly:
--
--     Subscription prices — prices are not to be finalised (brief §20).
--
-- So this build must be able to represent A PLAN THAT HAS NO PRICE, and must be
-- unable to confuse that with a plan that costs nothing.
--
-- A nullable `price` column on `billing_plans` would do it badly: every read
-- would have to remember the NULL case, and the first `COALESCE(price, 0)`
-- anybody writes turns "nobody has decided" into "free" permanently and
-- silently. That is failure class 12 — a swallowed absence returning a
-- plausible benign result — applied to money, which is the worst place for it.
--
-- So a price is a separate ROW in `billing_plan_prices`. No row means no price.
-- There is nothing to COALESCE, the join simply finds nothing, and code that
-- forgot the case gets no number rather than a wrong one.
--
-- The table is also APPEND-ONLY history: a price has an `effective_from`, and
-- changing a price adds a row rather than editing one. An invoice must remain
-- explicable a year later, and it cannot be if the price it was computed from
-- has been overwritten. Same reasoning as `model_usage`'s denormalised
-- `locality` in 029: the record must keep saying what was true when it was made.
--
-- ── What this migration deliberately does NOT do ───────────────────────────
--
--   * It sets no price. `billing_plan_prices` is created EMPTY and an apply-time
--     assertion proves it.
--   * It seeds no plan. Which plans exist is a product decision, and a seeded
--     "free"/"pro" pair would be this migration inventing the commercial model.
--   * It creates no payment provider, key, contract or charge.
--
-- ── Who may write what ─────────────────────────────────────────────────────
--
-- The runtime reads all five tables on the request path and must not be able to
-- edit any of the ones that decide what a tenant is owed or owes — the same rule
-- 068, 076 and 077 apply, for the same reason: the process that answers requests
-- must not be able to edit the record it answers from.
--
--   billing_plans                 ira_app: SELECT only
--   billing_plan_prices           ira_app: SELECT only   ← a runtime that could
--                                                          write here could set
--                                                          its own customer's price
--   plan_entitlements             ira_app: SELECT only   ← or raise its own limits
--   tenant_entitlement_overrides  ira_app: SELECT only   ← likewise
--   tenant_subscriptions          ira_app: SELECT, INSERT, UPDATE (no DELETE)
--
-- `tenant_subscriptions` is the one the runtime legitimately writes — subscribing
-- and cancelling are things a tenant does. DELETE is withheld: a cancelled
-- subscription is `status='canceled'` with a date, not an absence. "This tenant
-- was never subscribed" and "this tenant cancelled" must not look identical.
--
-- ── How this relates to the quotas that already exist ──────────────────────
--
-- `usage_quotas` (029) and the atomic counter (032) are IMPLEMENTED_AND_VERIFIED
-- and are NOT replaced. Entitlements layer on top:
--
--     no subscription   → no plan → no entitlement rows → the deployment's
--                         existing quota behaviour is unchanged
--     a subscription    → the plan's entitlements apply, unless a tenant
--                         override says otherwise
--
-- So a deployment that never sells anything behaves exactly as it does today.
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'tenants') THEN
        RAISE EXCEPTION
            '078 REFUSING: tenants does not exist. The tenancy substrate has '
            'not been applied, and a billing table keyed on a tenant that has '
            'no table to reference would be created unreferenced.';
    END IF;
END $$;

-- ── 1. The plan catalogue — deployment-wide, like tool_policies ────────────

CREATE TABLE IF NOT EXISTS billing_plans (
    plan_id       TEXT PRIMARY KEY CHECK (plan_id ~ '^[a-z0-9_]{2,40}$'),
    display_name  TEXT        NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
    description   TEXT        NOT NULL DEFAULT '',
    -- A plan can be retired without being deleted: existing subscriptions must
    -- keep resolving. Deleting a plan somebody is on would make their
    -- subscription unexplainable.
    active        BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE billing_plans IS
    'The deployment''s plan catalogue. Deployment-wide reference data, not '
    'tenant-scoped: every tenant sees the same plans. Carries NO price — see '
    'billing_plan_prices, where absence of a row means no price has been set.';

-- ── 2. Prices — owner-set, append-only, absent by default ──────────────────

CREATE TABLE IF NOT EXISTS billing_plan_prices (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    plan_id         TEXT NOT NULL REFERENCES billing_plans(plan_id),
    currency        TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    -- Minor units (pence/cents). Never a float: a price that cannot be
    -- represented exactly is a price that will not reconcile.
    unit_amount_minor BIGINT NOT NULL CHECK (unit_amount_minor >= 0),
    recurrence      TEXT NOT NULL CHECK (recurrence IN ('monthly', 'yearly')),
    effective_from  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Who decided this. A price is an owner action and the record says so.
    set_by          TEXT NOT NULL CHECK (length(set_by) BETWEEN 1 AND 120),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- One price per plan per currency per recurrence per instant. Changing a
    -- price adds a row with a later `effective_from`.
    CONSTRAINT plan_price_unique_at_instant
        UNIQUE (plan_id, currency, recurrence, effective_from)
);

CREATE INDEX IF NOT EXISTS billing_plan_prices_lookup
    ON billing_plan_prices (plan_id, currency, recurrence, effective_from DESC);

COMMENT ON TABLE billing_plan_prices IS
    'Owner-set prices, append-only. NO ROW MEANS NO PRICE — billing against a '
    'plan with no price row must refuse, never default or zero. Changing a '
    'price inserts a row with a later effective_from; the old row stays so an '
    'invoice issued under it remains explicable.';

-- ── 3. What a plan entitles you to ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS plan_entitlements (
    plan_id     TEXT NOT NULL REFERENCES billing_plans(plan_id) ON DELETE CASCADE,
    -- A stable machine key the runtime asks for, e.g. 'requests_per_day'.
    key         TEXT NOT NULL CHECK (key ~ '^[a-z0-9_.]{2,60}$'),
    -- NULL means UNLIMITED, and is distinct from 0, which means NONE. Both are
    -- legitimate answers and collapsing them is how an unlimited plan becomes a
    -- blocked one, or the reverse.
    limit_value BIGINT CHECK (limit_value IS NULL OR limit_value >= 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (plan_id, key)
);

COMMENT ON COLUMN plan_entitlements.limit_value IS
    'NULL = unlimited; 0 = none. Deliberately different values: collapsing them '
    'turns an unlimited plan into a blocked one or the reverse.';

-- ── 4. Who is subscribed to what — TENANT SCOPED ───────────────────────────

CREATE TABLE IF NOT EXISTS tenant_subscriptions (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    plan_id       TEXT NOT NULL REFERENCES billing_plans(plan_id),
    status        TEXT NOT NULL DEFAULT 'trialing'
                      CHECK (status IN ('trialing', 'active', 'past_due',
                                        'canceled')),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    current_period_start TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    current_period_end   TIMESTAMPTZ NOT NULL,
    canceled_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- A cancelled subscription carries its date, and a live one does not. Tied
    -- as a shape rather than left to the writer, the same way 035 ties
    -- status='removed' to revoked_at.
    CONSTRAINT subscription_canceled_agree
        CHECK ((status = 'canceled') = (canceled_at IS NOT NULL)),
    CONSTRAINT subscription_period_ordered
        CHECK (current_period_end > current_period_start)
);

-- One LIVE subscription per tenant. Cancelled ones accumulate as history, so
-- this is a partial index rather than a plain unique constraint.
CREATE UNIQUE INDEX IF NOT EXISTS tenant_subscriptions_one_live
    ON tenant_subscriptions (tenant_id)
    WHERE status <> 'canceled';

CREATE INDEX IF NOT EXISTS tenant_subscriptions_tenant
    ON tenant_subscriptions (tenant_id, status);

-- ── 5. Per-tenant overrides — TENANT SCOPED, and not self-served ───────────

CREATE TABLE IF NOT EXISTS tenant_entitlement_overrides (
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    key         TEXT NOT NULL CHECK (key ~ '^[a-z0-9_.]{2,60}$'),
    limit_value BIGINT CHECK (limit_value IS NULL OR limit_value >= 0),
    reason      TEXT NOT NULL DEFAULT '',
    granted_by  TEXT NOT NULL CHECK (length(granted_by) BETWEEN 1 AND 120),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (tenant_id, key)
);

COMMENT ON TABLE tenant_entitlement_overrides IS
    'Operator-granted exceptions to a plan''s entitlements. Tenant-scoped and '
    'RLS-enforced so one tenant cannot read another''s, and NOT writable by '
    'ira_app: a runtime that could insert here could raise its own caller''s '
    'limits, which is the whole control.';

-- ── 6. Row-level security on the two tenant-scoped tables ──────────────────

ALTER TABLE tenant_subscriptions          ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_subscriptions          FORCE  ROW LEVEL SECURITY;
ALTER TABLE tenant_entitlement_overrides  ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_entitlement_overrides  FORCE  ROW LEVEL SECURITY;

-- NULLIF, not a bare cast. `current_setting('app.tenant_id', true)` returns the
-- EMPTY STRING when the GUC is set-but-cleared, and `''::uuid` RAISES
-- "invalid input syntax for type uuid" rather than matching nothing.
--
-- That difference is the whole point. A raise is an exception application code
-- can catch, and a caller that treats it as a transient database fault has
-- turned "this request has no tenant" into "try again in a moment" — the
-- swallowed-failure-returns-a-plausible-answer class, on the boundary that
-- separates customers. NULLIF makes the predicate NULL, NULL is not TRUE, and a
-- predicate that is not TRUE denies. `023` established this and
-- `test_cross_tenant_sweep` is what caught 078 not following it.
DROP POLICY IF EXISTS tenant_isolation ON tenant_subscriptions;
CREATE POLICY tenant_isolation ON tenant_subscriptions
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS tenant_isolation ON tenant_entitlement_overrides;
CREATE POLICY tenant_isolation ON tenant_entitlement_overrides
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- ── 7. Grants ──────────────────────────────────────────────────────────────
--
-- 042's default privileges hand DML on every new table to ira_app and
-- ira_worker, so each REVOKE below is taking something away that would
-- otherwise be there. That is why they are written out rather than assumed —
-- and why the cycle in test.yml re-applies this migration after 042, the
-- lesson P9/P10-04 paid for.

GRANT SELECT ON billing_plans, billing_plan_prices, plan_entitlements,
                tenant_subscriptions, tenant_entitlement_overrides
    TO ira_app, ira_worker;

GRANT INSERT, UPDATE ON tenant_subscriptions TO ira_app, ira_worker;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON billing_plans       FROM ira_app, ira_worker;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON billing_plan_prices FROM ira_app, ira_worker;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON plan_entitlements   FROM ira_app, ira_worker;
REVOKE DELETE, TRUNCATE          ON tenant_subscriptions       FROM ira_app, ira_worker;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON tenant_entitlement_overrides FROM ira_app, ira_worker;

-- ── 8. Prove the shape, at apply time ──────────────────────────────────────

DO $$
DECLARE
    n_prices BIGINT;
    n_plans  BIGINT;
BEGIN
    -- The owner boundary, asserted rather than described. If a later edit ever
    -- seeds a price here, this fails and names why.
    SELECT count(*) INTO n_prices FROM billing_plan_prices;
    IF n_prices <> 0 THEN
        RAISE EXCEPTION
            '078 FAILED: % price row(s) were seeded. Setting a subscription '
            'price is an OWNER action (COST_AND_CAPACITY_REGISTER.md, phase '
            '11) and this migration must create the table empty.', n_prices;
    END IF;

    SELECT count(*) INTO n_plans FROM billing_plans;
    IF n_plans <> 0 THEN
        RAISE EXCEPTION
            '078 FAILED: % plan row(s) were seeded. Which plans exist is a '
            'product decision, not this migration''s.', n_plans;
    END IF;

    IF has_table_privilege('ira_app', 'billing_plan_prices', 'UPDATE')
       OR has_table_privilege('ira_app', 'billing_plan_prices', 'INSERT') THEN
        RAISE EXCEPTION
            '078 FAILED: ira_app can write billing_plan_prices. The process '
            'that answers requests must not be able to set its own customer''s '
            'price.';
    END IF;

    IF has_table_privilege('ira_app', 'tenant_entitlement_overrides', 'INSERT') THEN
        RAISE EXCEPTION
            '078 FAILED: ira_app can insert tenant_entitlement_overrides, so '
            'the runtime could raise its own caller''s limits.';
    END IF;

    IF has_table_privilege('ira_app', 'tenant_subscriptions', 'DELETE') THEN
        RAISE EXCEPTION
            '078 FAILED: ira_app can DELETE tenant_subscriptions. A cancelled '
            'subscription must remain visible as cancelled; "never subscribed" '
            'and "cancelled" must not look identical.';
    END IF;

    IF NOT (SELECT relforcerowsecurity FROM pg_class
             WHERE relname = 'tenant_subscriptions') THEN
        RAISE EXCEPTION '078 FAILED: FORCE RLS is not set on tenant_subscriptions.';
    END IF;
    IF NOT (SELECT relforcerowsecurity FROM pg_class
             WHERE relname = 'tenant_entitlement_overrides') THEN
        RAISE EXCEPTION
            '078 FAILED: FORCE RLS is not set on tenant_entitlement_overrides.';
    END IF;
END $$;

COMMIT;
