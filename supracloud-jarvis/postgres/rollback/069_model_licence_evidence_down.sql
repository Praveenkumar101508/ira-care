-- =============================================================================
-- rollback/069_model_licence_evidence_down.sql
--
-- Removes the seeded licence evidence. The table itself belongs to `068`.
--
-- ── It removes only the SEEDED rows, and only undetermined ones ────────────
--
-- Two things must survive this rollback, and a blanket `DELETE FROM
-- model_licence_register` would destroy both:
--
--   * a row a NAMED HUMAN has classified. That is a legal determination with
--     their name and the date on it, and it is the most expensive record in
--     this schema to reproduce — `OPEN_DECISIONS.md` D-08 is still the open
--     question of who that person is. Deleting it silently would be worse than
--     any state this migration created.
--
--   * a row some later evidence collection added. `069` seeded fifteen; a
--     rollback of `069` is not a mandate to discard whatever arrived after it.
--
-- So the DELETE is keyed on the exact (source, identifier, reference) triples
-- this file inserted, and refuses to touch a row that has been determined.
--
-- ── Nothing is left able to serve by removing evidence ─────────────────────
--
-- Deleting an undetermined row cannot enable anything: `068`'s gate requires a
-- `known_approved` row, and these are all `review_required` or `unknown`. The
-- direction is closed either way — with them, a model is refused because its
-- licence is unread; without them, it is refused because there is no row at
-- all. That is why this rollback needs no serving guard of its own, and `068`'s
-- does the work.
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
       AND (source, identifier) IN (
            ('huggingface', 'ai4bharat/indic-parler-tts'),
            ('huggingface', 'k2-fsa/OmniVoice'),
            ('huggingface', 'pyannote/speaker-diarization-3.1'),
            ('ollama', 'bge-m3'),
            ('ollama', 'deepseek-r1'),
            ('ollama', 'gemma3'),
            ('ollama', 'gemma3n'),
            ('ollama', 'nomic-embed-text'),
            ('ollama', 'qwen3'),
            ('ollama', 'qwen3-coder-next'));

    IF determined > 0 THEN
        RAISE EXCEPTION
            '069-down REFUSING: % seeded licence row(s) now carry a human '
            'determination. Removing them would delete a named person''s '
            'recorded reading of a licence.', determined
        USING HINT =
            'Rolling back the EVIDENCE does not undo the DETERMINATION made on '
            'top of it. Withdraw the determinations deliberately first, or '
            'leave these rows in place — they are harmless, since 068''s gate '
            'refuses anything that is not known_approved.';
    END IF;
END $$;

DELETE FROM model_licence_register
 WHERE classification IN ('unknown', 'review_required')
   AND (source, identifier) IN (
        ('huggingface', 'ai4bharat/indic-parler-tts'),
        ('huggingface', 'k2-fsa/OmniVoice'),
        ('huggingface', 'pyannote/speaker-diarization-3.1'),
        ('ollama', 'bge-m3'),
        ('ollama', 'deepseek-r1'),
        ('ollama', 'gemma3'),
        ('ollama', 'gemma3n'),
        ('ollama', 'nomic-embed-text'),
        ('ollama', 'qwen3'),
        ('ollama', 'qwen3-coder-next'));

COMMIT;
