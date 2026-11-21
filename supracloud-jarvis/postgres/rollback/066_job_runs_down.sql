-- =============================================================================
-- rollback/066_job_runs_down.sql
--
-- Drops `job_runs`.
--
-- ── It REFUSES while a run is in flight ────────────────────────────────────
--
-- A `running` row with no `finished_at` means a scheduler process is either
-- working right now or died mid-pass. Dropping the table in either case
-- destroys the only record that the pass began — and the second case is
-- precisely when somebody needs it, because "did last night's retention finish"
-- becomes unanswerable.
--
-- Unlike the audit-log guards, this one is genuinely safe to clear: stopping the
-- scheduler and marking the row `interrupted` loses nothing, because the work is
-- idempotent and the next run redoes it. The HINT says so rather than leaving
-- the operator to work it out.
--
-- Dropping completed history is not guarded. It is a record of work, not of
-- access, and 061's reasoning about intruders removing their own arrival does
-- not apply: nothing here says who reached what.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    in_flight INTEGER;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'job_runs') THEN
        SELECT count(*) INTO in_flight
          FROM job_runs WHERE finished_at IS NULL;

        IF in_flight > 0 THEN
            RAISE EXCEPTION
                '066-down REFUSING: % job run(s) have not finished. Dropping '
                'the table now destroys the only record that they started.',
                in_flight
            USING HINT =
                'Stop the scheduler, then mark them interrupted: UPDATE '
                'job_runs SET status = ''interrupted'', finished_at = NOW() '
                'WHERE finished_at IS NULL. The work is idempotent, so nothing '
                'is lost by doing this — the next run repeats it.';
        END IF;
    END IF;
END $$;

DROP TABLE IF EXISTS job_runs;

COMMIT;
