-- =============================================================================
-- 063_contract_stage.sql — Phase 4 batch 03, the plaintext columns go
--
-- `036` and `037` added the encrypted destinations and kept the plaintext
-- beside them, deliberately: an expand stage that dropped the old column in the
-- same breath would have had nothing to fall back to. `062` supplied the
-- per-record key columns and the worker filled them in. This is the contract
-- stage — the one that makes "encrypted at rest" true rather than "available".
--
-- ── This migration refuses unless the work is actually done ─────────────────
--
-- It does not trust that the worker ran. It reads the two contract gates and
-- refuses on anything short of ready, because the failure it is preventing is
-- unrecoverable: `secret` and `embedding` are the ONLY copy of a second factor
-- and a voiceprint for any row that is not encrypted, and DROP COLUMN does not
-- ask twice.
--
-- The gates are `totp_migration_status.contract_ready` and
-- `voice_migration_status.contract_ready`. Between them they require: every
-- live row ENCRYPTED, every row's owner resolved, and — added by `062` — no row
-- claiming ENCRYPTED while holding no wrapped key, which would be ciphertext
-- nobody can open.
--
-- ── Counted with FORCE lifted, and this is the whole migration ──────────────
--
-- `totp_secrets` and `voice_profiles` are USER_PRIVATE under FORCE ROW LEVEL
-- SECURITY, keyed on `app.user_id`. This script binds no GUCs. An unbound
-- connection therefore matches ZERO rows, every count is zero, and BOTH gates
-- report `contract_ready = true` — on a database where every secret is still
-- plaintext and merely invisible.
--
-- That is the P2-05 shape, and this is the worst instance of it in the entire
-- chain: the gate would pass, the columns would drop, and every second factor
-- and every voiceprint in the deployment would be destroyed in one statement
-- that reported success. `scripts/phase3b/migrate_secrets.py` records the same
-- reasoning at length and refuses to report at all under a filtered role.
--
-- So FORCE is lifted for the read and restored immediately, and the guard
-- additionally refuses if the totals it just counted are inconsistent with the
-- raw table — which is what a lifted-FORCE read that somehow stayed filtered
-- would look like.
--
-- ── What is NOT recoverable afterwards ──────────────────────────────────────
--
-- The reverse paths. `scripts/phase2/mfa_migrate.py decrypt` and
-- `scripts/phase4/encrypt_voiceprints.py decrypt` write the plaintext columns,
-- and after this migration there are none to write. `rollback/062` can no
-- longer be satisfied by decrypting first, which is exactly what its HINT tells
-- an operator to do.
--
-- That is stated in `rollback/063` rather than discovered, and it is the reason
-- this is a separate migration from `062` rather than the end of it.
--
-- ── Why this lives in `contract/` and not in the chain ─────────────────────
--
-- The migration runner globs `postgres/*.sql` at the top level only, and
-- `provisioning/` sits outside it for a stated reason: *"role creation is an
-- operator decision rather than a migration."* Dropping the only copy of a
-- second factor and a voiceprint is the same kind of decision, and a stronger
-- case for it.
--
-- The gate below refuses when it is unsafe. But even when it is SAFE, the
-- timing is not a deploy's to choose: an operator runs the workers, confirms
-- both gates, takes whatever backup their retention policy calls for, and only
-- then contracts. A deployment that silently dropped the fallback the moment it
-- shipped would take that decision away.
--
-- There is a second, practical consequence and it is worth stating rather than
-- discovering: the migration WORKERS need the plaintext columns. So does the
-- test corpus that covers them — `test_p205_mfa_encryption` exercises the
-- plaintext-to-ciphertext path and cannot do that against a schema where the
-- plaintext column is already gone. Auto-applying this would delete the only
-- schema in which the upgrade path can be tested, on every fresh database,
-- before any of it ran.
--
--     Apply:   psql -f postgres/contract/063_contract_stage.sql
--     Reverse: psql -f postgres/contract/rollback/063_contract_stage_down.sql
--
-- Rollback: contract/rollback/063_contract_stage_down.sql
-- =============================================================================

BEGIN;

DO $$
DECLARE
    totp_ready       boolean;
    voice_ready      boolean;
    totp_plaintext   bigint;
    voice_plaintext  bigint;
    totp_seen        bigint;
    voice_seen       bigint;
    totp_raw         bigint;
    voice_raw        bigint;
BEGIN
    -- Nothing to contract if the expand stage never happened.
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'totp_secrets' AND column_name = 'secret')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'voice_profiles' AND column_name = 'embedding') THEN
        RETURN;
    END IF;

    EXECUTE 'ALTER TABLE totp_secrets   NO FORCE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE voice_profiles NO FORCE ROW LEVEL SECURITY';

    -- ASSERT THE LIFT HAPPENED, before counting anything.
    --
    -- The first version of this guard counted the raw tables and refused if a
    -- non-empty table read as zero. That cannot work, and testing it is what
    -- showed why: the raw count goes through the SAME policy as the view, so a
    -- filtered read reports the table empty too. The check compared one blinded
    -- number against another and would have agreed with itself all the way to
    -- DROP COLUMN.
    --
    -- There is no filter-immune row count available here. What IS available is
    -- whether the filter is on, so that is what gets asserted — the state, not
    -- its consequences.
    IF (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'public.totp_secrets'::regclass)
       OR (SELECT relforcerowsecurity FROM pg_class WHERE oid = 'public.voice_profiles'::regclass) THEN
        RAISE EXCEPTION
            '063 REFUSING: FORCE ROW LEVEL SECURITY is still enabled while the '
            'contract gates are being read.'
            USING HINT =
                'Every count below would be a count of what this role happens '
                'to see, which for a USER_PRIVATE table with no app.user_id '
                'bound is none — so both gates would report ready for a '
                'database where every secret is still plaintext and merely '
                'invisible, and the DROP COLUMN would destroy every second '
                'factor and every voiceprint in the deployment while reporting '
                'success.';
    END IF;

    -- Belt and braces: a role that BYPASSES RLS would read correctly here, but
    -- one that is neither the owner nor a bypasser cannot lift FORCE at all and
    -- would have failed above. Recorded so the reasoning is not re-derived.
    SELECT contract_ready, still_plaintext, total_rows
      INTO totp_ready, totp_plaintext, totp_seen
      FROM totp_migration_status;
    SELECT contract_ready, still_plaintext, total_rows
      INTO voice_ready, voice_plaintext, voice_seen
      FROM voice_migration_status;

    SELECT count(*) INTO totp_raw  FROM totp_secrets;
    SELECT count(*) INTO voice_raw FROM voice_profiles;

    EXECUTE 'ALTER TABLE totp_secrets   FORCE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE voice_profiles FORCE ROW LEVEL SECURITY';

    -- The views filter (`revoked_at IS NULL`, `deletion_status = 'live'`), so
    -- the raw totals are deliberately >= the views'. A view reporting MORE rows
    -- than the table holds is not a filtering artefact — it is the view having
    -- drifted onto a different relation — and it invalidates the gate either
    -- way.
    IF totp_seen > totp_raw OR voice_seen > voice_raw THEN
        RAISE EXCEPTION
            '063 REFUSING: a contract gate reports more rows (% TOTP, % voice) '
            'than the table holds (%, %).',
            totp_seen, voice_seen, totp_raw, voice_raw
            USING HINT =
                'The view and the table disagree about what they are counting, '
                'so the gate says nothing about the rows whose plaintext is '
                'about to be dropped.';
    END IF;

    IF NOT totp_ready THEN
        RAISE EXCEPTION
            '063 REFUSING: totp_migration_status.contract_ready is false '
            '(% row(s) still plaintext).', totp_plaintext
            USING HINT =
                'secret is the ONLY copy of those second factors. Run '
                'scripts/phase2/mfa_migrate.py to encrypt them, resolve any row '
                'whose owner is unknown, and re-run. DROP COLUMN does not ask '
                'twice.';
    END IF;

    IF NOT voice_ready THEN
        RAISE EXCEPTION
            '063 REFUSING: voice_migration_status.contract_ready is false '
            '(% row(s) still plaintext).', voice_plaintext
            USING HINT =
                'embedding is the ONLY copy of those voiceprints. Run '
                'scripts/phase4/encrypt_voiceprints.py encrypt --confirm, '
                'resolve any row whose owner is unknown, and re-run.';
    END IF;
END $$;

-- ── The contraction ─────────────────────────────────────────────────────────
--
-- No CASCADE. Nothing in the schema depends on either column — checked, not
-- assumed — and CASCADE on a deployed database drops whatever happens to
-- depend on the column, which is not a set this script knows. If a dependency
-- appears later this fails loudly, which is the outcome to want.

ALTER TABLE totp_secrets   DROP COLUMN IF EXISTS secret;
ALTER TABLE voice_profiles DROP COLUMN IF EXISTS embedding;

-- The status views survive and keep reporting, because "is everything still
-- encrypted" stays a live question after the plaintext is gone — a row that
-- somehow becomes FAILED afterwards is a row nobody can open, and it should be
-- visible rather than merely impossible.
--
-- `still_plaintext` can no longer be anything but zero, and that is worth
-- keeping rather than removing: a non-zero value in a column that cannot be
-- non-zero is a signal something reintroduced a plaintext path.

DO $$
DECLARE
    n_missing text;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'totp_secrets' AND column_name = 'secret') THEN
        RAISE EXCEPTION '063: totp_secrets.secret survived';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'voice_profiles' AND column_name = 'embedding') THEN
        RAISE EXCEPTION '063: voice_profiles.embedding survived';
    END IF;

    -- The ciphertext and its key must all still be here. A contract stage that
    -- dropped the plaintext AND lost a key column would be the destruction this
    -- whole sequence exists to prevent, performed by the migration that was
    -- supposed to complete it.
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'totp_secrets' AND column_name = 'secret_ciphertext') THEN
        RAISE EXCEPTION '063 destroyed totp_secrets.secret_ciphertext';
    END IF;
    FOR n_missing IN
        SELECT c FROM unnest(ARRAY['template_ciphertext','template_nonce',
                                   'wrapped_dek','wrap_nonce','kek_id']) AS c
         WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'voice_profiles' AND column_name = c)
    LOOP
        RAISE EXCEPTION '063 destroyed voice_profiles.%', n_missing;
    END LOOP;

    IF NOT (SELECT relforcerowsecurity FROM pg_class
             WHERE oid = 'public.totp_secrets'::regclass)
       OR NOT (SELECT relforcerowsecurity FROM pg_class
                WHERE oid = 'public.voice_profiles'::regclass) THEN
        RAISE EXCEPTION
            '063 left a secret table without FORCE ROW LEVEL SECURITY. The '
            'gate lifts it to count and must put it back.';
    END IF;
END $$;

COMMIT;
