-- =============================================================================
-- 074_omnivoice_card_evidence.sql — the terms the collector could not see.
--
-- A DATA migration. It creates nothing and alters nothing: it records evidence
-- that existed at the pinned revision all along and that `069` could not read.
--
-- ── What went wrong ────────────────────────────────────────────────────────
--
-- `069` recorded `k2-fsa/OmniVoice` as `unknown` — the publisher declares no
-- licence — and `MODEL_LICENCE_DETERMINATIONS_REQUIRED.md` L-03 said, in as many
-- words, *"There is nothing to read."*
--
-- There was. At the same pinned revision `c5fdb5cc…`, the model card says:
--
--     Our code is released under the Apache 2.0 License. The pre-trained model
--     is licensed under the CC-BY-NC due to constraints from its training data
--     (e.g., Emilia).
--
-- `collect_licence_evidence.py` read `cardData.license`, `license_name`,
-- `license_link` and `license:*` tags — **structured metadata only** — so a
-- licence stated in the card body was invisible to it. The register recorded
-- `unknown` while the publisher had declared terms, on the one artefact IRA
-- already fetches.
--
-- Phase 6's principle was *"absent metadata is not a permissive default."* The
-- sharper version, and the one this migration exists for: **absent metadata is
-- not absent terms.**
--
-- ── This records evidence. It determines nothing ──────────────────────────
--
-- The row moves `unknown` → `review_required`. Both are pre-determination
-- states, and the move says only *"there is now something to read"* — which is
-- what `069` did for the other fourteen rows from their licence layers.
--
-- It does NOT set `commercial_use`, `hosted_saas_redistribution`,
-- `derivative_distribution`, `classified_by` or `classified_at`. `068`'s
-- `undetermined_rows_answer_nothing` would refuse this transaction if it tried,
-- and `determination_names_a_human` would refuse an attribution to a migration.
--
-- **The direction is not softening.** `unknown` and `review_required` both fail
-- closed: a model whose row is either cannot serve, and `071`'s gate refuses to
-- train from either. What changes is that the reviewer now sees the publisher's
-- own words instead of a blank.
--
-- ── `licence_identifier` carries the WEIGHTS licence ──────────────────────
--
-- The card states two, scoped differently: Apache-2.0 for the code, CC-BY-NC
-- for the pre-trained model. `licence_identifier` describes the artefact IRA
-- would run, so it takes the weights answer. The code answer is not lost — it
-- is in `MODEL_LICENCE_EVIDENCE.json` under `card_evidence.code_licences`, with
-- both sentences quoted verbatim — and the two must not be conflated, because
-- using the code is not using the weights.
-- =============================================================================

BEGIN;

-- ── First, a method the schema does not yet know about ────────────────────
--
-- `068` constrains `evidence_method` to a fixed vocabulary — the same
-- discipline `073` applies to `manifest_method`: a method nobody implemented
-- cannot be recorded, because a digest or a quote whose derivation nothing can
-- reproduce is decoration.
--
-- Reading the model card body is a new method, and it was refused on the first
-- attempt. `068` is merged and applied, so it is not edited: **the forward
-- migration repairs the deployed state.** The constraint is replaced with one
-- that names the new method alongside the four `068` allowed, and nothing else
-- widens.
ALTER TABLE model_licence_register
    DROP CONSTRAINT IF EXISTS model_licence_register_evidence_method_check;

ALTER TABLE model_licence_register
    ADD CONSTRAINT model_licence_register_evidence_method_check CHECK (
        evidence_method IN ('huggingface-model-api',
                            'huggingface-model-card',
                            'ollama-registry-manifest',
                            'publisher-document',
                            'owner-supplied'));

UPDATE model_licence_register SET
    -- Verbatim from the card. Not normalised to an SPDX identifier: `CC-BY-NC`
    -- is what the publisher wrote, and which CC-BY-NC version applies is part
    -- of the reading, not part of the recording.
    licence_identifier    = 'CC-BY-NC',
    -- The digest of the card the sentence was read from, so the quote can be
    -- checked against the bytes it came from.
    licence_text_sha256   = 'sha256:b5d645a1874baa96a460a0e3a7d5a262811d682d04ce235878a16d5fed287fd3',
    evidence_url          = 'https://huggingface.co/k2-fsa/OmniVoice/raw/c5fdb5ccb189668d56333f77ba2629f4cd7535f4/README.md',
    evidence_method       = 'huggingface-model-card',
    evidence_retrieved_at = TIMESTAMPTZ '2026-08-09T19:40:20+00:00',
    classification        = 'review_required'
 WHERE source = 'huggingface'
   AND identifier = 'k2-fsa/OmniVoice'
   AND reference = 'c5fdb5ccb189668d56333f77ba2629f4cd7535f4'
   -- Idempotent, and refuses to touch a row somebody has since determined:
   -- overwriting a human's classification from a migration is the one thing
   -- this file must never do.
   AND classification IN ('unknown', 'review_required')
   AND classified_by IS NULL;

DO $$
DECLARE
    state TEXT;
    ident TEXT;
BEGIN
    SELECT classification, licence_identifier INTO state, ident
      FROM model_licence_register
     WHERE source = 'huggingface' AND identifier = 'k2-fsa/OmniVoice';

    -- The row must exist and must carry the evidence. A data migration that
    -- silently matched nothing is indistinguishable from one that worked, and
    -- this one exists precisely because something invisible went unnoticed.
    IF NOT FOUND THEN
        RAISE EXCEPTION '074: no k2-fsa/OmniVoice row to record evidence against';
    END IF;
    IF ident IS DISTINCT FROM 'CC-BY-NC' OR state IS DISTINCT FROM 'review_required' THEN
        RAISE EXCEPTION
            '074: the OmniVoice row reads %/% after this migration, not '
            'CC-BY-NC/review_required', ident, state;
    END IF;
END $$;

COMMIT;
