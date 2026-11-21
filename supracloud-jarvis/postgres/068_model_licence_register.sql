-- =============================================================================
-- 068_model_licence_register.sql — Phase 6, batch P6-01.
--
-- Closes the ENGINEERING half of MOD-01. The other half is a legal reading, and
-- this migration's job is to make sure nothing can be mistaken for one.
--
-- `BASELINE_MODEL_INVENTORY.md` §7:
--
--   FINDING MOD-01 — the licence position of every configured model is
--   unrecorded. … For a paid subscription product, commercial permission has to
--   be established before a model can serve a paying tenant.
--
-- ── What was actually wrong, and it is not "the field was empty" ───────────
--
-- `067` already refuses to let an unlicensed version serve:
--
--   CONSTRAINT serving_is_licensed CHECK (
--       status <> 'serving'
--       OR (commercial_licence IS NOT NULL AND licence_recorded_at IS NOT NULL))
--
-- and `IMPLEMENTED_VS_PLANNED.md` describes that as *"the field is a gate, not
-- an answer"*. It is not a gate. `commercial_licence` is free text, so:
--
--   UPDATE model_registry SET status = 'serving',
--          commercial_licence = 'x', licence_recorded_at = NOW();   -- succeeds
--
-- A constraint that any operator satisfies by typing a character is a prompt.
-- Worse, it is a prompt that LOOKS like a control, which is how it survived a
-- phase: the schema reads as though a licence had been established, and nothing
-- distinguishes a legal determination from a placeholder.
--
-- ── The line this table draws ──────────────────────────────────────────────
--
--   Engineering may record what a publisher SAYS.
--   Only a named human may record what it MEANS for IRA.
--
-- So the columns are in two groups, and the constraints enforce which group a
-- writer is allowed to touch:
--
--   EVIDENCE       the licence identifier as published; the sha256 of the
--                  licence text; the artefact digest it belongs to; the URL;
--                  the method; the time it was retrieved.
--                  Written by `scripts/models/collect_licence_evidence.py`.
--
--   DETERMINATION  commercial_use, hosted_saas_redistribution,
--                  attribution_required, output_restrictions,
--                  derivative_distribution, and the classification itself.
--                  Written by a person, whose name and date are stored beside
--                  the answer.
--
-- Four classification states, and only the first two may be written by anything
-- automatic:
--
--   unknown          no licence metadata exists at the source. Fails closed.
--   review_required  evidence was collected; nobody has read it yet. Fails closed.
--   known_restricted a human read it and it does not permit IRA's use.
--   known_approved   a human read it and it does.
--
-- **No code path in this repository writes either of the last two.** The
-- `determination_names_a_human` constraint rejects `classified_by` values of
-- `migration`, `automation`, `system`, `script`, `collector`, and the rest of
-- the process vocabulary, so a future migration that tried to "seed" an
-- approval fails at apply time rather than shipping one.
--
-- ── Why an approval is bound to a DIGEST and not to a tag ──────────────────
--
-- `qwen3:14b` is a moving target: the publisher can push new weights to that
-- tag tomorrow. An approval recorded against the tag would silently transfer to
-- an artefact nobody read the licence of — the same substitution shape MOD-02
-- describes, arriving through the licence door instead of the fetch door. So
-- `approval_is_bound_to_an_immutable_artefact` requires `artifact_digest`, and
-- the evidence columns freeze once a row is approved.
--
-- ── Classification: REFERENCE_DATA, no RLS ─────────────────────────────────
--
-- Same class as `model_registry` (067) and `model_providers` (029):
-- platform-authored operator configuration, no tenant column, no customer data.
-- `ira_app` reads and cannot write, for the reason 067 records — a process that
-- answers requests must not be able to approve its own licence.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS model_licence_register (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- ── What this licence position is ABOUT ────────────────────────────────

    -- Same vocabulary as `model_pull_allowlist`: a source nobody wrote a
    -- collector for cannot be recorded into existence.
    source              TEXT NOT NULL CHECK (source IN ('huggingface', 'ollama')),

    -- The upstream identifier, e.g. 'BAAI/bge-m3' or 'qwen3'.
    identifier          TEXT NOT NULL CHECK (identifier <> ''),

    -- The tag or revision the evidence was taken AT. Deliberately NOT required
    -- to be pinned, unlike `model_registry.revision`: for Ollama the reference
    -- IS a mutable tag ('14b'), and pretending otherwise would force the
    -- collector to lie. `artifact_digest` below is what pins it, and that is
    -- what an approval is bound to.
    reference           TEXT NOT NULL CHECK (reference <> ''),

    -- ── EVIDENCE — engineering may write these ─────────────────────────────

    -- Verbatim from the source. `apache-2.0` from a Hugging Face card;
    -- `Apache License` — the first line of the licence layer — from an Ollama
    -- manifest. NOT normalised, NOT mapped to SPDX, NOT inferred: normalising
    -- `Gemma Terms of Use` into a licence identifier would be the whole defect.
    --
    -- NULL means the source declared NOTHING, which is a real and important
    -- state: `k2-fsa/OmniVoice` is in it, and IRA already fetches that artefact.
    licence_identifier  TEXT,

    -- The licence text itself, hashed. An identifier can stay the same while the
    -- text changes underneath it; this is what makes that detectable.
    licence_text_sha256 TEXT CHECK (
        licence_text_sha256 IS NULL
        OR licence_text_sha256 ~ '^sha256:[0-9a-f]{64}$'),

    -- The artefact the licence was shipped with, named immutably. What an
    -- approval is bound to.
    --
    -- Two shapes, because the two sources pin differently and forcing one of
    -- them into the other's format would mean inventing a value:
    --
    --   sha256:<64 hex>  an Ollama `vnd.ollama.image.model` layer digest, which
    --                    the registry serves without downloading the weights
    --   <40 hex>         a Hugging Face commit sha
    --
    -- A commit sha pins content exactly as immutably as a content digest does —
    -- it IS a hash of the tree — so both satisfy the property this column
    -- exists for. What neither shape admits is a TAG: `14b`, `main` and
    -- `latest` match nothing here, which is the point.
    artifact_digest     TEXT,

    evidence_url        TEXT NOT NULL CHECK (evidence_url <> ''),
    evidence_method     TEXT NOT NULL CHECK (evidence_method IN (
                            'huggingface-model-api',
                            'ollama-registry-manifest',
                            'publisher-document',
                            'owner-supplied')),
    evidence_retrieved_at TIMESTAMPTZ NOT NULL,

    -- ── DETERMINATION — only a named human may write these ─────────────────

    classification      TEXT NOT NULL DEFAULT 'unknown' CHECK (
                            classification IN ('unknown', 'review_required',
                                               'known_restricted', 'known_approved')),

    -- The questions `MODEL_LICENSE_REGISTER.md` requires answered per row before
    -- a model may serve a paying tenant. Every one defaults to 'unknown', and
    -- 'unknown' is a state, not a placeholder for 'probably fine'.
    commercial_use              TEXT NOT NULL DEFAULT 'unknown' CHECK (
        commercial_use IN ('unknown', 'permitted', 'restricted', 'prohibited')),
    hosted_saas_redistribution  TEXT NOT NULL DEFAULT 'unknown' CHECK (
        hosted_saas_redistribution IN ('unknown', 'permitted', 'restricted', 'prohibited')),
    derivative_distribution     TEXT NOT NULL DEFAULT 'unknown' CHECK (
        derivative_distribution IN ('unknown', 'permitted', 'restricted', 'prohibited')),
    attribution_required        TEXT NOT NULL DEFAULT 'unknown' CHECK (
        attribution_required IN ('unknown', 'yes', 'no')),
    output_restrictions         TEXT NOT NULL DEFAULT 'unknown' CHECK (
        output_restrictions IN ('unknown', 'yes', 'no')),

    -- Who read the licence, and when. A person. See the constraint.
    classified_by       TEXT,
    classified_at       TIMESTAMPTZ,

    -- What they read, in their words. An approval with no reasoning is
    -- indistinguishable from a mistake, and cannot be re-checked later.
    determination_note  TEXT NOT NULL DEFAULT '',

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT model_licence_register_identity
        UNIQUE (source, identifier, reference),

    CONSTRAINT digest_is_an_immutable_reference CHECK (
        artifact_digest IS NULL
        OR artifact_digest ~ '^sha256:[0-9a-f]{64}$'   -- Ollama layer digest
        OR artifact_digest ~ '^[0-9a-f]{40}$'),        -- Hugging Face commit sha

    -- ── The states engineering may write carry NO answers ──────────────────
    --
    -- This is the "do not convert unknown into approved" rule, made
    -- unrepresentable rather than documented. A collector that filled in
    -- `commercial_use = 'permitted'` while leaving the row `review_required`
    -- would have made the determination in everything but name, and a reader
    -- scanning the column would never notice the classification disagreed.
    CONSTRAINT undetermined_rows_answer_nothing CHECK (
        classification NOT IN ('unknown', 'review_required')
        OR (commercial_use = 'unknown'
            AND hosted_saas_redistribution = 'unknown'
            AND derivative_distribution = 'unknown'
            AND attribution_required = 'unknown'
            AND output_restrictions = 'unknown'
            AND classified_by IS NULL
            AND classified_at IS NULL)
    ),

    -- ── A determination names a human, dates itself, and gives a reason ────
    CONSTRAINT determination_is_attributed CHECK (
        classification NOT IN ('known_approved', 'known_restricted')
        OR (classified_by IS NOT NULL
            AND btrim(classified_by) <> ''
            AND classified_at IS NOT NULL
            AND btrim(determination_note) <> '')
    ),

    -- The list is the point of the whole migration.
    --
    -- `OPEN_DECISIONS.md` D-08 asks the owner to "confirm who verifies licence
    -- terms and records them". Until that person exists, nothing may occupy the
    -- field on their behalf — and the values most likely to be typed there by
    -- something automating its way past this table are exactly these.
    CONSTRAINT determination_names_a_human CHECK (
        classification NOT IN ('known_approved', 'known_restricted')
        OR (length(btrim(classified_by)) >= 3
            AND lower(btrim(classified_by)) NOT IN (
                'migration', 'migrations', 'automation', 'automated', 'system',
                'script', 'scripts', 'collector', 'engineering', 'engineer',
                'ira', 'bot', 'robot', 'ci', 'cicd', 'pipeline', 'admin',
                'administrator', 'operator', 'root', 'postgres', 'unknown',
                'n/a', 'na', 'tbd', 'todo', 'none', 'nobody', 'anonymous',
                'default', 'test', 'placeholder', 'agent', 'assistant',
                'ira_migrator', 'ira_app', 'ira_worker', 'ira_schema_owner'))
    ),

    -- ── An approval rests on evidence that exists ──────────────────────────
    --
    -- `k2-fsa/OmniVoice` has neither an identifier nor a licence text, because
    -- its publisher declares none. This constraint means the only way to
    -- approve it is to first RECORD terms obtained from the publisher — which
    -- is option 1 of L-03 and a real act — rather than to change a word in a
    -- column.
    CONSTRAINT approval_rests_on_evidence CHECK (
        classification <> 'known_approved'
        OR licence_identifier IS NOT NULL
        OR licence_text_sha256 IS NOT NULL
    ),

    -- ── An approval is bound to one immutable artefact ─────────────────────
    CONSTRAINT approval_is_bound_to_an_immutable_artefact CHECK (
        classification <> 'known_approved' OR artifact_digest IS NOT NULL
    ),

    -- ── An approval answers the two questions that matter for a paid SaaS ──
    --
    -- `MODEL_LICENSE_REGISTER.md`: "'Open weights' and 'permitted for
    -- commercial SaaS' are different questions, and only the second one matters
    -- for a paid subscription product." An approval that leaves either at
    -- 'unknown' has not answered it.
    CONSTRAINT approval_answers_the_commercial_questions CHECK (
        classification <> 'known_approved'
        OR (commercial_use = 'permitted'
            AND hosted_saas_redistribution = 'permitted')
    )
);

COMMENT ON TABLE model_licence_register IS
    'MOD-01, Phase 6. Licence EVIDENCE (what a publisher published, with '
    'provenance) separated from licence DETERMINATION (what it permits IRA, '
    'recorded by a named human). Nothing automatic may write known_approved or '
    'known_restricted. Platform-authored: ira_app reads and cannot write.';

COMMENT ON COLUMN model_licence_register.licence_identifier IS
    'Verbatim from the source. Never normalised, mapped to SPDX, or inferred. '
    'NULL means the source declared no licence — a real state, and one an '
    'artefact IRA already fetches is in.';

COMMENT ON COLUMN model_licence_register.artifact_digest IS
    'The artefact the licence shipped with. An approval binds to this rather '
    'than to a tag, because a tag can be repointed at weights nobody read the '
    'licence of.';

COMMENT ON COLUMN model_licence_register.classified_by IS
    'A person. OPEN_DECISIONS.md D-08 is the decision that names them. The '
    'determination_names_a_human constraint rejects process names.';

CREATE INDEX IF NOT EXISTS model_licence_register_lookup_idx
    ON model_licence_register (source, identifier);

CREATE INDEX IF NOT EXISTS model_licence_register_approved_idx
    ON model_licence_register (id)
    WHERE classification = 'known_approved';


-- ── The gate on serving ─────────────────────────────────────────────────────
--
-- `067` left `commercial_licence` as free text. It stays, because an applied
-- migration is immutable and because it now carries a different meaning: a
-- human-readable note beside the row that decides. `licence_id` is what
-- decides.

ALTER TABLE model_registry
    ADD COLUMN IF NOT EXISTS licence_id UUID
        REFERENCES model_licence_register(id) ON DELETE RESTRICT;

COMMENT ON COLUMN model_registry.licence_id IS
    'MOD-01. The licence register row this version serves under. A serving row '
    'must reference one whose classification is known_approved — enforced by '
    'trigger, because a CHECK cannot read another table. Supersedes the free-'
    'text commercial_licence from 067, which 068 leaves in place as a note.';

CREATE INDEX IF NOT EXISTS model_registry_licence_idx
    ON model_registry (licence_id) WHERE licence_id IS NOT NULL;


-- A trigger rather than a CHECK, because the condition lives in another table.
-- `search_path` is pinned: R-45 found twelve functions without one, including
-- two security guards, and a guard that can be redirected by a caller's
-- search_path is not a guard.
CREATE OR REPLACE FUNCTION model_registry_requires_approved_licence()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    state TEXT;
BEGIN
    IF NEW.status <> 'serving' THEN
        RETURN NEW;
    END IF;

    IF NEW.licence_id IS NULL THEN
        RAISE EXCEPTION
            'model_registry %/% cannot serve: no licence register row',
            NEW.model_key, NEW.version
        USING ERRCODE = 'check_violation',
              HINT = 'Set licence_id to a model_licence_register row whose '
                     'classification is known_approved. Recording an approval '
                     'is a named human''s act — see '
                     'docs/cloud/MODEL_LICENCE_DETERMINATIONS_REQUIRED.md.';
    END IF;

    SELECT classification INTO state
      FROM model_licence_register WHERE id = NEW.licence_id;

    -- NULL when the row does not exist. The foreign key already prevents that,
    -- but a guard that treats "I could not read it" as a pass is the failure
    -- P5-02 recorded for the registry itself, so it is refused explicitly.
    IF state IS DISTINCT FROM 'known_approved' THEN
        RAISE EXCEPTION
            'model_registry %/% cannot serve: its licence is %',
            NEW.model_key, NEW.version, COALESCE(state, 'unreadable')
        USING ERRCODE = 'check_violation',
              HINT = 'unknown and review_required both fail closed. Only a '
                     'named human may record known_approved.';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS model_registry_licence_gate ON model_registry;
CREATE TRIGGER model_registry_licence_gate
    BEFORE INSERT OR UPDATE ON model_registry
    FOR EACH ROW EXECUTE FUNCTION model_registry_requires_approved_licence();


-- ── Withdrawing an approval stops what it was approving ─────────────────────
--
-- The direction matters and it is easy to get backwards. Refusing the
-- withdrawal while a model serves would keep the model serving under an
-- approval somebody is actively trying to take back — precisely the wrong way
-- round. So a withdrawal retires the versions that relied on it, and writes the
-- reason into `notes` so the status change is not silent.
--
-- The evidence columns freeze while a row is approved, for the same reason the
-- approval binds to a digest: an approval that can be re-pointed at different
-- evidence is not evidence of anything.
CREATE OR REPLACE FUNCTION model_licence_register_withdrawal()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
    stopped INTEGER;
BEGIN
    IF OLD.classification = 'known_approved' THEN
        IF NEW.classification = 'known_approved'
           AND (NEW.artifact_digest     IS DISTINCT FROM OLD.artifact_digest
             OR NEW.licence_identifier  IS DISTINCT FROM OLD.licence_identifier
             OR NEW.licence_text_sha256 IS DISTINCT FROM OLD.licence_text_sha256
             OR NEW.identifier          IS DISTINCT FROM OLD.identifier
             OR NEW.source              IS DISTINCT FROM OLD.source) THEN
            RAISE EXCEPTION
                'model_licence_register %: evidence cannot change while the '
                'row is approved', OLD.id
            USING ERRCODE = 'check_violation',
                  HINT = 'Withdraw the approval first. An approval that can be '
                         're-pointed at different evidence approves nothing.';
        END IF;

        IF NEW.classification <> 'known_approved' THEN
            UPDATE model_registry
               SET status = 'retired',
                   updated_at = NOW(),
                   notes = btrim(notes || ' [retired: licence approval '
                                 || 'withdrawn on ' || NOW()::DATE || ']')
             WHERE licence_id = NEW.id AND status = 'serving';
            GET DIAGNOSTICS stopped = ROW_COUNT;
            IF stopped > 0 THEN
                RAISE NOTICE
                    'licence approval withdrawn: % model version(s) retired',
                    stopped;
            END IF;
        END IF;
    END IF;

    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS model_licence_register_withdrawal_gate
    ON model_licence_register;
CREATE TRIGGER model_licence_register_withdrawal_gate
    BEFORE UPDATE ON model_licence_register
    FOR EACH ROW EXECUTE FUNCTION model_licence_register_withdrawal();


-- ── Grants, and the REVOKE that makes them mean something ───────────────────
--
-- `042` sets ALTER DEFAULT PRIVILEGES FOR ROLE ira_schema_owner GRANT SELECT,
-- INSERT, UPDATE, DELETE ON TABLES TO ira_app, ira_worker — so this table
-- arrives with full DML for both runtime roles already attached, and a bare
-- `GRANT SELECT` would grant nothing and revoke nothing. P5-01 shipped exactly
-- that mistake on `067` and it is the reason these two lines exist.
--
-- Here it matters more than it did there: a runtime role that can UPDATE this
-- table can approve its own licence.
GRANT SELECT ON model_licence_register TO ira_app, ira_worker;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON model_licence_register
    FROM ira_app, ira_worker;

COMMIT;
