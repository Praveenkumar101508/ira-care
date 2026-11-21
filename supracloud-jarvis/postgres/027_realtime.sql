-- =============================================================================
-- 027_realtime.sql — Phase 6: real-time voice and avatar session telemetry
--
-- A live conversation with a companion produces two very different things:
--
--   * the CONVERSATION  — audio, transcripts, what was actually said;
--   * the CONNECTION    — which transport, which quality rung, how long the
--                         first audio packet took, how many times it dropped.
--
-- This migration stores ONLY the second, and is built so that it cannot store
-- the first even by mistake. That is the whole design constraint: a realtime
-- stack routes a user's live microphone through an external SFU, and the one
-- claim that must never quietly become false is "IRA is not recording this".
--
-- ── How "no audio, no transcripts" is enforced, not just promised ────────────
--
-- A comment saying "don't put transcripts here" is worth nothing. Three
-- structural devices instead:
--
--   1. NO COLUMN CAN HOLD CONTENT. Every column below is a UUID, a timestamp,
--      an integer, a boolean, or a TEXT column constrained by CHECK to a fixed
--      enumeration. There is no free TEXT column and no BYTEA anywhere in this
--      migration. A transcript has nowhere to go.
--
--   2. THE ONE JSONB COLUMN REFUSES STRINGS. `realtime_events.metrics` is
--      guarded by `realtime_metrics_are_measurements()`, which requires a flat
--      object whose keys are on a fixed allow-list and whose values are
--      numbers or booleans. `{"transcript": "..."}` fails the CHECK — not
--      because "transcript" is blocklisted, but because a string is.
--
--   3. THE DISCLOSURE IS A GENERATED COLUMN. `realtime_sessions.audio_stored`
--      and `.transcript_stored` are GENERATED ALWAYS AS (FALSE) STORED. No
--      INSERT or UPDATE can set them. When the Privacy Centre tells a user
--      "no audio from this session was stored", it is reading a value the
--      database guarantees rather than a literal someone typed in Python.
--
-- ── What this migration does NOT claim ──────────────────────────────────────
--
-- It does not make the media path private. Audio and video traverse whichever
-- external provider the session used; that provider sees the media. What is
-- recorded here is that the traversal happened, to which provider, and when —
-- so the disclosure surface can be truthful about it. End-to-end encryption of
-- the media is NOT provided and is not claimed anywhere in Phase 6.
--
-- It does not, by existing, mean a realtime provider is configured. With no
-- provider credentials the only reachable rung of the ladder is 'text_only',
-- and `realtime_sessions.provider` records 'none' — which is the honest value,
-- not a failure.
--
-- Additive. Idempotent. Rollback: rollback/027_realtime_down.sql.
-- =============================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. The capability ladder — global reference data, not customer data.
--
-- Every rung a session can occupy, richest first, with the conditions under
-- which it is viable. Deliberately NOT tenant-scoped and deliberately NO RLS:
-- this is a fixed product vocabulary, identical for every tenant, and holds
-- nothing about anybody. Same call as 024's catalogues and 025's tool_policies.
--
-- `target_first_audio_ms` is the STATED BUDGET, in one place, so that the
-- latency assertions in core/realtime and the number quoted in the docs cannot
-- drift apart. It is a budget to measure against — not a measured result, and
-- not a guarantee. Phase 6 measures against it and reports the truth.
--
-- 'text_only' has a NULL budget because it produces no audio at all. NULL here
-- means "not applicable"; a 0 would read as "instant", which would be a lie.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS realtime_capability_profiles (
    level                   TEXT PRIMARY KEY
                                CHECK (level IN ('full_avatar', 'low_resource_avatar',
                                                 'voice_only', 'text_only')),
    -- 1 is the richest rung. Descent is always to rung+1; there is no path
    -- that skips a rung, so a session's history reads as a monotonic ladder.
    rung                    SMALLINT NOT NULL UNIQUE CHECK (rung BETWEEN 1 AND 4),

    renders_avatar          BOOLEAN NOT NULL,
    produces_audio          BOOLEAN NOT NULL,

    target_first_audio_ms   INTEGER CHECK (target_first_audio_ms > 0),
    min_downlink_kbps       INTEGER NOT NULL CHECK (min_downlink_kbps >= 0),

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- A rung that renders an avatar but produces no audio is not a rung on
    -- this ladder; it is a bug in whoever inserted it.
    CONSTRAINT avatar_rungs_speak CHECK (NOT renders_avatar OR produces_audio),
    -- A rung that produces audio must state the budget it is measured against.
    CONSTRAINT audible_rungs_have_a_budget
        CHECK (produces_audio = (target_first_audio_ms IS NOT NULL))
);

INSERT INTO realtime_capability_profiles
    (level, rung, renders_avatar, produces_audio, target_first_audio_ms, min_downlink_kbps)
VALUES
    ('full_avatar',         1, TRUE,  TRUE,  1200, 1500),
    ('low_resource_avatar', 2, TRUE,  TRUE,  1200,  600),
    ('voice_only',          3, FALSE, TRUE,   900,  100),
    ('text_only',           4, FALSE, FALSE,  NULL,   0)
ON CONFLICT (level) DO UPDATE SET
    rung                  = EXCLUDED.rung,
    renders_avatar        = EXCLUDED.renders_avatar,
    produces_audio        = EXCLUDED.produces_audio,
    target_first_audio_ms = EXCLUDED.target_first_audio_ms,
    min_downlink_kbps     = EXCLUDED.min_downlink_kbps;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. The guard that makes "no content" structural rather than aspirational.
--
-- IMMUTABLE so it can be used in a CHECK constraint. Deliberately an
-- ALLOW-list of keys AND a type restriction: blocking the key 'transcript'
-- alone would be trivially defeated by calling it 'text'. Requiring every
-- value to be a number or a boolean means no string of any name can be
-- stored — which is the property actually wanted.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION realtime_metrics_are_measurements(m JSONB)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT m IS NULL
        OR (
            jsonb_typeof(m) = 'object'
            AND NOT EXISTS (
                SELECT 1
                FROM jsonb_each(m) AS kv(k, v)
                WHERE kv.k NOT IN (
                        'first_audio_ms', 'connect_ms', 'rtt_ms', 'jitter_ms',
                        'packet_loss_pct', 'downlink_kbps', 'uplink_kbps',
                        'attempt', 'backoff_ms', 'from_rung', 'to_rung',
                        'budget_ms', 'within_budget', 'video_enabled',
                        'microphone_enabled'
                      )
                   OR jsonb_typeof(kv.v) NOT IN ('number', 'boolean')
            )
        );
$$;

COMMENT ON FUNCTION realtime_metrics_are_measurements(JSONB) IS
    'Realtime telemetry must be measurements, never content. Enforces a flat '
    'object of allow-listed keys whose values are numbers or booleans, so no '
    'string — and therefore no transcript, no audio, no utterance — can be '
    'stored in realtime_events.metrics under any key name.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Sessions — one row per attempt to open a live conversation.
--
-- A row exists from the moment a session is REQUESTED, before any transport is
-- negotiated, so a session that never connects is visible rather than absent.
-- "IRA tried to open a live session and failed" is information the user is
-- entitled to; a table that only records successes cannot express it.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS realtime_sessions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id             UUID REFERENCES users(id) ON DELETE CASCADE,
    companion_id        UUID REFERENCES companions(id) ON DELETE SET NULL,

    -- Which external transport carried the media, by name. 'none' is a real,
    -- expected value: it is what a deployment with no provider configured
    -- records, and it is what the text-only rung uses.
    provider            TEXT NOT NULL DEFAULT 'none'
                            CHECK (provider IN ('none', 'livekit')),
    -- Where the provider's edge was, when the provider reports it. Coarse and
    -- enumerated on purpose: a free-text region string is a place to hide an
    -- identifier, and a precise location is not needed to be truthful about
    -- jurisdiction.
    provider_region     TEXT NOT NULL DEFAULT 'unknown'
                            CHECK (provider_region IN ('unknown', 'eu', 'uk', 'us',
                                                       'apac', 'other')),

    state               TEXT NOT NULL DEFAULT 'requested'
                            CHECK (state IN ('requested', 'connecting', 'live',
                                             'degraded', 'reconnecting',
                                             'ended', 'failed')),
    -- The rung the session opened on, and the rung it is on now. Both, because
    -- "started full and fell to voice-only" and "was voice-only throughout"
    -- are different experiences and the user can tell them apart.
    opened_level        TEXT NOT NULL DEFAULT 'text_only'
                            REFERENCES realtime_capability_profiles(level),
    current_level       TEXT NOT NULL DEFAULT 'text_only'
                            REFERENCES realtime_capability_profiles(level),

    requested_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    connected_at        TIMESTAMPTZ,
    ended_at            TIMESTAMPTZ,

    -- Measured, not assumed. NULL means "no audio was produced", which is the
    -- honest value for a session that failed or never left text_only.
    first_audio_ms      INTEGER CHECK (first_audio_ms >= 0),
    reconnect_count     INTEGER NOT NULL DEFAULT 0 CHECK (reconnect_count >= 0),
    downgrade_count     INTEGER NOT NULL DEFAULT 0 CHECK (downgrade_count >= 0),

    end_reason          TEXT CHECK (end_reason IN (
                            'user_ended', 'user_interrupted', 'idle_timeout',
                            'transport_lost', 'reconnect_exhausted',
                            'provider_unavailable', 'permission_denied',
                            'no_provider_configured', 'server_shutdown',
                            'capability_floor_reached', 'error')),

    -- ── The structural disclosure ────────────────────────────────────────────
    -- GENERATED ALWAYS: no INSERT and no UPDATE can make either of these true.
    -- Storing session audio or a transcript would require a schema migration
    -- that visibly changes these lines — which is exactly the review moment
    -- such a change deserves.
    audio_stored        BOOLEAN GENERATED ALWAYS AS (FALSE) STORED,
    transcript_stored   BOOLEAN GENERATED ALWAYS AS (FALSE) STORED,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Timestamps must tell a coherent story or the latency figures derived
    -- from them are meaningless.
    CONSTRAINT connected_after_requested
        CHECK (connected_at IS NULL OR connected_at >= requested_at),
    CONSTRAINT ended_after_requested
        CHECK (ended_at IS NULL OR ended_at >= requested_at),
    -- A session cannot have produced audio without having connected.
    CONSTRAINT audio_implies_connection
        CHECK (first_audio_ms IS NULL OR connected_at IS NOT NULL),
    -- A finished session must say why it finished.
    CONSTRAINT terminal_states_give_a_reason
        CHECK (state NOT IN ('ended', 'failed') OR end_reason IS NOT NULL)
);

COMMENT ON COLUMN realtime_sessions.audio_stored IS
    'Always FALSE, enforced by the database. Read by the disclosure surface so '
    'the claim shown to the user is backed by the schema rather than asserted '
    'in application code.';

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Events — the transitions within a session.
--
-- Telemetry, not audit. It is deliberately NOT append-only in the trust-ledger
-- sense: it is deletable, because these rows are linked to a user and Phase
-- 5's erasure must be able to reach them. What it does refuse is UPDATE — a
-- retroactively edited latency figure is worse than no figure, and there is no
-- legitimate reason to revise a measurement after the fact.
--
-- Deleting a row here loses no evidence about what IRA was PERMITTED to do;
-- that lives in permission_decisions and the trust ledger, which remain
-- append-only. See ADR-0004 for where that line is drawn.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS realtime_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    session_id      UUID NOT NULL REFERENCES realtime_sessions(id) ON DELETE CASCADE,

    kind            TEXT NOT NULL CHECK (kind IN (
                        'requested', 'connecting', 'connected', 'first_audio',
                        'degraded', 'downgraded', 'upgraded',
                        'reconnect_attempt', 'reconnected', 'interrupted',
                        'microphone_opened', 'microphone_closed',
                        'ended', 'failed')),
    level           TEXT REFERENCES realtime_capability_profiles(level),

    -- Elapsed since the session was requested. One monotonic axis, so events
    -- from different sources are comparable without trusting clock skew.
    at_ms           INTEGER NOT NULL CHECK (at_ms >= 0),
    at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    metrics         JSONB NOT NULL DEFAULT '{}'
                        CHECK (realtime_metrics_are_measurements(metrics)),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Indexes. Every read here filters on tenant_id first — without an index,
--    RLS turns each one into a sequential scan.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS realtime_sessions_tenant_idx
    ON realtime_sessions (tenant_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS realtime_sessions_live_idx
    ON realtime_sessions (tenant_id, state)
    WHERE state IN ('requested', 'connecting', 'live', 'degraded', 'reconnecting');
CREATE INDEX IF NOT EXISTS realtime_events_session_idx
    ON realtime_events (tenant_id, session_id, at_ms);

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. Row-Level Security on the customer-data tables.
--
-- realtime_capability_profiles is intentionally absent: it is global reference
-- data with no tenant column, and giving it a policy keyed on a column it does
-- not have would fail at CREATE POLICY, not fail closed.
-- ─────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    t TEXT;
    scoped TEXT[] := ARRAY[
        'realtime_sessions',
        'realtime_events'
    ];
BEGIN
    FOREACH t IN ARRAY scoped LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON %I
                USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        $f$, t);
    END LOOP;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 7. Measurements are not revisable.
--
-- UPDATE is refused; DELETE is permitted so erasure can reach these rows.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION realtime_events_are_not_revisable()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'realtime_events is a measurement log: rows may be inserted and (for '
        'erasure) deleted, but never edited. A latency figure changed after '
        'the fact is worse than no figure at all.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS realtime_events_no_update ON realtime_events;
CREATE TRIGGER realtime_events_no_update
    BEFORE UPDATE ON realtime_events
    FOR EACH ROW EXECUTE FUNCTION realtime_events_are_not_revisable();

COMMIT;
