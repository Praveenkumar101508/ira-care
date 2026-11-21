-- =============================================================================
-- rollback/074_omnivoice_card_evidence_down.sql
--
-- Restores the `k2-fsa/OmniVoice` row to the state `069` left it in.
--
-- ── It REFUSES while a human has classified the row ────────────────────────
--
-- Reversing this puts `licence_identifier` back to NULL and the classification
-- back to `unknown` — which would erase a determination if one had been made
-- against the evidence this migration recorded. That is somebody's reading of
-- CC-BY-NC, and no rollback may delete it silently.
--
-- ── Reversing this makes nothing MORE permissive ───────────────────────────
--
-- `unknown` and `review_required` both fail closed: the model cannot serve
-- under either, and `071` refuses to train from either. What reversal loses is
-- the reviewer's sight of the publisher's own words — the blank comes back, and
-- the blank is what caused the problem.
--
-- So the guard here is about the RECORD, and the honest note is that re-running
-- `collect_licence_evidence.py` regenerates the evidence file, but not this
-- row: the database is where the gate reads from.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    determined INTEGER;
BEGIN
    SELECT count(*) INTO determined
      FROM model_licence_register
     WHERE source = 'huggingface' AND identifier = 'k2-fsa/OmniVoice'
       AND classified_by IS NOT NULL;

    IF determined > 0 THEN
        RAISE EXCEPTION
            '074-down REFUSING: the OmniVoice row carries a human licence '
            'determination. Reversing this migration would delete the evidence '
            'that determination was made against and put the row back to '
            '''unknown''.'
        USING HINT =
            'Withdraw the determination deliberately first: UPDATE '
            'model_licence_register SET classification = ''review_required'', '
            'classified_by = NULL, classified_at = NULL, commercial_use = '
            '''unknown'', hosted_saas_redistribution = ''unknown'', '
            'derivative_distribution = ''unknown'' WHERE identifier = '
            '''k2-fsa/OmniVoice''. Then re-run this rollback.';
    END IF;
END $$;

UPDATE model_licence_register SET
    licence_identifier    = NULL,
    licence_text_sha256   = NULL,
    evidence_url          = 'https://huggingface.co/api/models/k2-fsa/OmniVoice/revision/c5fdb5ccb189668d56333f77ba2629f4cd7535f4',
    evidence_method       = 'huggingface-model-api',
    classification        = 'unknown'
 WHERE source = 'huggingface'
   AND identifier = 'k2-fsa/OmniVoice'
   AND classified_by IS NULL;

-- The vocabulary goes back to `068`'s four. AFTER the UPDATE above, or the row
-- would still carry the method the constraint is about to forbid.
ALTER TABLE model_licence_register
    DROP CONSTRAINT IF EXISTS model_licence_register_evidence_method_check;

ALTER TABLE model_licence_register
    ADD CONSTRAINT model_licence_register_evidence_method_check CHECK (
        evidence_method IN ('huggingface-model-api',
                            'ollama-registry-manifest',
                            'publisher-document',
                            'owner-supplied'));

COMMIT;
