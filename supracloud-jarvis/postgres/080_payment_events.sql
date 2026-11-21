-- =============================================================================
-- 080_payment_events.sql — Phase 11, batch P11-04.
--
-- The intake ledger for inbound payment-provider webhooks.
--
-- ── Why this table is not tenant-scoped, stated plainly ────────────────────
--
-- Every other table carrying tenant data in this schema is TENANT_SCOPED under
-- FORCE RLS, and departing from that needs a reason rather than a preference.
--
-- The reason is the order events arrive in. A webhook is an unauthenticated
-- HTTP request from the internet. At the moment the bytes land, NOTHING has
-- established which tenant they concern — the provider's customer reference has
-- not been looked up yet, and it may not resolve at all. `app.tenant_id` cannot
-- be set to a value nobody knows, and under FORCE RLS an INSERT with the GUC
-- unset does not error: it is REFUSED BY THE POLICY. The intake path would then
-- silently drop exactly the events that most need investigating — the ones that
-- cannot be attributed.
--
-- So this table records ARRIVAL, and it is deliberately split from ACTING:
--
--   1. intake   — the owner connection writes the event down, verbatim and
--                 exactly once. No billing state changes here.
--   2. attribute— the customer reference is resolved to a tenant, or is not.
--   3. act      — anything that follows runs under a real tenant principal,
--                 through the tenant-scoped tables, under FORCE RLS as usual.
--
-- `tenant_id` is therefore NULLABLE and is a RESULT of step 2, never an input.
-- An event that resolves to no tenant is kept with `tenant_id IS NULL` — a
-- payment nobody can attribute is a thing a human must look at, and deleting it
-- to keep the table tidy destroys the only record that it happened.
--
-- ── Idempotency is a UNIQUE INDEX, not a code path ─────────────────────────
--
-- Providers retry. A webhook delivered twice that is PROCESSED twice is a
-- customer charged twice, or an invoice paid twice, and both are the kind of
-- defect a customer discovers before we do.
--
-- `UNIQUE (provider, provider_event_id)` makes the second delivery lose a race
-- it cannot win, in the database, under concurrency, without a lock the
-- application has to remember to take. A check-then-insert in Python is not the
-- same control: two deliveries arriving together both see "not present" and
-- both proceed.
--
-- ── Signature verification is NOT recorded as a boolean ────────────────────
--
-- There is no `signature_valid` column. A row exists here ONLY if the signature
-- verified — verification happens before the INSERT, and a request that fails
-- it is refused with nothing written.
--
-- A boolean column would invite the opposite design: store everything, filter
-- later. That turns an unauthenticated request from anyone on the internet into
-- a row in the billing schema, and makes "did we check?" a question about
-- whether every later reader remembered the filter.
-- =============================================================================

BEGIN;

-- Whether THIS run is the one that creates the table. The emptiness assertion
-- at the end means "080 seeded nothing", and on a table that already holds
-- runtime rows that is a different question — 078 shipped the same shape and
-- refuses to re-apply on any database where a plan legitimately exists.
CREATE TEMP TABLE _080_before ON COMMIT DROP AS
SELECT to_regclass('public.payment_events') IS NULL AS is_new;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'invoices') THEN
        RAISE EXCEPTION
            '080 REFUSING: invoices does not exist. 079 has not been applied, '
            'so there is nothing for a payment event to be about.';
    END IF;
END $$;

-- ── 1. The intake ledger ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS payment_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Which provider sent this. Bounded so the column cannot become somewhere
    -- arbitrary text is kept.
    provider        TEXT NOT NULL CHECK (provider ~ '^[a-z0-9_]{2,40}$'),
    -- The provider's OWN id for the event. The idempotency key, and the reason
    -- it is TEXT rather than UUID: it is their identifier, not ours, and
    -- coercing it to our type would be inventing a format they never promised.
    provider_event_id TEXT NOT NULL CHECK (length(provider_event_id) BETWEEN 1 AND 200),
    event_type      TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 100),

    -- The provider's customer reference as it arrived. This is the ONLY link
    -- to a tenant, deliberately: `tenants.provider_customer_ref` is the single
    -- source of truth for that mapping, and a `tenant_id` column here would be
    -- a second copy of a fact that can then disagree with the first.
    --
    -- It also keeps this table honestly unscoped. A table carrying `tenant_id`
    -- holds one tenant's data and must be invisible without that tenant bound —
    -- `test_every_tenant_scoped_table_enables_and_forces_rls` and
    -- `test_unset_tenant_context_sees_nothing_anywhere` enforce exactly that,
    -- with no exemption list, and they refused the first draft of this file.
    -- The rule is right. An arrival is not any tenant's data until attribution
    -- says so, and attribution is a lookup, not a column.
    customer_ref    TEXT NOT NULL DEFAULT '' CHECK (length(customer_ref) <= 200),

    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,

    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at    TIMESTAMPTZ,
    -- 'received'    — written down, not yet acted on
    -- 'applied'     — acted on
    -- 'unattributed'— no tenant matched; a person must look
    -- 'ignored'     — a type this deployment does not act on
    outcome         TEXT NOT NULL DEFAULT 'received'
                        CHECK (outcome IN ('received','applied','unattributed','ignored')),
    -- Why, in words, when the outcome is not 'applied'. Absent metadata is not
    -- absent terms: an unexplained 'ignored' is indistinguishable from a bug.
    note            TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 500),

    CONSTRAINT payment_event_processed_agree
        CHECK ((outcome <> 'received') = (processed_at IS NOT NULL)),
    -- THE idempotency control. Enforced by the database under concurrency,
    -- because a check-then-insert in the application lets two simultaneous
    -- deliveries both observe "not present" and both proceed.
    CONSTRAINT payment_event_delivered_once
        UNIQUE (provider, provider_event_id)
);

CREATE INDEX IF NOT EXISTS payment_events_customer
    ON payment_events (customer_ref, received_at DESC);
CREATE INDEX IF NOT EXISTS payment_events_unprocessed
    ON payment_events (received_at) WHERE outcome = 'received';

-- ── 1b. How a provider's customer maps to a tenant ─────────────────────────
--
-- Attribution must answer "which tenant owns provider customer X" while NOTHING
-- is bound. That question cannot be answered from a tenant-scoped table:
-- `tenant_subscriptions` is FORCE RLS, and an unbound read of it does not error
-- — IT MATCHES NOTHING — so every payment would come back unattributable,
-- silently.
--
-- Earlier drafts of this migration tried two wrong answers. A SECURITY DEFINER
-- lookup: refused by `test_there_are_no_security_definer_functions`, and it
-- would not have worked anyway, because SECURITY DEFINER changes WHO the query
-- runs as while FORCE RLS applies to the owner too. Then a separate unscoped
-- directory table with a `USING (true)` read policy: refused by the cross-tenant
-- sweep, because it carried `tenant_id` and returned rows with no tenant bound.
--
-- The repository already answers this. The tenancy ROOTS are readable before
-- scope exists, and they are readable precisely because they do not carry a
-- `tenant_id` column — `tenants` is what you read to ESTABLISH scope.
--
-- So the mapping goes on `tenants`. A provider's customer identifies a paying
-- ORGANISATION, which is a property of the tenant rather than of whichever
-- subscription happens to be current — a tenant that cancels and resubscribes
-- is the same customer to the provider. One column, on the table that already
-- has the right visibility, with no second copy to drift and no trigger to keep
-- two copies agreeing.

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS provider_customer_ref TEXT NOT NULL DEFAULT ''
        CHECK (length(provider_customer_ref) <= 200);

-- One provider customer belongs to ONE tenant. Two would make attribution
-- ambiguous, and an ambiguity resolved arbitrarily credits a payment to the
-- wrong company. Partial, so the many tenants with no provider reference do not
-- all collide on ''.
CREATE UNIQUE INDEX IF NOT EXISTS tenants_provider_customer
    ON tenants (provider_customer_ref)
    WHERE provider_customer_ref <> '';

-- ── 1c. The mapping is not writable by the runtime ─────────────────────────
--
-- `tenants` is the tenancy ROOT: no RLS, no policies, and `ira_app` holds
-- table-level INSERT and UPDATE. Measured, not assumed:
--
--     relrowsecurity = f, relforcerowsecurity = f, policies = 0
--     has_table_privilege('ira_app', 'tenants', 'UPDATE') = t
--
-- That is CORRECT for the root — a policy keyed on the tenant would make the
-- table unreadable, and an unreadable root cannot establish scope — but it
-- means a bare column here would be writable by the runtime FOR ANY TENANT. A
-- first version of this migration shipped exactly that, and a test written for
-- it caught it: the runtime could point any provider customer at any tenant and
-- have the next payment credited to whoever asked. Worse than the directory
-- table it replaced, which at least confined writes by policy.
--
-- ── Why a trigger and not column privileges ────────────────────────────────
--
-- Column-level privileges are the native tool and were the obvious candidate:
-- REVOKE UPDATE ON tenants, then re-GRANT UPDATE (col, ...) for every column
-- except this one. Two things rule it out, and the second is decisive.
--
--   * The list must be enumerated here and kept correct forever. A column added
--     later is simply not granted, which fails safe — but the list is also a
--     standing invitation to "fix" a broken write by appending to it.
--   * 042 GRANTs SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public.
--     A table-level UPDATE grant SUPERSEDES column-level ones, so the next time
--     042 is applied -- which the CI rollback cycle does on every run -- the
--     protection would be silently gone. This is the same 042-shaped hole
--     P10-04 found the hard way, and it is why 076, 077, 078 and 079 must all
--     be re-applied AFTER 042.
--
-- A trigger has neither problem. A GRANT cannot remove a trigger, so 042 cannot
-- undo it and the recompose cycle cannot restore unsafe authority. It needs no
-- SECURITY DEFINER because it only ever REFUSES: it grants nothing, so there is
-- nothing for it to do with somebody else's authority. And it is keyed on THIS
-- column rather than on a list of all the others, so a column added later is
-- unaffected rather than accidentally privileged.
--
-- ── Why an allow-list of roles, not a deny-list ────────────────────────────
--
-- A deny-list naming `ira_app` and `ira_worker` fails OPEN: the day a third
-- runtime role is added it is not on the list, and it may write the mapping.
-- The allow-list fails CLOSED. A new role is refused until somebody adds it
-- here deliberately, which is a decision made in a migration under review
-- rather than an omission nobody notices.

CREATE OR REPLACE FUNCTION tenants_provider_customer_is_operator_set()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.provider_customer_ref <> ''
           AND current_user NOT IN ('ira_migrator', 'ira_schema_owner', 'postgres') THEN
            RAISE EXCEPTION
                'the runtime may not create a tenant already carrying a payment '
                'provider customer reference (%). Creating a tenant and deciding '
                'who pays for it are different decisions.',
                NEW.provider_customer_ref
            USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.provider_customer_ref IS DISTINCT FROM OLD.provider_customer_ref
       AND current_user NOT IN ('ira_migrator', 'ira_schema_owner', 'postgres') THEN
        RAISE EXCEPTION
            'the runtime may not change which payment provider customer maps to '
            'a tenant (% -> %). `tenants` carries no row-level security, so a '
            'writable mapping here could be pointed at ANY tenant, and the next '
            'payment for that customer would be credited to whoever asked.',
            OLD.provider_customer_ref, NEW.provider_customer_ref
        USING ERRCODE = 'insufficient_privilege',
              HINT = 'Establishing the mapping is a privileged action. The '
                     'runtime READS it to attribute an arriving payment, and '
                     'never writes it.';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
-- Not SECURITY DEFINER: it only refuses. `search_path` is pinned all the same --
-- 042 pins what exists when it runs and this is later -- with `pg_temp` last,
-- because a temporary schema is writable by any session.
SET search_path = public, pg_temp;

DROP TRIGGER IF EXISTS tenants_provider_customer_guard ON tenants;
CREATE TRIGGER tenants_provider_customer_guard
    BEFORE INSERT OR UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION tenants_provider_customer_is_operator_set();

-- ── 2. An arrival cannot be un-recorded, or re-pointed ─────────────────────
--
-- The row may be UPDATED to record what was decided about it — that is the
-- whole of step 2 and step 3. It may NOT be deleted, and the identity of the
-- event may not change: a delivery that could be re-labelled as a different
-- provider's different event is not an audit trail of anything.

CREATE OR REPLACE FUNCTION payment_events_are_arrivals()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'payment event % cannot be deleted. It is the record that a '
            'provider told us something; if it was acted on wrongly, correct '
            'the billing record and say so in `note`.', OLD.id;
    END IF;

    IF NEW.provider <> OLD.provider
       OR NEW.provider_event_id <> OLD.provider_event_id
       OR NEW.event_type <> OLD.event_type
       OR NEW.payload IS DISTINCT FROM OLD.payload
       OR NEW.customer_ref <> OLD.customer_ref
       OR NEW.received_at <> OLD.received_at THEN
        RAISE EXCEPTION
            'payment event % may record what was DECIDED about it, but what '
            'ARRIVED is fixed. Changing the provider, the event id, the type, '
            'the payload or the arrival time rewrites history.', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
-- Pinned for the same reason 079's triggers are: 042 pins what exists when it
-- runs, and this is later. `pg_temp` last — a temporary schema is writable by
-- any session.
SET search_path = public, pg_temp;

DROP TRIGGER IF EXISTS payment_events_immutable_arrival ON payment_events;
CREATE TRIGGER payment_events_immutable_arrival
    BEFORE UPDATE OR DELETE ON payment_events
    FOR EACH ROW EXECUTE FUNCTION payment_events_are_arrivals();

-- ── 3. Privileges ──────────────────────────────────────────────────────────
--
-- `ira_app` may INSERT (intake) and UPDATE (record the decision). It may not
-- DELETE, and the trigger refuses anyway — a privilege says who may ask, a
-- trigger says the answer is no, and neither should have to hold alone.
--
-- Written out longhand because 042's `GRANT … ON ALL TABLES` would otherwise
-- hand over all four, which is the 042-shaped hole P10-04 found the hard way.

GRANT SELECT, INSERT, UPDATE ON payment_events TO ira_app, ira_worker;
REVOKE DELETE, TRUNCATE ON payment_events FROM ira_app, ira_worker;

-- ── 4. Prove the shape ─────────────────────────────────────────────────────

DO $$
BEGIN
    IF has_table_privilege('ira_app', 'payment_events', 'DELETE') THEN
        RAISE EXCEPTION
            '080 FAILED: ira_app can DELETE a payment event. The record that a '
            'provider told us something must not be removable by the code that '
            'acts on it.';
    END IF;
    IF (SELECT is_new FROM _080_before) AND (SELECT count(*) FROM payment_events) <> 0 THEN
        RAISE EXCEPTION
            '080 FAILED: payment events were seeded. Nothing has arrived — the '
            'table exists to record what a provider actually sent.';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'payment_event_delivered_once' AND contype = 'u'
    ) THEN
        RAISE EXCEPTION
            '080 FAILED: the (provider, provider_event_id) uniqueness is not a '
            'constraint. Idempotency enforced in application code lets two '
            'simultaneous deliveries both observe "not present".';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname = 'tenants_provider_customer_guard' AND NOT tgisinternal
    ) THEN
        RAISE EXCEPTION
            '080 FAILED: the tenants provider-customer guard is missing. The '
            'mapping would then be writable by the runtime for ANY tenant, '
            'because tenants carries no row-level security.';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public' AND p.prosecdef
                  AND p.proname = 'tenants_provider_customer_is_operator_set') THEN
        RAISE EXCEPTION
            '080 FAILED: the guard is SECURITY DEFINER. It only refuses, so it '
            'has no reason to run as anybody else.';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'tenants' AND column_name = 'provider_customer_ref'
    ) THEN
        RAISE EXCEPTION
            '080 FAILED: tenants has no provider_customer_ref, so an arriving '
            'payment cannot be attributed to a tenant by reading a fact that is '
            'visible before any tenant is bound.';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public' AND p.prosecdef
                  AND p.proname LIKE 'payment%') THEN
        RAISE EXCEPTION
            '080 FAILED: a payment function is SECURITY DEFINER. It would run '
            'as its owner — a standing route for the runtime to act as '
            'ira_schema_owner that no policy on the caller can take back.';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'payment_events' AND column_name = 'tenant_id') THEN
        RAISE EXCEPTION
            '080 FAILED: payment_events carries tenant_id. A table with that '
            'column holds one tenant''s data and must be invisible without that '
            'tenant bound — which intake cannot be, because it happens before '
            'any tenant is known. The mapping lives on tenants instead.';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public'
           AND p.proname = 'payment_events_are_arrivals'
           AND (p.proconfig IS NULL OR NOT EXISTS (
                SELECT 1 FROM unnest(p.proconfig) c WHERE c LIKE 'search_path=%'))
    ) THEN
        RAISE EXCEPTION
            '080 FAILED: a payment function has an unpinned search_path. For '
            'the SECURITY DEFINER lookup that is not hygiene: it would run a '
            'caller-chosen table AS ITS OWNER.';
    END IF;
END $$;

COMMIT;
