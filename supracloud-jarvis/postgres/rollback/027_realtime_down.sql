-- =============================================================================
-- rollback/027_realtime_down.sql — rollback for ../027_realtime.sql
--
-- ⚠️  THIS FILE LIVES IN rollback/ ON PURPOSE. DO NOT MOVE IT UP A LEVEL.
--     postgres/ is mounted as /docker-entrypoint-initdb.d and every .sql there
--     runs on a fresh install, in order. See tests/tenancy/test_migration_hygiene.py.
--
-- Reverses Phase 6's realtime telemetry schema. Run ONLY, and MANUALLY.
--
-- ── What is lost, and what is not ────────────────────────────────────────────
--
-- Lost: the record of which live sessions were opened, which external provider
-- carried the media, how long the first audio took, and how often the
-- connection dropped or fell down the capability ladder.
--
-- Not lost: no conversation content, because none was ever here. That is the
-- one pleasant property of a schema that is structurally incapable of holding
-- content — rolling it back cannot destroy anything a user said.
--
-- ── The reason to hesitate anyway ────────────────────────────────────────────
--
-- `realtime_sessions` is the evidence for a specific disclosure: which external
-- provider a user's live microphone was routed through, and when. A user who
-- later asks "who else has heard me?" is answered from this table. Dropping it
-- does not undo the routing — it only removes the ability to account for it.
--
-- So: export before rolling back on any deployment that has served real
-- sessions. This script REFUSES to run while more than one tenant has rows,
-- and warns (without refusing) when a single tenant does.
-- =============================================================================

BEGIN;

-- ── Guard: refuse on a deployment that has served more than one tenant ───────
DO $$
DECLARE
    tenant_count INTEGER;
    session_count INTEGER;
BEGIN
    SELECT COUNT(DISTINCT tenant_id), COUNT(*)
      INTO tenant_count, session_count
      FROM realtime_sessions;

    IF tenant_count > 1 THEN
        RAISE EXCEPTION
            'Refusing to roll back: % tenants across % realtime session(s) '
            'have provider-routing history here. This table is the evidence '
            'for which external provider carried each user''s live '
            'microphone; dropping it removes the ability to answer that '
            'question, without undoing the routing. Export per tenant first.',
            tenant_count, session_count;
    END IF;

    IF session_count > 0 THEN
        RAISE WARNING
            'Rolling back % realtime session record(s) for a single tenant. '
            'The provider-routing history for those sessions will be gone.',
            session_count;
    END IF;
END
$$;

-- ── Drop in dependency order ─────────────────────────────────────────────────
-- realtime_events references realtime_sessions; both reference the profiles.

DROP TRIGGER IF EXISTS realtime_events_no_update ON realtime_events;
DROP FUNCTION IF EXISTS realtime_events_are_not_revisable();

DROP INDEX IF EXISTS realtime_events_session_idx;
DROP INDEX IF EXISTS realtime_sessions_live_idx;
DROP INDEX IF EXISTS realtime_sessions_tenant_idx;

DROP TABLE IF EXISTS realtime_events;
DROP TABLE IF EXISTS realtime_sessions;
DROP TABLE IF EXISTS realtime_capability_profiles;

-- Dropped last: the CHECK constraints that used it are gone with the tables.
DROP FUNCTION IF EXISTS realtime_metrics_are_measurements(JSONB);

COMMIT;
