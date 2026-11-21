-- =============================================================================
-- 079_invoices.sql — Phase 11, batch P11-03.
--
-- An invoice, derived from metered usage, and immutable once issued.
--
-- ── The unit decision P11-02 deferred, made here explicitly ────────────────
--
-- `core/billing/entitlements.py` deliberately did NOT map a cost entitlement,
-- and said why: `plan_entitlements.limit_value` is a bare BIGINT with no
-- currency beside it, and inventing a unit for money is how an invoice comes
-- out a hundred times too large.
--
-- This migration makes the decision rather than inheriting it:
--
--   * Every money column is `*_minor` — an INTEGER count of the currency's
--     minor unit (pence, cents). Never a float. A price that cannot be
--     represented exactly is a price that will not reconcile, and 0.1 + 0.2 is
--     not 0.3 in binary floating point.
--   * Every money column travels with an ISO `currency` on the same row. A
--     number without its currency is not an amount, and the join that would
--     supply it later is the join somebody forgets.
--   * `plan_entitlements.limit_value` stays a COUNT and is never money. That is
--     why no cost entitlement exists: entitlements limit how much you may DO,
--     prices say what it COSTS, and one table doing both is where the unit
--     confusion would start.
--
-- ── No price, no invoice ───────────────────────────────────────────────────
--
-- `078` created `billing_plan_prices` empty because setting a price is an owner
-- action (COST_AND_CAPACITY_REGISTER.md, phase 11). The consequence lands here:
-- issuing an invoice for a plan with no price row must REFUSE.
--
-- Enforced as a CHECK rather than left to the writer — `unit_amount_minor` is
-- NOT NULL on every line, so a line computed from a missing price cannot be
-- stored at all. The application refuses first, with a message naming the plan;
-- this is the backstop for the day somebody writes a second writer.
--
-- ── Immutable once issued ──────────────────────────────────────────────────
--
-- A draft invoice is a working document and may be recomputed. An ISSUED
-- invoice is evidence — the customer has been told what they owe — and a system
-- that can quietly change it afterwards cannot be reconciled against anything.
--
-- So the trigger refuses UPDATE and DELETE on an issued invoice, EXCEPT the one
-- transition an accounting system genuinely needs: `issued` → `void`, which
-- adds a reason and a timestamp and changes no amount. Voiding is how a wrong
-- invoice is corrected — you void it and issue another — because editing it
-- would destroy the record that the first one was ever sent.
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'tenant_subscriptions') THEN
        RAISE EXCEPTION
            '079 REFUSING: tenant_subscriptions does not exist. 078 has not '
            'been applied, so there is no subscription for an invoice to bill '
            'and no price table for it to be computed from.';
    END IF;
END $$;

-- ── 1. Invoices — TENANT SCOPED ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS invoices (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- The subscription this bills. NOT cascaded to NULL: an invoice that no
    -- longer says what it billed is not an invoice.
    subscription_id UUID NOT NULL REFERENCES tenant_subscriptions(id),

    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,

    currency        TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    -- The sum of the lines, in minor units. Stored rather than computed on read
    -- so the invoice keeps saying what it said when it was issued, even if a
    -- line is later understood differently.
    total_minor     BIGINT NOT NULL CHECK (total_minor >= 0),

    status          TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'issued', 'void')),
    issued_at       TIMESTAMPTZ,
    voided_at       TIMESTAMPTZ,
    void_reason     TEXT NOT NULL DEFAULT '',

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Status and its timestamps agree, as a shape rather than a convention —
    -- the same rule 035 applies to removed/revoked_at and 078 to
    -- canceled/canceled_at.
    CONSTRAINT invoice_issued_agree
        CHECK ((status IN ('issued', 'void')) = (issued_at IS NOT NULL)),
    CONSTRAINT invoice_void_agree
        CHECK ((status = 'void') = (voided_at IS NOT NULL)),
    CONSTRAINT invoice_void_has_reason
        CHECK (status <> 'void' OR length(void_reason) > 0),
    CONSTRAINT invoice_period_ordered
        CHECK (period_end > period_start)
);

-- One invoice per subscription per period, unless it was voided. A voided
-- invoice and its replacement legitimately share a period.
CREATE UNIQUE INDEX IF NOT EXISTS invoices_one_live_per_period
    ON invoices (subscription_id, period_start, period_end)
    WHERE status <> 'void';

CREATE INDEX IF NOT EXISTS invoices_tenant ON invoices (tenant_id, status);

-- ── 2. Invoice lines — where the derivation is recorded ────────────────────

CREATE TABLE IF NOT EXISTS invoice_lines (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    invoice_id      UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- What this line is for, in words a customer could read on a bill.
    description     TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 200),
    -- 'subscription' — the recurring plan fee.
    -- 'usage'        — metered consumption, derived from model_usage.
    kind            TEXT NOT NULL CHECK (kind IN ('subscription', 'usage')),

    quantity        BIGINT NOT NULL CHECK (quantity >= 0),
    -- NOT NULL, deliberately: a line computed from a plan with no price row
    -- cannot be stored. The application refuses first and names the plan; this
    -- is the backstop.
    unit_amount_minor BIGINT NOT NULL CHECK (unit_amount_minor >= 0),
    amount_minor      BIGINT NOT NULL CHECK (amount_minor >= 0),
    currency        TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),

    -- The price row this line was computed from, so the bill stays explicable
    -- after the price changes. `billing_plan_prices` is append-only for exactly
    -- this reason; without the pointer, "why was I charged that" is answerable
    -- only by guessing which price was current at the time.
    price_id        UUID REFERENCES billing_plan_prices(id),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Arithmetic that cannot silently disagree with itself.
    CONSTRAINT invoice_line_amount_is_the_product
        CHECK (amount_minor = quantity * unit_amount_minor)
);

CREATE INDEX IF NOT EXISTS invoice_lines_invoice ON invoice_lines (invoice_id);

-- ── 3. Immutable once issued ───────────────────────────────────────────────

CREATE OR REPLACE FUNCTION invoices_immutable_once_issued()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION
                'invoice % is % and cannot be deleted. An issued invoice is '
                'evidence that the customer was told what they owe; correct it '
                'by VOIDING it and issuing another.', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'draft' THEN
        RETURN NEW;                     -- a draft is a working document
    END IF;

    IF OLD.status = 'issued' AND NEW.status = 'void'
       AND NEW.total_minor = OLD.total_minor
       AND NEW.currency    = OLD.currency
       AND NEW.issued_at   = OLD.issued_at THEN
        RETURN NEW;                     -- the one transition accounting needs
    END IF;

    RAISE EXCEPTION
        'invoice % is % and its amounts are immutable. The only permitted '
        'change is issued -> void, which records a reason and alters no '
        'amount. Editing an issued invoice destroys the record of what was '
        'actually sent.', OLD.id, OLD.status;
END;
$$ LANGUAGE plpgsql
-- Pinned, like every other function in this schema. An unpinned search_path
-- lets anyone who can create objects earlier in the path shadow a table this
-- function reads — and this one decides whether an invoice may be changed.
-- 042 pins every function that exists WHEN IT RUNS; 079 is later, so it pins
-- its own. `pg_temp` last, because a temporary schema is writable by any
-- session.
SET search_path = public, pg_temp;

DROP TRIGGER IF EXISTS invoices_no_edit_once_issued ON invoices;
CREATE TRIGGER invoices_no_edit_once_issued
    BEFORE UPDATE OR DELETE ON invoices
    FOR EACH ROW EXECUTE FUNCTION invoices_immutable_once_issued();

CREATE OR REPLACE FUNCTION invoice_lines_follow_their_invoice()
RETURNS TRIGGER AS $$
DECLARE
    parent TEXT;
BEGIN
    SELECT status INTO parent FROM invoices
     WHERE id = COALESCE(NEW.invoice_id, OLD.invoice_id);
    IF parent IS NOT NULL AND parent <> 'draft' THEN
        RAISE EXCEPTION
            'the lines of a % invoice cannot be changed. The total would then '
            'disagree with the lines that justify it.', parent;
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql
SET search_path = public, pg_temp;

DROP TRIGGER IF EXISTS invoice_lines_no_edit_once_issued ON invoice_lines;
CREATE TRIGGER invoice_lines_no_edit_once_issued
    BEFORE INSERT OR UPDATE OR DELETE ON invoice_lines
    FOR EACH ROW EXECUTE FUNCTION invoice_lines_follow_their_invoice();

-- ── 4. Row-level security ──────────────────────────────────────────────────

ALTER TABLE invoices      ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices      FORCE  ROW LEVEL SECURITY;
ALTER TABLE invoice_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoice_lines FORCE  ROW LEVEL SECURITY;

-- NULLIF, not a bare cast: `''::uuid` RAISES rather than matching nothing, and
-- a caller catching that turns "no tenant" into "transient fault". 078 shipped
-- with the bare cast and `test_cross_tenant_sweep` caught it.
DROP POLICY IF EXISTS tenant_isolation ON invoices;
CREATE POLICY tenant_isolation ON invoices
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DROP POLICY IF EXISTS tenant_isolation ON invoice_lines;
CREATE POLICY tenant_isolation ON invoice_lines
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- ── 5. Grants ──────────────────────────────────────────────────────────────
--
-- The runtime generates drafts and issues them, so it needs INSERT and UPDATE.
-- It does NOT get DELETE: the trigger already refuses to delete anything that
-- is not a draft, and withholding the privilege as well means a draft cannot be
-- silently discarded either. 042's default privileges would otherwise grant all
-- four, which is why these are written out.

GRANT SELECT, INSERT, UPDATE ON invoices, invoice_lines TO ira_app, ira_worker;
REVOKE DELETE, TRUNCATE ON invoices      FROM ira_app, ira_worker;
REVOKE DELETE, TRUNCATE ON invoice_lines FROM ira_app, ira_worker;

-- ── 6. Prove the shape ─────────────────────────────────────────────────────

DO $$
BEGIN
    IF has_table_privilege('ira_app', 'invoices', 'DELETE') THEN
        RAISE EXCEPTION
            '079 FAILED: ira_app can DELETE an invoice. A bill that can '
            'disappear is not evidence of anything.';
    END IF;
    IF NOT (SELECT relforcerowsecurity FROM pg_class WHERE relname = 'invoices') THEN
        RAISE EXCEPTION '079 FAILED: FORCE RLS is not set on invoices.';
    END IF;
    IF NOT (SELECT relforcerowsecurity FROM pg_class WHERE relname = 'invoice_lines') THEN
        RAISE EXCEPTION '079 FAILED: FORCE RLS is not set on invoice_lines.';
    END IF;
    IF (SELECT count(*) FROM invoices) <> 0 THEN
        RAISE EXCEPTION '079 FAILED: invoices were seeded. Nothing has been billed.';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public'
           AND p.proname IN ('invoices_immutable_once_issued',
                             'invoice_lines_follow_their_invoice')
           AND (p.proconfig IS NULL OR NOT EXISTS (
                SELECT 1 FROM unnest(p.proconfig) c WHERE c LIKE 'search_path=%'))
    ) THEN
        RAISE EXCEPTION
            '079 FAILED: a trigger function has an unpinned search_path. These '
            'two decide whether an invoice may be changed, so a caller who can '
            'create objects earlier in the path could shadow what they read.';
    END IF;
END $$;

COMMIT;
