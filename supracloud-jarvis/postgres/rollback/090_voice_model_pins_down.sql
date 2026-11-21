-- =============================================================================
-- rollback/090_voice_model_pins_down.sql
--
-- Removes the five voice artefacts `090` allowlisted and their licence
-- evidence rows.
--
-- ── It refuses in one case, the same one `069`'s rollback refuses in ───────
--
-- The rows `090` seeds are machine-collected evidence: the collectors in
-- `scripts/models/` regenerate them from the same source in seconds, so
-- removing them loses nothing that cannot be rebuilt. A DETERMINATION recorded
-- on top of one of them is different — a named person's reading of a licence,
-- which no script can reconstruct. While any of these five rows carries one,
-- this refuses rather than deleting it.
--
-- ── Nor can removing them widen anything ───────────────────────────────────
--
-- Without the allowlist rows, `check_model_artefacts` fails readiness in
-- production-like environments for as long as `core/models/artefacts.py` still
-- pins them — the fail-closed direction. Without the register rows, nothing
-- can serve that could not serve before: `068`'s gate refuses anything that is
-- not `known_approved`.
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
       AND identifier IN ('Systran/faster-whisper-base', 'Systran/faster-whisper-small',
                          'Systran/faster-distil-whisper-large-v3',
                          'Systran/faster-whisper-large-v3', 'Supertone/supertonic-3');
    IF determined > 0 THEN
        RAISE EXCEPTION
            '090-down REFUSING: % voice licence row(s) now carry a human '
            'determination. Removing them would delete a named person''s '
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
   AND identifier IN ('Systran/faster-whisper-base', 'Systran/faster-whisper-small',
                      'Systran/faster-distil-whisper-large-v3',
                      'Systran/faster-whisper-large-v3', 'Supertone/supertonic-3');

DELETE FROM model_pull_allowlist
 WHERE source = 'huggingface'
   AND (identifier, revision) IN (
        ('Systran/faster-whisper-base',            'ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66'),
        ('Systran/faster-whisper-small',           '536b0662742c02347bc0e980a01041f333bce120'),
        ('Systran/faster-distil-whisper-large-v3', 'c3058b475261292e64a0412df1d2681c06260fab'),
        ('Systran/faster-whisper-large-v3',        'edaa852ec7e145841d8ffdb056a99866b5f0a478'),
        ('Supertone/supertonic-3',                 '724fb5abbf5502583fb520898d45929e62f02c0b'));

COMMIT;
