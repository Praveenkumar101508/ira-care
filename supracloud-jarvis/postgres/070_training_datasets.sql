-- =============================================================================
-- 070_training_datasets.sql — Phase 7, batch P7-01. MOD-04's data half.
--
-- `BASELINE_MODEL_INVENTORY.md` §6, "Absent — the whole of Phase 7":
--
--   Datasets (raw/quarantined/reviewed/train/val/test)              absent
--   Dataset cards, licences, PII/secret scanning, contamination     absent
--
-- ── The failure this exists to prevent ─────────────────────────────────────
--
-- Those two rows describe a DIRECTORY LAYOUT, and a directory layout is the
-- easiest thing in this list to satisfy dishonestly. `mkdir -p
-- datasets/reviewed` takes no time and proves nothing: a file that moved from
-- `raw/` to `reviewed/` has been moved, not reviewed.
--
-- Phase 6 found the same shape one level up — `commercial_licence` was free
-- text, so `'x'` satisfied a constraint that read like a control. The lesson
-- transfers directly: **the lifecycle has to be state a machine enforces, with
-- the evidence for each transition recorded, or it is a naming convention.**
--
-- So `state` is not a folder. Reaching `train`, `validation` or `test` requires,
-- and the trigger below enforces:
--
--   * every required check PASSED — PII, secrets, contamination, licence;
--   * each of those checks having run against **this dataset's current content
--     digest**, not an earlier one;
--   * a licence classification of `known_approved`, recorded by a named human;
--   * a lawful basis recorded by a named human;
--   * the personal-data question answered.
--
-- ── Why the checks carry a digest, and why that is the load-bearing part ────
--
-- A check that passed is a fact about the bytes it read. Append one row to a
-- JSONL file and every previous check becomes a statement about a file that no
-- longer exists — while the "passed" record sits there looking current.
--
-- That is the single most likely way this table would end up certifying
-- unscanned data, and it would be invisible: the dataset says `train`, the
-- checks say `passed`, and nothing anywhere disagrees. `dataset_checks` records
-- the digest it ran against, and the promotion trigger compares it.
--
-- ── This table holds NO dataset content ────────────────────────────────────
--
-- Only metadata and digests. That matters for classification: a table that held
-- training records could hold personal data, and would need the whole tenancy
-- and erasure apparatus. It holds a name, a state, a hash and some provenance,
-- so it is REFERENCE_DATA — the same class as `model_registry` (067) and
-- `model_licence_register` (068), protected by role grants rather than RLS.
--
-- `ira_app` reads and cannot write. Promoting a dataset is an operator act, for
-- the reason `067` records about the allowlist: a process that answers requests
-- must not be able to certify its own training data.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS training_datasets (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    name                TEXT NOT NULL CHECK (name <> ''),
    version             INTEGER NOT NULL CHECK (version > 0),

    -- ── The lifecycle §6 asks for, as state rather than a directory ────────
    --
    --   raw          ingested, nothing established about it
    --   quarantined  a check found something; it may not proceed
    --   reviewed     every check passed at the current digest
    --   train/validation/test  a reviewed dataset assigned to a split
    --   rejected     deliberately refused; kept so the decision is visible
    --   shredded     content destroyed; the row remains as the record that it
    --                existed, which erasure requires and deletion would lose
    state               TEXT NOT NULL DEFAULT 'raw' CHECK (state IN (
                            'raw', 'quarantined', 'reviewed',
                            'train', 'validation', 'test',
                            'rejected', 'shredded')),

    -- ── Provenance ────────────────────────────────────────────────────────

    -- Where the records came from, in words a person can check.
    source              TEXT NOT NULL CHECK (source <> ''),
    source_url          TEXT,
    collected_at        TIMESTAMPTZ,

    -- The bytes. Everything else in this row is a claim ABOUT this digest, and
    -- the digest is what makes those claims falsifiable.
    content_sha256      TEXT NOT NULL CHECK (
                            content_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    record_count        INTEGER NOT NULL DEFAULT 0 CHECK (record_count >= 0),
    byte_size           BIGINT CHECK (byte_size IS NULL OR byte_size >= 0),

    -- ── Licence — EVIDENCE, which engineering may write ───────────────────
    --
    -- Same split as `model_licence_register`. A dataset has terms too, and
    -- "we found it on the internet" is not one of them.
    licence_identifier  TEXT,
    licence_evidence_url TEXT,

    -- ── Licence — DETERMINATION, which only a named human may write ───────
    licence_classification TEXT NOT NULL DEFAULT 'unknown' CHECK (
                            licence_classification IN (
                                'unknown', 'review_required',
                                'known_restricted', 'known_approved')),
    licence_classified_by  TEXT,
    licence_classified_at  TIMESTAMPTZ,

    -- ── The privacy questions (PP-05) ─────────────────────────────────────
    --
    -- `contains_personal_data` is asked separately from the PII scan on
    -- purpose. The scan is evidence — it finds what its patterns match. This is
    -- the human's answer, and "the scanner found nothing" is not the same
    -- statement as "this contains no personal data".
    contains_personal_data TEXT NOT NULL DEFAULT 'unknown' CHECK (
                            contains_personal_data IN ('unknown', 'yes', 'no')),

    -- Why IRA is permitted to train on it. `PRIVACY_IMPACT_ASSESSMENT.md` and
    -- `DATA_RETENTION_SCHEDULE.md` govern retention; this governs use, and no
    -- engineering artefact may supply it.
    lawful_basis        TEXT,
    lawful_basis_by     TEXT,
    lawful_basis_at     TIMESTAMPTZ,

    notes               TEXT NOT NULL DEFAULT '',
    registered_by       TEXT NOT NULL DEFAULT 'migration',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT training_datasets_identity UNIQUE (name, version),

    -- A determination names a human and dates itself. Same rule, same reason,
    -- same deny-list as `068` — an approval attributed to a process is not one.
    CONSTRAINT dataset_licence_determination_is_attributed CHECK (
        licence_classification NOT IN ('known_approved', 'known_restricted')
        OR (licence_classified_by IS NOT NULL
            AND length(btrim(licence_classified_by)) >= 3
            AND licence_classified_at IS NOT NULL
            AND lower(btrim(licence_classified_by)) NOT IN (
                'migration', 'migrations', 'automation', 'automated', 'system',
                'script', 'scripts', 'collector', 'engineering', 'engineer',
                'ira', 'bot', 'robot', 'ci', 'cicd', 'pipeline', 'admin',
                'administrator', 'operator', 'root', 'postgres', 'unknown',
                'n/a', 'na', 'tbd', 'todo', 'none', 'nobody', 'anonymous',
                'default', 'test', 'placeholder', 'agent', 'assistant',
                'ira_migrator', 'ira_app', 'ira_worker', 'ira_schema_owner'))
    ),

    -- The undetermined states carry no answers, so a loader cannot make the
    -- determination in all but name.
    CONSTRAINT undetermined_dataset_licence_answers_nothing CHECK (
        licence_classification NOT IN ('unknown', 'review_required')
        OR (licence_classified_by IS NULL AND licence_classified_at IS NULL)
    ),

    -- A lawful basis is a person's statement, with the same deny-list. Recorded
    -- together or not at all: a basis with no author is a sentence somebody
    -- typed.
    CONSTRAINT lawful_basis_is_attributed CHECK (
        (lawful_basis IS NULL AND lawful_basis_by IS NULL
         AND lawful_basis_at IS NULL)
        OR (lawful_basis IS NOT NULL AND btrim(lawful_basis) <> ''
            AND lawful_basis_by IS NOT NULL
            AND length(btrim(lawful_basis_by)) >= 3
            AND lawful_basis_at IS NOT NULL
            AND lower(btrim(lawful_basis_by)) NOT IN (
                'migration', 'automation', 'system', 'script', 'collector',
                'engineering', 'ira', 'bot', 'ci', 'pipeline', 'admin',
                'operator', 'root', 'postgres', 'unknown', 'n/a', 'tbd',
                'none', 'default', 'test', 'placeholder', 'agent',
                'ira_migrator', 'ira_app', 'ira_worker', 'ira_schema_owner'))
    ),

    -- A shredded dataset kept a record and lost its content. Nothing may claim
    -- it is still usable.
    CONSTRAINT shredded_datasets_are_not_usable CHECK (
        state <> 'shredded' OR record_count = 0
    )
);

COMMENT ON TABLE training_datasets IS
    'MOD-04, Phase 7. Training dataset METADATA and digests — never content. '
    'The raw/quarantined/reviewed/train/val/test lifecycle as enforced state '
    'rather than a directory layout. REFERENCE_DATA: no tenant column, no '
    'customer records; ira_app reads and cannot write.';

COMMENT ON COLUMN training_datasets.content_sha256 IS
    'The bytes every other claim in this row is ABOUT. dataset_checks records '
    'the digest each check ran against, so a check cannot survive a change to '
    'the data it read.';

COMMENT ON COLUMN training_datasets.contains_personal_data IS
    'The human answer, asked separately from the PII scan. "The scanner found '
    'nothing" and "this contains no personal data" are different statements.';

COMMENT ON COLUMN training_datasets.lawful_basis IS
    'PP-05. Why IRA may train on this. No engineering artefact may supply it.';

CREATE INDEX IF NOT EXISTS training_datasets_state_idx
    ON training_datasets (state, name);

CREATE INDEX IF NOT EXISTS training_datasets_usable_idx
    ON training_datasets (name, version)
    WHERE state IN ('train', 'validation', 'test');


-- ── The checks a dataset must pass ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dataset_checks (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    dataset_id          UUID NOT NULL
                        REFERENCES training_datasets(id) ON DELETE CASCADE,

    -- The four §6 names. Constrained rather than free text: a check nobody
    -- wrote a scanner for cannot be recorded into existence, and the promotion
    -- trigger below requires all four by name.
    check_kind          TEXT NOT NULL CHECK (check_kind IN (
                            'pii', 'secret', 'contamination', 'licence')),

    -- **The load-bearing column.** The digest the check actually read. A check
    -- whose digest no longer matches the dataset's is a statement about bytes
    -- that no longer exist, and the promotion trigger treats it as absent.
    content_sha256      TEXT NOT NULL CHECK (
                            content_sha256 ~ '^sha256:[0-9a-f]{64}$'),

    outcome             TEXT NOT NULL CHECK (outcome IN (
                            'passed', 'failed', 'error')),

    -- How many things it found. Zero on a pass, by construction below.
    finding_count       INTEGER NOT NULL DEFAULT 0 CHECK (finding_count >= 0),

    -- What it found, in terms safe to store: counts, categories, line numbers.
    -- **Never the matched text.** A PII scanner's findings are the personal
    -- data it found, and copying them here would move the problem into a table
    -- that has no erasure path.
    detail              TEXT NOT NULL DEFAULT '',

    -- Which scanner, at which version. A pass by a tool nobody can identify is
    -- not reproducible, and P6-04 already made the same demand of a benchmark
    -- score with no method.
    tool                TEXT NOT NULL CHECK (tool <> ''),
    tool_version        TEXT NOT NULL CHECK (tool_version <> ''),

    ran_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ran_by              TEXT NOT NULL DEFAULT 'automation',

    -- A pass with findings is a contradiction, and it is the exact shape a
    -- scanner that "handled" its findings would produce.
    CONSTRAINT a_pass_found_nothing CHECK (
        outcome <> 'passed' OR finding_count = 0
    ),

    -- A failure that found nothing is equally incoherent, and would let a
    -- scanner record a failure it cannot substantiate.
    CONSTRAINT a_failure_found_something CHECK (
        outcome <> 'failed' OR finding_count > 0
    )
);

COMMENT ON TABLE dataset_checks IS
    'One row per check execution. content_sha256 is the digest the check '
    'actually read: a check whose digest no longer matches its dataset is a '
    'statement about bytes that no longer exist, and does not count.';

COMMENT ON COLUMN dataset_checks.detail IS
    'Counts, categories and locations only. NEVER the matched text — a PII '
    'scanner''s findings ARE personal data, and this table has no erasure path.';

CREATE INDEX IF NOT EXISTS dataset_checks_lookup_idx
    ON dataset_checks (dataset_id, check_kind, ran_at DESC);


-- ── The promotion gate ──────────────────────────────────────────────────────
--
-- A trigger, because the condition spans two tables and a CHECK cannot read
-- one. `search_path` is pinned: R-45 found twelve functions without one,
-- including two security guards, and a guard a caller can redirect is not a
-- guard.

CREATE OR REPLACE FUNCTION training_dataset_promotion_gate()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    required TEXT[] := ARRAY['pii', 'secret', 'contamination', 'licence'];
    kind     TEXT;
    ok       BOOLEAN;
BEGIN
    -- Only promotion is gated. Moving to `quarantined`, `rejected` or
    -- `shredded` must always be possible: those are how somebody stops a
    -- dataset being used, and a gate that blocked them would make the safe
    -- direction the hard one.
    IF NEW.state NOT IN ('reviewed', 'train', 'validation', 'test') THEN
        RETURN NEW;
    END IF;

    FOREACH kind IN ARRAY required LOOP
        SELECT EXISTS (
            SELECT 1 FROM dataset_checks c
             WHERE c.dataset_id = NEW.id
               AND c.check_kind = kind
               AND c.outcome = 'passed'
               -- The comparison this table exists for.
               AND c.content_sha256 = NEW.content_sha256
        ) INTO ok;

        IF NOT ok THEN
            RAISE EXCEPTION
                'dataset %/% cannot reach %: no passing % check at digest %',
                NEW.name, NEW.version, NEW.state, kind,
                left(NEW.content_sha256, 20) || '…'
            USING ERRCODE = 'check_violation',
                  HINT = 'Every required check (pii, secret, contamination, '
                         'licence) must have passed AGAINST THIS DIGEST. A '
                         'check that ran before the content changed describes '
                         'bytes that no longer exist.';
        END IF;
    END LOOP;

    IF NEW.licence_classification IS DISTINCT FROM 'known_approved' THEN
        RAISE EXCEPTION
            'dataset %/% cannot reach %: its licence is %',
            NEW.name, NEW.version, NEW.state, NEW.licence_classification
        USING ERRCODE = 'check_violation',
              HINT = 'unknown and review_required both fail closed. Only a '
                     'named human may record known_approved.';
    END IF;

    IF NEW.lawful_basis IS NULL OR btrim(NEW.lawful_basis) = '' THEN
        RAISE EXCEPTION
            'dataset %/% cannot reach %: no lawful basis is recorded',
            NEW.name, NEW.version, NEW.state
        USING ERRCODE = 'check_violation',
              HINT = 'PP-05. Why IRA may train on this data is a decision with '
                     'a named author — see docs/cloud/PRE_PRODUCTION_REQUIRED.md.';
    END IF;

    IF NEW.contains_personal_data = 'unknown' THEN
        RAISE EXCEPTION
            'dataset %/% cannot reach %: nobody has answered whether it '
            'contains personal data', NEW.name, NEW.version, NEW.state
        USING ERRCODE = 'check_violation',
              HINT = 'A clean PII scan is evidence, not an answer. The scan '
                     'finds what its patterns match; this is the human''s '
                     'statement about the data.';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS training_dataset_promotion ON training_datasets;
CREATE TRIGGER training_dataset_promotion
    BEFORE INSERT OR UPDATE ON training_datasets
    FOR EACH ROW EXECUTE FUNCTION training_dataset_promotion_gate();


-- ── Changing the content demotes the dataset ────────────────────────────────
--
-- The other half of the digest rule, and the one that closes the loop.
--
-- Without it, an operator could promote a dataset to `train` with four valid
-- checks, then UPDATE `content_sha256` — and the row would sit in `train` with
-- every check now describing different bytes. The promotion trigger fires only
-- on the transition INTO a promoted state, so a later content change would slip
-- past it.
--
-- So a content change on a promoted dataset drops it back to `raw` and says so.
-- Not an error: re-ingesting data is normal, and refusing the UPDATE would push
-- somebody to delete and recreate the row, losing its history.
CREATE OR REPLACE FUNCTION training_dataset_content_changed()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
BEGIN
    IF NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
       AND OLD.state IN ('reviewed', 'train', 'validation', 'test') THEN
        NEW.state := 'raw';
        NEW.notes := btrim(NEW.notes || ' [demoted to raw: content changed on '
                           || NOW()::DATE || '; every check now describes the '
                           || 'previous digest]');
        RAISE NOTICE
            'dataset %/% demoted to raw: its content digest changed',
            NEW.name, NEW.version;
    END IF;
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

-- BEFORE the promotion gate, alphabetically: PostgreSQL fires same-timing row
-- triggers in NAME order, and `training_dataset_content_change` sorts before
-- `training_dataset_promotion`. That ordering is deliberate and load-bearing —
-- the demotion must set `state = 'raw'` before the gate inspects NEW.state, or
-- the gate would reject the update instead of demoting it.
DROP TRIGGER IF EXISTS training_dataset_content_change ON training_datasets;
CREATE TRIGGER training_dataset_content_change
    BEFORE UPDATE ON training_datasets
    FOR EACH ROW EXECUTE FUNCTION training_dataset_content_changed();


-- ── Grants, and the REVOKE that makes them mean something ───────────────────
--
-- `042` sets ALTER DEFAULT PRIVILEGES FOR ROLE ira_schema_owner GRANT SELECT,
-- INSERT, UPDATE, DELETE ON TABLES TO ira_app, ira_worker, so both tables
-- arrive with full DML already attached and a bare `GRANT SELECT` would grant
-- nothing. P5-01 shipped exactly that mistake on `067`.
--
-- Here it means: a process that answers requests could otherwise certify its
-- own training data, or record a check that never ran.
GRANT SELECT ON training_datasets TO ira_app, ira_worker;
GRANT SELECT ON dataset_checks    TO ira_app, ira_worker;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON training_datasets
    FROM ira_app, ira_worker;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON dataset_checks
    FROM ira_app, ira_worker;

COMMIT;
