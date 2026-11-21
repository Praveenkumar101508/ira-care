-- =============================================================================
-- 071_model_training_runs.sql — Phase 7, batch P7-03. The gate on training.
--
-- `CLOUD_PHASE_STATUS.md` row 7: *"Proprietary model pipeline | NOT_STARTED |
-- **needs owner approval before any training**"*, and
-- `COST_AND_CAPACITY_REGISTER.md` lists under "Requires owner approval before
-- proceeding":
--
--     Renting GPU capacity              | 7
--     Starting expensive model training | 7
--
-- ── Three things must be true before a run may start ───────────────────────
--
--   1. the BASE MODEL's licence permits fine-tuning and distributing a
--      derivative;
--   2. every DATASET it consumes has passed governance, at the digest it was
--      bound at;
--   3. the OWNER has approved this specific run.
--
-- None of the three is engineering's to satisfy, and all three are enforced
-- here rather than described in a runbook.
--
-- ── (1) is a column Phase 6 already built and nothing consumed ─────────────
--
-- `MODEL_LICENSE_REGISTER.md` lists, among the fields required per row:
--
--     **Whether fine-tuning and distributing a derivative is permitted
--     (Phase 7)** — ⏸ `derivative_distribution`
--
-- `068` created that column and `069` seeded it `unknown` on all fifteen rows,
-- because `undetermined_rows_answer_nothing` forbids anything else until a
-- named human classifies the row. Phase 6 enforced `classification` for
-- SERVING; this enforces `derivative_distribution` for TRAINING. Same table,
-- same rule about who may write it, different column, different protected
-- action.
--
-- **Consequence, stated so nobody has to discover it: no training run can
-- currently be approved.** That is L-01/L-02 blocking, working as described —
-- not a defect, and not something to work around.
--
-- ── Why a bound dataset carries the digest it was bound at ─────────────────
--
-- The same reason `dataset_checks` does, one level up. `070` demotes a dataset
-- whose content changes, but a run approved yesterday holds a reference, and
-- the reference is to a NAME. Re-ingesting the data under the same name would
-- leave the run pointing at bytes nobody scanned while its binding still read
-- `train`.
--
-- So `training_run_datasets` records the digest at bind time and the gate
-- compares it against the dataset's current digest on every transition. A run
-- whose data moved underneath it cannot proceed.
--
-- ── What this table is NOT ────────────────────────────────────────────────
--
-- It is not a job queue and it does not execute anything. It records what a run
-- would be, whether it is permitted, and — once PP-04 is satisfied and a run
-- actually happens — what it emitted. Nothing in this repository can move a row
-- to `completed` with a real adapter behind it, because no GPU and no corpus
-- exist; `test_p7_training_gate.py` asserts the table holds no completed run.
--
-- REFERENCE_DATA, like `067`, `068` and `070`: no tenant column, no customer
-- data, `ira_app` reads and cannot write. A process that answers requests must
-- not be able to approve its own training run.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS model_training_runs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    run_key             TEXT NOT NULL CHECK (run_key <> ''),
    attempt             INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),

    -- ── What it would train FROM ──────────────────────────────────────────
    --
    -- The licence row, not just the name. A run that recorded `qwen3` as text
    -- would be a run whose permission nobody could check afterwards, and the
    -- foreign key is what makes `derivative_distribution` reachable from here.
    base_licence_id     UUID NOT NULL
                        REFERENCES model_licence_register(id) ON DELETE RESTRICT,

    -- Denormalised for readability in a report, and pinned. `revision` is the
    -- immutable reference — the same two shapes `068` accepts — because a run
    -- recorded against a tag describes whatever that tag pointed at that day.
    base_model          TEXT NOT NULL CHECK (base_model <> ''),
    base_revision       TEXT NOT NULL CHECK (
                            base_revision ~ '^sha256:[0-9a-f]{64}$'
                            OR base_revision ~ '^[0-9a-f]{40}$'),

    -- ── How ───────────────────────────────────────────────────────────────
    --
    -- §6: "LoRA / QLoRA training configs". `full` is present and will not be
    -- approvable in practice — it needs hardware nobody has — but excluding it
    -- from the vocabulary would mean a future full fine-tune had to change the
    -- schema to be recorded, which is how untracked runs happen.
    method              TEXT NOT NULL CHECK (method IN ('lora', 'qlora', 'full')),

    -- The config, hashed. Two runs with the same recorded config and different
    -- results are otherwise indistinguishable, and `MODEL_EVALUATION_MATRIX.md`
    -- already demands this shape of every benchmark.
    config_path         TEXT NOT NULL CHECK (config_path <> ''),
    config_sha256       TEXT NOT NULL CHECK (
                            config_sha256 ~ '^sha256:[0-9a-f]{64}$'),

    -- ── The owner's approval (PP-04) ──────────────────────────────────────
    --
    -- A person, dated, with a reason. Same deny-list as `068` and `070`: an
    -- approval attributed to `automation` is not an approval, and a pipeline
    -- that could write one would have defeated the requirement the cost
    -- register states.
    approved_by         TEXT,
    approved_at         TIMESTAMPTZ,
    approval_note       TEXT NOT NULL DEFAULT '',

    state               TEXT NOT NULL DEFAULT 'draft' CHECK (state IN (
                            'draft', 'approved', 'running', 'completed',
                            'failed', 'cancelled')),

    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,

    -- ── What it emitted ───────────────────────────────────────────────────
    --
    -- NULL until a run actually happens. `adapter_sha256` is what a model card
    -- and a registry row would point at, and a `completed` run without one is
    -- a claim that weights exist somewhere nobody can identify.
    adapter_sha256      TEXT CHECK (
                            adapter_sha256 IS NULL
                            OR adapter_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    adapter_path        TEXT,

    failure_reason      TEXT NOT NULL DEFAULT '',
    notes               TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT model_training_runs_identity UNIQUE (run_key, attempt),

    CONSTRAINT training_approval_is_attributed CHECK (
        (approved_by IS NULL AND approved_at IS NULL)
        OR (approved_by IS NOT NULL
            AND length(btrim(approved_by)) >= 3
            AND approved_at IS NOT NULL
            AND btrim(approval_note) <> ''
            AND lower(btrim(approved_by)) NOT IN (
                'migration', 'migrations', 'automation', 'automated', 'system',
                'script', 'scripts', 'collector', 'engineering', 'engineer',
                'ira', 'bot', 'robot', 'ci', 'cicd', 'pipeline', 'admin',
                'administrator', 'operator', 'root', 'postgres', 'unknown',
                'n/a', 'na', 'tbd', 'todo', 'none', 'nobody', 'anonymous',
                'default', 'test', 'placeholder', 'agent', 'assistant',
                'ira_migrator', 'ira_app', 'ira_worker', 'ira_schema_owner'))
    ),

    -- A completed run produced weights, or it did not complete. Without this a
    -- row could claim success with nothing behind it — and a model card
    -- generated from that row would describe an artefact that never existed.
    CONSTRAINT a_completed_run_emitted_an_adapter CHECK (
        state <> 'completed'
        OR (adapter_sha256 IS NOT NULL AND adapter_path IS NOT NULL
            AND finished_at IS NOT NULL)
    ),

    -- The mirror: a run that has not completed has emitted nothing. This is the
    -- shape a half-written row takes, and it is the one a model card would
    -- happily consume.
    CONSTRAINT only_a_completed_run_has_an_adapter CHECK (
        state = 'completed' OR adapter_sha256 IS NULL
    ),

    CONSTRAINT a_failed_run_says_why CHECK (
        state <> 'failed' OR btrim(failure_reason) <> ''
    )
);

COMMENT ON TABLE model_training_runs IS
    'MOD-04, Phase 7. What a training run would be, whether it is permitted, '
    'and what it emitted. Not a job queue and it executes nothing. A run needs '
    'a base licence permitting derivatives, governed datasets at their bound '
    'digests, and a named human approval (PP-04).';

COMMENT ON COLUMN model_training_runs.base_licence_id IS
    'The model_licence_register row. Recording the base model as text would '
    'make the permission uncheckable afterwards; this is what makes '
    'derivative_distribution reachable from a run.';

COMMENT ON COLUMN model_training_runs.approved_by IS
    'PP-04. A person. COST_AND_CAPACITY_REGISTER requires owner approval for '
    'GPU rental and model training; the deny-list rejects process names.';

CREATE INDEX IF NOT EXISTS model_training_runs_state_idx
    ON model_training_runs (state, run_key);


-- ── Which datasets a run consumes ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS training_run_datasets (
    run_id              UUID NOT NULL
                        REFERENCES model_training_runs(id) ON DELETE CASCADE,

    -- RESTRICT, not CASCADE. Deleting a dataset out from under a recorded run
    -- would leave the run claiming a provenance it can no longer show.
    dataset_id          UUID NOT NULL
                        REFERENCES training_datasets(id) ON DELETE RESTRICT,

    role                TEXT NOT NULL CHECK (role IN (
                            'train', 'validation', 'test')),

    -- **The digest at bind time.** `070` demotes a dataset whose content
    -- changes, but a run holds a reference by id — so re-ingesting under the
    -- same row would leave this binding pointing at bytes nobody scanned. The
    -- gate compares this against the dataset's current digest.
    bound_sha256        TEXT NOT NULL CHECK (
                            bound_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    bound_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (run_id, dataset_id)
);

COMMENT ON TABLE training_run_datasets IS
    'Which governed datasets a run consumes, and the content digest each was '
    'bound at. A run whose data moved underneath it cannot proceed.';

CREATE INDEX IF NOT EXISTS training_run_datasets_dataset_idx
    ON training_run_datasets (dataset_id);


-- ── The gate ────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION model_training_run_gate()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    licence_state  TEXT;
    derivative     TEXT;
    train_splits   INTEGER;
    drifted        RECORD;
    ungoverned     RECORD;
BEGIN
    -- `draft`, `cancelled` and `failed` are always reachable. Drafting a run
    -- and recording that it was cancelled or failed must never be blocked: a
    -- gate on those would make "delete the row" the easy path and lose the
    -- record that the run was ever contemplated.
    IF NEW.state NOT IN ('approved', 'running', 'completed') THEN
        RETURN NEW;
    END IF;

    -- ── (1) The base model's licence must permit a derivative ─────────────
    SELECT classification, derivative_distribution
      INTO licence_state, derivative
      FROM model_licence_register WHERE id = NEW.base_licence_id;

    -- NULL when unreadable. The foreign key prevents a missing row, but a guard
    -- that treats "I could not check" as a pass is the failure P5-02 recorded,
    -- so it is refused explicitly rather than left to the FK.
    IF licence_state IS DISTINCT FROM 'known_approved' THEN
        RAISE EXCEPTION
            'training run %/% cannot reach %: the base model''s licence is %',
            NEW.run_key, NEW.attempt, NEW.state,
            COALESCE(licence_state, 'unreadable')
        USING ERRCODE = 'check_violation',
              HINT = 'Only a named human may record known_approved — see '
                     'docs/cloud/MODEL_LICENCE_DETERMINATIONS_REQUIRED.md.';
    END IF;

    IF derivative IS DISTINCT FROM 'permitted' THEN
        RAISE EXCEPTION
            'training run %/% cannot reach %: derivative distribution is % for %',
            NEW.run_key, NEW.attempt, NEW.state,
            COALESCE(derivative, 'unreadable'), NEW.base_model
        USING ERRCODE = 'check_violation',
              HINT = 'MODEL_LICENSE_REGISTER.md: "Whether fine-tuning and '
                     'distributing a derivative is permitted (Phase 7)". A '
                     'licence approved for SERVING is not thereby approved for '
                     'TRAINING — L-01 and L-02.';
    END IF;

    -- ── (2) Every dataset must be governed, at the digest it was bound at ──
    SELECT count(*) INTO train_splits
      FROM training_run_datasets WHERE run_id = NEW.id AND role = 'train';

    IF train_splits = 0 THEN
        RAISE EXCEPTION
            'training run %/% cannot reach %: no training dataset is bound',
            NEW.run_key, NEW.attempt, NEW.state
        USING ERRCODE = 'check_violation',
              HINT = 'A run with no data is a run whose provenance is empty.';
    END IF;

    SELECT d.name, d.version, d.state INTO ungoverned
      FROM training_run_datasets b
      JOIN training_datasets d ON d.id = b.dataset_id
     WHERE b.run_id = NEW.id
       AND d.state NOT IN ('train', 'validation', 'test')
     LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION
            'training run %/% cannot reach %: dataset %/% is %',
            NEW.run_key, NEW.attempt, NEW.state,
            ungoverned.name, ungoverned.version, ungoverned.state
        USING ERRCODE = 'check_violation',
              HINT = 'Only a dataset that passed every check at its current '
                     'digest, with an approved licence and a recorded lawful '
                     'basis, reaches a usable split — see 070.';
    END IF;

    SELECT d.name, d.version INTO drifted
      FROM training_run_datasets b
      JOIN training_datasets d ON d.id = b.dataset_id
     WHERE b.run_id = NEW.id
       AND d.content_sha256 IS DISTINCT FROM b.bound_sha256
     LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION
            'training run %/% cannot reach %: dataset %/% changed after it was '
            'bound', NEW.run_key, NEW.attempt, NEW.state,
            drifted.name, drifted.version
        USING ERRCODE = 'check_violation',
              HINT = 'The run would consume bytes nobody scanned for it. '
                     'Re-bind the dataset at its current digest, which records '
                     'that the run is about different data.';
    END IF;

    -- ── (3) The owner's approval ──────────────────────────────────────────
    IF NEW.approved_by IS NULL OR NEW.approved_at IS NULL THEN
        RAISE EXCEPTION
            'training run %/% cannot reach %: no owner approval is recorded',
            NEW.run_key, NEW.attempt, NEW.state
        USING ERRCODE = 'check_violation',
              HINT = 'PP-04. CLOUD_PHASE_STATUS.md row 7 requires owner '
                     'approval before any training, and '
                     'COST_AND_CAPACITY_REGISTER.md requires it for GPU '
                     'capacity and for starting a run.';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS model_training_run_gate_trigger ON model_training_runs;
CREATE TRIGGER model_training_run_gate_trigger
    BEFORE INSERT OR UPDATE ON model_training_runs
    FOR EACH ROW EXECUTE FUNCTION model_training_run_gate();


-- ── A binding cannot be added to a run that has left draft ──────────────────
--
-- Otherwise the gate is trivially defeated: approve a run with one clean
-- dataset, then bind three more. The gate fires on `model_training_runs`, not
-- on the binding table, so nothing would re-check.
CREATE OR REPLACE FUNCTION training_run_binding_gate()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    run_state TEXT;
    key       TEXT;
BEGIN
    SELECT state, run_key INTO run_state, key
      FROM model_training_runs WHERE id = COALESCE(NEW.run_id, OLD.run_id);

    -- ── The run is gone: this is the parent's ON DELETE CASCADE ───────────
    --
    -- Found by a test teardown, and it was a real defect rather than a test
    -- problem. `training_run_datasets.run_id` is ON DELETE CASCADE, so deleting
    -- a run deletes its bindings — and this trigger fired on those deletes,
    -- read a parent row that no longer existed, and refused. **A run with any
    -- binding could never be deleted at all.**
    --
    -- Permitting a not-found parent is the correct direction and is safe
    -- because of the foreign key, not in spite of it: a binding cannot exist
    -- without its run, so the only way to reach this branch is the cascade. The
    -- control being protected is a change of bindings on a LIVE run, and a run
    -- that no longer exists is not one.
    IF NOT FOUND THEN
        RETURN COALESCE(NEW, OLD);
    END IF;

    IF run_state IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION
            'training run % is %, so its datasets cannot change',
            COALESCE(key, '?'), COALESCE(run_state, 'unreadable')
        USING ERRCODE = 'check_violation',
              HINT = 'Bindings are fixed when a run leaves draft. Otherwise an '
                     'approved run could take on data nobody approved. Record a '
                     'new attempt instead.';
    END IF;

    RETURN COALESCE(NEW, OLD);
END;
$$;

DROP TRIGGER IF EXISTS training_run_binding_gate_trigger ON training_run_datasets;
CREATE TRIGGER training_run_binding_gate_trigger
    BEFORE INSERT OR UPDATE OR DELETE ON training_run_datasets
    FOR EACH ROW EXECUTE FUNCTION training_run_binding_gate();


-- ── Grants, and the REVOKE that makes them mean something ───────────────────
--
-- `042`'s ALTER DEFAULT PRIVILEGES would otherwise give both runtime roles full
-- DML — the P5-01 finding. Here it would mean a request-handling process could
-- approve its own training run.
GRANT SELECT ON model_training_runs    TO ira_app, ira_worker;
GRANT SELECT ON training_run_datasets  TO ira_app, ira_worker;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON model_training_runs
    FROM ira_app, ira_worker;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON training_run_datasets
    FROM ira_app, ira_worker;

COMMIT;
