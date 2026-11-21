-- =============================================================================
-- rollback/098_memory_and_speaker_model_pins_down.sql
--
-- Removes the three artefacts `098` allowlisted (the memory embedder and
-- reranker and the voice-print encoder) and their licence evidence rows.
--
-- ── It refuses in one case, the same one `069`'s and `090`'s rollbacks do ───
--
-- The rows `098` seeds are machine-collected evidence that the collectors in
-- `scripts/models/` regenerate in seconds. A DETERMINATION recorded on top of
-- one of them is a named person's reading of a licence, which no script can
-- reconstruct. While any of these three rows carries one, this refuses.
--
-- ── Nor can removing them widen anything ───────────────────────────────────
--
-- Without the allowlist rows, `check_model_artefacts` fails readiness in
-- production-like environments for as long as `core/models/artefacts.py` still
-- pins them: the fail-closed direction. Without the register rows, nothing can
-- serve that could not serve before.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    determined INTEGER;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class
                    WHERE relname = 'model_licence_register') THEN
        RETURN;
    END IF;
    SELECT count(*) INTO determined
      FROM model_licence_register
     WHERE classification IN ('known_approved', 'known_restricted')
       AND source = 'huggingface'
       AND identifier IN ('BAAI/bge-large-en-v1.5', 'BAAI/bge-reranker-v2-m3',
                          'speechbrain/spkrec-ecapa-voxceleb');
    IF determined > 0 THEN
        RAISE EXCEPTION
            '098-down REFUSING: % memory/voice-print licence row(s) now carry a '
            'human determination. Removing them would delete a named person''s '
            'recorded reading of a licence.', determined
        USING HINT =
            'Rolling back the EVIDENCE does not undo the DETERMINATION made on '
            'top of it. Withdraw the determination deliberately first, or leave '
            'these rows in place.';
    END IF;
END $$;

DELETE FROM model_licence_register
 WHERE classification IN ('unknown', 'review_required')
   AND source = 'huggingface'
   AND identifier IN ('BAAI/bge-large-en-v1.5', 'BAAI/bge-reranker-v2-m3',
                      'speechbrain/spkrec-ecapa-voxceleb');

DELETE FROM model_pull_allowlist
 WHERE source = 'huggingface'
   AND (identifier, revision) IN (
        ('BAAI/bge-large-en-v1.5',            'd4aa6901d3a41ba39fb536a557fa166f842b0e09'),
        ('BAAI/bge-reranker-v2-m3',           '953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e'),
        ('speechbrain/spkrec-ecapa-voxceleb', '0f99f2d0ebe89ac095bcc5903c4dd8f72b367286'));

COMMIT;
