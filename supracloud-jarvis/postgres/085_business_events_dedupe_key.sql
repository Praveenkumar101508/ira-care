-- =============================================================================
-- 085_business_events_dedupe_key.sql — Phase 14 P14-R3, FINDING AQ
--
-- One nullable column and one partial unique index, so that a webhook a
-- provider delivers twice becomes one record instead of two.
--
-- ── What was wrong ──────────────────────────────────────────────────────────
--
-- `POST /webhooks/booking` and `POST /webhooks/lead` each minted a FRESH
-- `uuid4()` per request and inserted it as the row's identity. Nothing about a
-- request was ever compared with anything already stored.
--
-- Webhook delivery is at-least-once. That is not a corner case, it is the
-- contract every provider publishes: a timeout, a 5xx, a connection reset or a
-- slow response and the same event is delivered again. Cal.com does it.
--
-- So the same booking arrived twice and became two rows with two different ids,
-- and the owner was alerted twice for one client. Driven against a real
-- database before the fix, with the same `booking_id` posted twice:
--
--     rows in business_events for booking calcom-abc-123: 2
--     owner notifications sent: 2
--
-- The sting is that the identity was already in the payload. `BookingPayload`
-- carries `booking_id` — the PROVIDER's own stable id for the booking — and it
-- was stored inside the JSON blob and never used as identity. The information
-- needed to deduplicate was present at every duplicate insert and ignored.
--
-- ── Why a separate column and not a natural primary key ─────────────────────
--
-- `id` could not simply become the provider's id: it is a UUID, provider ids
-- are opaque strings, and rows already exist. More importantly the two are
-- different things — `id` is this system's handle for the record, and the
-- dedupe key is a statement about which EXTERNAL event the record came from.
-- Collapsing them would mean a row could not exist without a provider id,
-- which is exactly the case that must keep working (see below).
--
-- ── Why the index is PARTIAL, and why that is not a loophole ────────────────
--
-- The index covers only rows where `dedupe_key IS NOT NULL`, because not every
-- source has a stable id. A website lead form posts a name and a message and
-- nothing that identifies the SUBMISSION, and inventing one from the content
-- would be worse than not having it: two genuine enquiries from the same person
-- about the same thing are two leads, and a content hash would silently discard
-- the second.
--
-- The honest resolution is that such a source is NOT deduplicated, and the API
-- says so in its response rather than implying a guarantee it cannot make. A
-- caller that wants the guarantee supplies an `Idempotency-Key` header — the
-- ordinary mechanism, and the one a caller can always use because only the
-- caller knows whether this POST is a retry or a new event.
--
-- This is the same rule the supply-chain gates follow: a check that inspected
-- nothing reports UNKNOWN, never CLEAN. A webhook that could not deduplicate
-- reports `deduplicated: false`, never silence.
--
-- ── Why tenant_id is in the index ───────────────────────────────────────────
--
-- `043` gave this table a tenant and row-level security. Without `tenant_id` in
-- the key, one tenant's provider id would collide with another's — and worse,
-- the conflict would be with a row RLS hides, so `ON CONFLICT DO NOTHING` would
-- silently drop a DIFFERENT tenant's event. Scoping the index to the tenant
-- makes a conflict always mean "this tenant has already seen this event".
--
-- `event_type` is in it too: a lead and a booking that happen to carry the same
-- provider id are not the same event.
--
-- ── Reversibility ───────────────────────────────────────────────────────────
--
-- Additive and reversible. The column is nullable with no default, so existing
-- rows are unaffected and no backfill is possible or wanted — a historical row
-- has no provider id to recover. The down migration drops both, and dropping
-- them loses only the duplicate protection, never a record.
-- =============================================================================

BEGIN;

ALTER TABLE business_events
    ADD COLUMN IF NOT EXISTS dedupe_key TEXT;

COMMENT ON COLUMN business_events.dedupe_key IS
    'P14-R3 FINDING AQ. The external identity of the event this row came from: '
    'a provider id (e.g. Cal.com booking_id) or a caller-supplied '
    'Idempotency-Key. NULL means this source offered no stable id, so the row '
    'is NOT deduplicated — and the API reports deduplicated:false rather than '
    'implying a guarantee it cannot make.';

-- Partial: rows without an external identity are not constrained, because
-- "no id" is not a value two events can share.
CREATE UNIQUE INDEX IF NOT EXISTS uq_business_events_dedupe
    ON business_events (tenant_id, event_type, dedupe_key)
    WHERE dedupe_key IS NOT NULL;

COMMIT;
