-- =============================================================================
-- 095_owners_and_single_user_backfill.sql
--
-- ADR-0010, Groups B and C: "delete everything" and "export everything" reach
-- only the person's own rows. Decided by the owner on 2026-10-20 (Q3, Q4) and,
-- for the backfill, on the same day after Group B was measured.
--
-- ── 1. An owner column on the five tables that had none (Group C) ────────────
--
-- `tasks`, `people`, `notifications`, `briefings` and `heartbeat_surfaced`
-- record nothing about whose a row is, so erasure and export could only reach
-- them workspace-wide: one member's "delete everything" deleted everyone's.
-- Each gains `owner_user_id`, a `users` id, set to NULL if that user is
-- deleted, like Group B's `created_by` (043-045).
--
-- No DEFAULT. 093 defaults `conversations.owner_user_id` from `app.user_id`
-- with no foreign key, because a job may own a conversation. Here a job's
-- derived id is not a person, and a row a job writes should read "no owner",
-- which erasure keeps and counts (Q4), not belong to a job no erasure will
-- ever name. A DEFAULT cannot check `users`, so each writer sets the column
-- itself, exactly as Group B's writers set `created_by`:
--
--     (SELECT id FROM users WHERE id = NULLIF(current_setting('app.user_id', true), '')::uuid)
--
-- ── 2. The single-user backfill (Groups B and C) ────────────────────────────
--
-- Nothing recorded a Group B author before ADR-0010 Group B, and Group C had no
-- column at all, so every existing row has no owner, and "delete everything"
-- would keep all of them. In a tenant with exactly ONE membership row, of any
-- status, there is only one person those rows can be: this backfill attributes
-- them to that member. A tenant with two or more membership rows, a suspended
-- one included, is left alone: its rows stay without an owner, kept and
-- counted.
--
-- A fresh private install has one membership, the owner seeded by 023, so a
-- private install that never granted anyone an account is attributed in full.
--
-- One membership row alone does not prove one person. Revoking or removing a
-- member changes the row's status and keeps it, but erasing an account deletes
-- its membership, so a tenant that once had two people can now have one row.
-- Rows the erased person left with no author must not go to the one who
-- remains. So a tenant is also left alone when anything kept records another
-- person:
--
--   * an account erasure requested by anyone else (`deletion_requests`, which
--     erasure keeps by design);
--   * an invitation someone else accepted (`tenant_invitations`, kept and
--     redacted by erasure; an erased account's `accepted_by` may be NULL);
--   * a delegated account other than the primary owner (`delegated_users`,
--     the pre-092 table), including rows with no tenant.
--
-- What it still cannot see: a person removed by a direct DELETE in the
-- database, outside IRA, who left none of those three records.
--
-- ── 3. A record of what the backfill attributed ─────────────────────────────
--
-- Group C's backfill lives in the new column, so dropping the column undoes it.
-- Group B's goes into `created_by`, which predates this migration and which
-- writers fill from ADR-0010 Group B on, so the rollback cannot tell a
-- backfilled author from a recorded one by looking at the row.
-- `attribution_backfill_095` lists every Group B row this migration attributed,
-- and to whom; the rollback clears exactly those, where the value is still the
-- one written here. It holds table names, row keys and user ids: no content.
-- The runtime roles get no privilege on it; default privileges would have
-- granted them read and write, so they are revoked explicitly.
--
-- ── RLS window ──────────────────────────────────────────────────────────────
--
-- Migrations run as ira_schema_owner, and FORCE RLS applies to the owner: with
-- no app.tenant_id bound, every UPDATE here would match no rows and report
-- success. Forcing is lifted on the tables read or written, for this
-- transaction only, and restored at the end, as in 049, 093 and 094. A failure
-- anywhere rolls the window shut with everything else.
--
-- Idempotent: the columns and the record table are created IF NOT EXISTS, and
-- a second run finds no row without an owner in a single-member tenant.
--
-- Rollback: rollback/095_owners_and_single_user_backfill_down.sql
-- =============================================================================

BEGIN;

-- 1. The owner column -------------------------------------------------------

ALTER TABLE tasks              ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE people             ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE notifications      ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE briefings          ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE heartbeat_surfaced ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL;

COMMENT ON COLUMN tasks.owner_user_id IS
    'The account this task is personal to (ADR-0010 Group C). Set by the writer '
    'from app.user_id when that id is an account; NULL = no owner: a job wrote '
    'it, or it predates 095 in a tenant with more than one member. Erasure and '
    'export reach a row only through its owner; rows with no owner are kept and '
    'counted.';
COMMENT ON COLUMN people.owner_user_id IS
    'The account whose relationship record this is (ADR-0010 Group C). See tasks.owner_user_id.';
COMMENT ON COLUMN notifications.owner_user_id IS
    'The account this notification was raised for (ADR-0010 Group C). See tasks.owner_user_id.';
COMMENT ON COLUMN briefings.owner_user_id IS
    'The account this briefing was generated for (ADR-0010 Group C). See tasks.owner_user_id.';
COMMENT ON COLUMN heartbeat_surfaced.owner_user_id IS
    'The account this surfaced item was claimed for (ADR-0010 Group C). See tasks.owner_user_id.';

-- 2. The record of the Group B backfill --------------------------------------

CREATE TABLE IF NOT EXISTS attribution_backfill_095 (
    table_name    TEXT NOT NULL,
    row_key       TEXT NOT NULL,
    attributed_to UUID NOT NULL,
    PRIMARY KEY (table_name, row_key)
);
COMMENT ON TABLE attribution_backfill_095 IS
    'The Group B rows 095 attributed to the only member of a single-member '
    'tenant, so rollback/095 clears exactly those. Row keys, no content. No '
    'runtime grant.';
REVOKE ALL ON attribution_backfill_095 FROM PUBLIC;
REVOKE ALL ON attribution_backfill_095 FROM ira_app, ira_worker;

-- 3. The backfill, inside the RLS window -------------------------------------

ALTER TABLE tenant_memberships   NO FORCE ROW LEVEL SECURITY;
ALTER TABLE deletion_requests    NO FORCE ROW LEVEL SECURITY;
ALTER TABLE tenant_invitations   NO FORCE ROW LEVEL SECURITY;
ALTER TABLE delegated_users      NO FORCE ROW LEVEL SECURITY;
ALTER TABLE tasks                NO FORCE ROW LEVEL SECURITY;
ALTER TABLE people               NO FORCE ROW LEVEL SECURITY;
ALTER TABLE notifications        NO FORCE ROW LEVEL SECURITY;
ALTER TABLE briefings            NO FORCE ROW LEVEL SECURITY;
ALTER TABLE heartbeat_surfaced   NO FORCE ROW LEVEL SECURITY;
ALTER TABLE projects             NO FORCE ROW LEVEL SECURITY;
ALTER TABLE agents               NO FORCE ROW LEVEL SECURITY;
ALTER TABLE decisions            NO FORCE ROW LEVEL SECURITY;
ALTER TABLE calendar_events      NO FORCE ROW LEVEL SECURITY;
ALTER TABLE reminders            NO FORCE ROW LEVEL SECURITY;
ALTER TABLE entities             NO FORCE ROW LEVEL SECURITY;
ALTER TABLE edges                NO FORCE ROW LEVEL SECURITY;
ALTER TABLE business_events      NO FORCE ROW LEVEL SECURITY;
ALTER TABLE strategy_predictions NO FORCE ROW LEVEL SECURITY;
ALTER TABLE strategy_calibration NO FORCE ROW LEVEL SECURITY;
ALTER TABLE model_performance    NO FORCE ROW LEVEL SECURITY;

-- The tenants with exactly one membership row, and that member, where nothing
-- kept records another person (see 2 above). IF NOT EXISTS so a second run
-- inside the same transaction (a test's) reuses it rather than failing;
-- ON COMMIT DROP removes it, so no DROP is needed.
CREATE TEMPORARY TABLE IF NOT EXISTS sole_member_095 ON COMMIT DROP AS
SELECT m.tenant_id, m.user_id
  FROM (SELECT tenant_id, (array_agg(user_id))[1] AS user_id
          FROM tenant_memberships
         GROUP BY tenant_id
        HAVING count(*) = 1) m
 WHERE NOT EXISTS (SELECT 1 FROM deletion_requests r
                    WHERE r.tenant_id = m.tenant_id AND r.scope = 'account'
                      AND r.user_id IS DISTINCT FROM m.user_id)
   AND NOT EXISTS (SELECT 1 FROM tenant_invitations i
                    WHERE i.tenant_id = m.tenant_id AND i.accepted_at IS NOT NULL
                      AND i.accepted_by IS DISTINCT FROM m.user_id)
   AND NOT EXISTS (SELECT 1 FROM delegated_users d
                    WHERE (d.tenant_id = m.tenant_id OR d.tenant_id IS NULL)
                      AND NOT d.is_primary_owner
                      AND d.user_id IS DISTINCT FROM m.user_id);

-- Group C: the new column, so nothing needs recording.
UPDATE tasks              t SET owner_user_id = s.user_id FROM sole_member_095 s WHERE t.tenant_id = s.tenant_id AND t.owner_user_id IS NULL;
UPDATE people             t SET owner_user_id = s.user_id FROM sole_member_095 s WHERE t.tenant_id = s.tenant_id AND t.owner_user_id IS NULL;
UPDATE notifications      t SET owner_user_id = s.user_id FROM sole_member_095 s WHERE t.tenant_id = s.tenant_id AND t.owner_user_id IS NULL;
UPDATE briefings          t SET owner_user_id = s.user_id FROM sole_member_095 s WHERE t.tenant_id = s.tenant_id AND t.owner_user_id IS NULL;
UPDATE heartbeat_surfaced t SET owner_user_id = s.user_id FROM sole_member_095 s WHERE t.tenant_id = s.tenant_id AND t.owner_user_id IS NULL;

-- Group B: an existing column, so every row attributed is recorded.
WITH done AS (UPDATE projects t SET created_by = s.user_id FROM sole_member_095 s
               WHERE t.tenant_id = s.tenant_id AND t.created_by IS NULL
           RETURNING t.id::text AS row_key, s.user_id)
INSERT INTO attribution_backfill_095 SELECT 'projects', row_key, user_id FROM done ON CONFLICT DO NOTHING;
WITH done AS (UPDATE agents t SET created_by = s.user_id FROM sole_member_095 s
               WHERE t.tenant_id = s.tenant_id AND t.created_by IS NULL
           RETURNING t.id::text AS row_key, s.user_id)
INSERT INTO attribution_backfill_095 SELECT 'agents', row_key, user_id FROM done ON CONFLICT DO NOTHING;
WITH done AS (UPDATE decisions t SET created_by = s.user_id FROM sole_member_095 s
               WHERE t.tenant_id = s.tenant_id AND t.created_by IS NULL
           RETURNING t.id::text AS row_key, s.user_id)
INSERT INTO attribution_backfill_095 SELECT 'decisions', row_key, user_id FROM done ON CONFLICT DO NOTHING;
WITH done AS (UPDATE calendar_events t SET created_by = s.user_id FROM sole_member_095 s
               WHERE t.tenant_id = s.tenant_id AND t.created_by IS NULL
           RETURNING t.id::text AS row_key, s.user_id)
INSERT INTO attribution_backfill_095 SELECT 'calendar_events', row_key, user_id FROM done ON CONFLICT DO NOTHING;
WITH done AS (UPDATE reminders t SET created_by = s.user_id FROM sole_member_095 s
               WHERE t.tenant_id = s.tenant_id AND t.created_by IS NULL
           RETURNING t.id::text AS row_key, s.user_id)
INSERT INTO attribution_backfill_095 SELECT 'reminders', row_key, user_id FROM done ON CONFLICT DO NOTHING;
WITH done AS (UPDATE entities t SET created_by = s.user_id FROM sole_member_095 s
               WHERE t.tenant_id = s.tenant_id AND t.created_by IS NULL
           RETURNING t.id::text AS row_key, s.user_id)
INSERT INTO attribution_backfill_095 SELECT 'entities', row_key, user_id FROM done ON CONFLICT DO NOTHING;
WITH done AS (UPDATE edges t SET created_by = s.user_id FROM sole_member_095 s
               WHERE t.tenant_id = s.tenant_id AND t.created_by IS NULL
           RETURNING t.id::text AS row_key, s.user_id)
INSERT INTO attribution_backfill_095 SELECT 'edges', row_key, user_id FROM done ON CONFLICT DO NOTHING;
WITH done AS (UPDATE business_events t SET created_by = s.user_id FROM sole_member_095 s
               WHERE t.tenant_id = s.tenant_id AND t.created_by IS NULL
           RETURNING t.id::text AS row_key, s.user_id)
INSERT INTO attribution_backfill_095 SELECT 'business_events', row_key, user_id FROM done ON CONFLICT DO NOTHING;
WITH done AS (UPDATE strategy_predictions t SET created_by = s.user_id FROM sole_member_095 s
               WHERE t.tenant_id = s.tenant_id AND t.created_by IS NULL
           RETURNING t.id::text AS row_key, s.user_id)
INSERT INTO attribution_backfill_095 SELECT 'strategy_predictions', row_key, user_id FROM done ON CONFLICT DO NOTHING;
-- strategy_calibration's key is (tenant_id, domain); it has no id.
WITH done AS (UPDATE strategy_calibration t SET created_by = s.user_id FROM sole_member_095 s
               WHERE t.tenant_id = s.tenant_id AND t.created_by IS NULL
           RETURNING t.tenant_id::text || '/' || t.domain AS row_key, s.user_id)
INSERT INTO attribution_backfill_095 SELECT 'strategy_calibration', row_key, user_id FROM done ON CONFLICT DO NOTHING;
WITH done AS (UPDATE model_performance t SET created_by = s.user_id FROM sole_member_095 s
               WHERE t.tenant_id = s.tenant_id AND t.created_by IS NULL
           RETURNING t.id::text AS row_key, s.user_id)
INSERT INTO attribution_backfill_095 SELECT 'model_performance', row_key, user_id FROM done ON CONFLICT DO NOTHING;

DO $$
BEGIN
    RAISE NOTICE '095: % single-member tenant(s); % Group B row(s) attributed',
        (SELECT count(*) FROM sole_member_095),
        (SELECT count(*) FROM attribution_backfill_095);
END $$;

ALTER TABLE tenant_memberships   FORCE ROW LEVEL SECURITY;
ALTER TABLE deletion_requests    FORCE ROW LEVEL SECURITY;
ALTER TABLE tenant_invitations   FORCE ROW LEVEL SECURITY;
ALTER TABLE delegated_users      FORCE ROW LEVEL SECURITY;
ALTER TABLE tasks                FORCE ROW LEVEL SECURITY;
ALTER TABLE people               FORCE ROW LEVEL SECURITY;
ALTER TABLE notifications        FORCE ROW LEVEL SECURITY;
ALTER TABLE briefings            FORCE ROW LEVEL SECURITY;
ALTER TABLE heartbeat_surfaced   FORCE ROW LEVEL SECURITY;
ALTER TABLE projects             FORCE ROW LEVEL SECURITY;
ALTER TABLE agents               FORCE ROW LEVEL SECURITY;
ALTER TABLE decisions            FORCE ROW LEVEL SECURITY;
ALTER TABLE calendar_events      FORCE ROW LEVEL SECURITY;
ALTER TABLE reminders            FORCE ROW LEVEL SECURITY;
ALTER TABLE entities             FORCE ROW LEVEL SECURITY;
ALTER TABLE edges                FORCE ROW LEVEL SECURITY;
ALTER TABLE business_events      FORCE ROW LEVEL SECURITY;
ALTER TABLE strategy_predictions FORCE ROW LEVEL SECURITY;
ALTER TABLE strategy_calibration FORCE ROW LEVEL SECURITY;
ALTER TABLE model_performance    FORCE ROW LEVEL SECURITY;

COMMIT;
