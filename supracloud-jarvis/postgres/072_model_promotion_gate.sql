-- =============================================================================
-- 072_model_promotion_gate.sql — Phase 7, batch P7-05. Promotion gates.
--
-- `BASELINE_MODEL_INVENTORY.md` §6 lists *"Model cards"* and *"Promotion
-- gates"* as absent. This is both, and they are one mechanism: a model card is
-- the document that says what an artefact is, and a promotion gate is the thing
-- that refuses to register an artefact that cannot produce one.
--
-- ── The column that reads like provenance and is not ───────────────────────
--
-- `067` created `model_registry.adapter_version` and said so plainly:
--
--     'Phase 7 (MOD-04). Present so a fine-tuned model needs no schema change;
--      nothing populates it today and no IRA-specific model exists.'
--
-- It is free text. Left as it stands, registering "an IRA model" is **typing a
-- string** — and a registry row reading `adapter_version = 'v1'` would look
-- exactly like a fine-tuned model that IRA actually produced.
--
-- That is `commercial_licence` again, one phase later. Phase 6 found a column
-- that read like a control and was satisfied by `'x'`; this is a column that
-- reads like provenance and is satisfied by `'v1'`. The fix is the same shape:
-- make the claim point at the record that supports it.
--
-- ── What that means concretely ─────────────────────────────────────────────
--
-- `training_run_id` is added, and `adapter_version` may not be set without it.
-- The gate then requires, at the moment of writing:
--
--   1. the run is `completed` — it actually produced weights;
--   2. `artifact_hash` **equals the adapter digest that run emitted**. This is
--      the load-bearing one: a registry row may point at any artefact it likes,
--      and this is what makes it point at the one the run made;
--   3. `base_model` and `revision` agree with the run's. Otherwise the registry
--      says the model is built on qwen3 while the run used gemma, and both rows
--      look internally consistent;
--   4. the base licence is STILL `known_approved` and its
--      `derivative_distribution` is STILL `permitted`. Registering is a later
--      act than training and permission expires — `068`'s withdrawal trigger
--      already retires serving versions, and this refuses the registration that
--      would re-create one;
--   5. a model card digest is recorded, and the card is generated from the run.
--
-- ── The ladder ─────────────────────────────────────────────────────────────
--
-- `067`'s own comment: *"promoting a model is registering it in the next one,
-- deliberately"*. Nothing enforced it. An IRA-trained adapter must now appear in
-- `staging` before `production`, and in `development` before `staging`, matched
-- on `artifact_hash` — the same bytes, not merely the same version string.
--
-- **Scope, stated rather than assumed:** the ladder binds rows that carry a
-- `training_run_id` and no others. A base model can still be registered
-- straight into production, which is what Phase 5 permitted; widening it to
-- every row changes Phase 5's contract and is a decision somebody should make
-- on its own terms, not a side effect of Phase 7. Recorded in
-- `PHASE_7_LEDGER.md` and in the closure report's limitations.
--
-- ── Nothing can satisfy this today ─────────────────────────────────────────
--
-- `model_training_runs` holds no `completed` row and cannot: every path to one
-- goes through a licence nobody has classified (L-01, L-02) and a corpus that
-- does not exist (PP-05). So no adapter can be registered, which is the honest
-- output of this phase and not a defect.
--
-- REFERENCE_DATA. No new tables; `ira_app` reads `model_registry` and cannot
-- write it, which `067` already established and this does not change.
-- =============================================================================

BEGIN;

ALTER TABLE model_registry
    ADD COLUMN IF NOT EXISTS training_run_id UUID
        REFERENCES model_training_runs(id) ON DELETE RESTRICT;

-- The digest of the card generated for this row. The card lives in
-- `model-platform/cards/` and is generated from the run; recording its digest
-- here is what stops the file and the row drifting apart, the same discipline
-- `config_sha256` applies to a training config.
ALTER TABLE model_registry
    ADD COLUMN IF NOT EXISTS model_card_sha256 TEXT;

COMMENT ON COLUMN model_registry.training_run_id IS
    'Phase 7 (MOD-04). The completed model_training_runs row that produced this '
    'artefact. adapter_version cannot be set without it: otherwise registering '
    'an IRA model is typing a string.';

COMMENT ON COLUMN model_registry.model_card_sha256 IS
    'Digest of the generated model card. A card is emitted from the run record, '
    'never hand-written — a hand-written card is a document that agrees with '
    'itself.';

DO $$
BEGIN
    -- Both directions. An `adapter_version` without a run is the claim with no
    -- record; a run without an `adapter_version` is a registry row that cannot
    -- be referred to by the name the config gave the artefact.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'an_ira_model_names_the_run_that_made_it') THEN
        ALTER TABLE model_registry ADD CONSTRAINT
            an_ira_model_names_the_run_that_made_it CHECK (
                (adapter_version IS NULL) = (training_run_id IS NULL));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'model_card_digest_is_a_sha256') THEN
        ALTER TABLE model_registry ADD CONSTRAINT
            model_card_digest_is_a_sha256 CHECK (
                model_card_sha256 IS NULL
                OR model_card_sha256 ~ '^sha256:[0-9a-f]{64}$');
    END IF;

    -- A base model registered from upstream has no card to generate — there is
    -- no run behind it. An IRA-trained adapter always does.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'an_ira_model_carries_its_card') THEN
        ALTER TABLE model_registry ADD CONSTRAINT
            an_ira_model_carries_its_card CHECK (
                training_run_id IS NULL OR model_card_sha256 IS NOT NULL);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS model_registry_training_run_idx
    ON model_registry (training_run_id) WHERE training_run_id IS NOT NULL;


-- ── The gate ────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION model_registry_promotion_gate()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    run          RECORD;
    previous     TEXT;
    seen         INTEGER;
BEGIN
    -- A row with no run is a base model registered from upstream, which `067`
    -- and `068` already govern. Phase 7 adds nothing to it and must not: a gate
    -- that quietly changed the rules for every existing row would be a Phase 5
    -- decision taken by a Phase 7 migration.
    IF NEW.training_run_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT r.state, r.adapter_sha256, r.base_model, r.base_revision,
           r.run_key, r.attempt,
           l.classification, l.derivative_distribution
      INTO run
      FROM model_training_runs r
      JOIN model_licence_register l ON l.id = r.base_licence_id
     WHERE r.id = NEW.training_run_id;

    -- NULL when unreadable. The foreign key prevents a missing run, but a guard
    -- that treats "I could not check" as a pass is the failure P5-02 recorded.
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'model_registry %/% names a training run that cannot be read',
            NEW.model_key, NEW.version
        USING ERRCODE = 'check_violation';
    END IF;

    IF run.state <> 'completed' THEN
        RAISE EXCEPTION
            'model_registry %/% cannot be registered: training run %/% is %, '
            'not completed', NEW.model_key, NEW.version, run.run_key,
            run.attempt, run.state
        USING ERRCODE = 'check_violation',
              HINT = 'Only a completed run produced weights. Registering from '
                     'any other state describes an artefact that does not '
                     'exist.';
    END IF;

    -- **The load-bearing comparison.** Without it the registry may point at any
    -- artefact while naming a run that produced a different one, and both rows
    -- read as consistent.
    IF NEW.artifact_hash IS DISTINCT FROM run.adapter_sha256 THEN
        RAISE EXCEPTION
            'model_registry %/% claims artefact % and training run %/% emitted '
            '%', NEW.model_key, NEW.version, NEW.artifact_hash, run.run_key,
            run.attempt, COALESCE(run.adapter_sha256, 'nothing')
        USING ERRCODE = 'check_violation',
              HINT = 'The registry entry has to point at the bytes the run '
                     'made. A different digest is a different artefact.';
    END IF;

    IF NEW.base_model IS DISTINCT FROM run.base_model
       OR NEW.revision IS DISTINCT FROM run.base_revision THEN
        RAISE EXCEPTION
            'model_registry %/% records base %@% and training run %/% used %@%',
            NEW.model_key, NEW.version, NEW.base_model, NEW.revision,
            run.run_key, run.attempt, run.base_model, run.base_revision
        USING ERRCODE = 'check_violation',
              HINT = 'Both rows would look internally consistent while '
                     'disagreeing about what the model is built on.';
    END IF;

    -- Permission expires, and registering is a later act than training.
    IF run.classification IS DISTINCT FROM 'known_approved' THEN
        RAISE EXCEPTION
            'model_registry %/% cannot be registered: the base licence for % is '
            'now %', NEW.model_key, NEW.version, run.base_model,
            COALESCE(run.classification, 'unreadable')
        USING ERRCODE = 'check_violation';
    END IF;

    IF run.derivative_distribution IS DISTINCT FROM 'permitted' THEN
        RAISE EXCEPTION
            'model_registry %/% cannot be registered: derivative distribution '
            'for % is now %', NEW.model_key, NEW.version, run.base_model,
            COALESCE(run.derivative_distribution, 'unreadable')
        USING ERRCODE = 'check_violation',
              HINT = 'Serving a derivative to tenants is distributing it. '
                     '068''s withdrawal trigger retires serving versions; this '
                     'refuses the registration that would create a new one.';
    END IF;

    -- ── The ladder ────────────────────────────────────────────────────────
    --
    -- Matched on artifact_hash rather than version: the point is that the same
    -- BYTES were registered in the previous environment, not that somebody
    -- reused a version string.
    previous := CASE NEW.environment
                    WHEN 'production' THEN 'staging'
                    WHEN 'staging'    THEN 'development'
                    ELSE NULL
                END;

    IF previous IS NOT NULL THEN
        SELECT count(*) INTO seen
          FROM model_registry m
         WHERE m.model_key = NEW.model_key
           AND m.artifact_hash = NEW.artifact_hash
           AND m.environment = previous
           AND m.id IS DISTINCT FROM NEW.id;

        IF seen = 0 THEN
            RAISE EXCEPTION
                'model_registry %/% cannot enter %: this artefact has not been '
                'registered in %', NEW.model_key, NEW.version, NEW.environment,
                previous
            USING ERRCODE = 'check_violation',
                  HINT = '067: "promoting a model is registering it in the next '
                         'one, deliberately". Register the same artifact_hash '
                         'in the previous environment first.';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION model_registry_promotion_gate() IS
    'Phase 7 (P7-05). An IRA-trained adapter may be registered only against a '
    'completed run whose adapter digest it carries, whose base it agrees with, '
    'whose licence still permits a derivative, and only one environment at a '
    'time.';

-- Fires after `model_registry_licence_gate` (068): PostgreSQL runs same-timing
-- row triggers in NAME order, and `model_registry_licence_gate` sorts before
-- `model_registry_promotion_gate`. Either order is correct — both refuse — but
-- the licence message is the more useful first one for an operator, so the
-- ordering is recorded rather than left to be discovered.
DROP TRIGGER IF EXISTS model_registry_promotion_gate ON model_registry;
CREATE TRIGGER model_registry_promotion_gate
    BEFORE INSERT OR UPDATE ON model_registry
    FOR EACH ROW EXECUTE FUNCTION model_registry_promotion_gate();

COMMIT;
