-- =============================================================================
-- 086_ira_pro_plan_and_price.sql — the owner's commercial decision, recorded
--
-- IRA Pro. £15.00 GBP, 1500 minor units, monthly.
--
-- ── Why this is a migration and not a constant in Python ────────────────────
--
-- `078_billing_plans_and_entitlements.sql` already decided where a price lives,
-- and said so in its own comment:
--
--     'Owner-set prices, append-only. NO ROW MEANS NO PRICE — billing against
--      a plan with no price row must refuse, never default or zero.'
--
-- So the authoritative price is a ROW, and there has never been one. Every
-- billing path in this repository has therefore answered `PRICE_NOT_CONFIGURED`
-- since Phase 11 — correctly, because no price had been set.
--
-- A price in application source would be a second authority. The runtime reads
-- the row; the API surfaces what the runtime read; the frontend renders what the
-- API returned. **£15 appears once, here.** A literal `1500` anywhere in
-- application code would be a copy that can disagree with the charge, and a
-- price that disagrees with the charge is the worst defect this system can have.
--
-- ── Append-only, and what that means for a future price change ──────────────
--
-- Changing the price later INSERTS a row with a later `effective_from`. This
-- row stays, so an invoice issued under it remains explicable years afterwards.
-- Nothing here updates or deletes a price.
--
-- ── Idempotency ─────────────────────────────────────────────────────────────
--
-- `plan_price_unique_at_instant` is UNIQUE on
-- (plan_id, currency, recurrence, effective_from). A fixed `effective_from` —
-- rather than NOW() — is what makes re-applying this file insert nothing the
-- second time instead of adding a duplicate priced at the same amount. A
-- migration that quietly doubles its own rows on a re-run is not reversible in
-- any useful sense.
--
-- ── Entitlements ────────────────────────────────────────────────────────────
--
-- The plan's entitlement keys are the ones the runtime already asks for. They
-- are read by `core/billing/entitlements.py::effective_limits` and enforced at
-- `IRAModelGateway._admit()`, which is the real chokepoint — not a status
-- column somebody remembers to check.
--
-- NULL means UNLIMITED and 0 means NONE; they are different values on purpose,
-- and `078` refuses to let them be collapsed.
-- =============================================================================

BEGIN;

-- ── The product ────────────────────────────────────────────────────────────

INSERT INTO billing_plans (plan_id, display_name, description, active)
VALUES (
    'ira_pro',
    'IRA Pro',
    'The paid IRA plan: hosted cloud workspace, higher model quotas, and '
    'connector access.',
    TRUE
)
ON CONFLICT (plan_id) DO NOTHING;

-- ── The price. One row. The only one in this repository ────────────────────
--
-- `effective_from` is pinned to the date of the owner's decision rather than
-- NOW(): a fixed value is what makes the UNIQUE constraint able to recognise a
-- second application of this file.

INSERT INTO billing_plan_prices (
    plan_id, currency, unit_amount_minor, recurrence, effective_from, set_by
)
VALUES (
    'ira_pro',
    'GBP',
    1500,                       -- £15.00, in pence. Never a float.
    'monthly',
    TIMESTAMPTZ '2026-08-15 00:00:00+00',
    'owner decision, recorded in docs/cloud/OPEN_DECISIONS.md'
)
ON CONFLICT (plan_id, currency, recurrence, effective_from) DO NOTHING;

-- ── What the plan entitles you to ──────────────────────────────────────────
--
-- Only keys the runtime actually asks for. An entitlement nothing reads is a
-- promise nothing keeps, which is the defect class this programme keeps finding.

INSERT INTO plan_entitlements (plan_id, key, limit_value)
VALUES
    -- Model requests per day. The gateway's `_admit()` reads this one.
    ('ira_pro', 'model.requests_per_day', 5000),
    -- Workspaces a tenant may create.
    ('ira_pro', 'workspaces.max', 10),
    -- Connector accounts across the tenant.
    ('ira_pro', 'connectors.max', 25)
ON CONFLICT (plan_id, key) DO NOTHING;

COMMIT;
