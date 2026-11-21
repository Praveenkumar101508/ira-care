-- =============================================================================
-- 090_voice_model_pins.sql
--
-- Allowlists the Whisper checkpoints local speech-to-text loads, and records
-- licence EVIDENCE — no determination — for them and for Supertonic-3.
--
-- ── Why now ────────────────────────────────────────────────────────────────
--
-- `voice/stt.py` called `WhisperModel("distil-large-v3")`, which faster-whisper
-- resolves to `Systran/faster-distil-whisper-large-v3` at `main`: an unpinned
-- Hub fetch, the exact shape MOD-02 names. It stayed invisible because
-- faster-whisper was never in `requirements.txt`, so the native install never
-- reached the line. Adding faster-whisper makes the fetch real, so it now goes
-- through `core/models/artefacts.py` like every other artefact — and an
-- artefact that is pinned must be allowlisted (readiness refuses otherwise) and
-- must have licence evidence (`test_every_pinned_artefact_has_licence_evidence`).
--
-- ── What each row is ───────────────────────────────────────────────────────
--
--   Systran/faster-whisper-base             STT on low_resource / <4 CPUs
--   Systran/faster-whisper-small            STT CPU default
--   Systran/faster-distil-whisper-large-v3  STT opt-in, English-only
--   Systran/faster-whisper-large-v3         STT opt-in, multilingual
--   Supertone/supertonic-3                  TTS weights — package NOT installed
--
-- Supertonic-3 is here so its licence position is RECORDED, not so it is
-- fetched: its `supertonic` package is deliberately absent from
-- requirements.txt. The weights declare BigScience OpenRAIL-M, whose use
-- restrictions travel with the model and whose clause 1(g) treats hosted/API
-- access as Distribution. Reading that against IRA is the owner's
-- (MODEL_LICENCE_DETERMINATIONS_REQUIRED.md, L-06). Allowlisting is about
-- WHICH BYTES may be fetched, never about whether a licence permits use —
-- `k2-fsa/OmniVoice` is allowlisted and `known_restricted`.
--
-- ── Provenance ─────────────────────────────────────────────────────────────
--
-- Every value was RETRIEVED, not typed: the allowlist manifest columns by
-- `scripts/models/collect_artefact_manifests.py` (recomputable offline with
-- `--verify`), the register rows by `collect_licence_evidence.py`'s own
-- `collect_huggingface()` at the pinned revision. Both are recorded beside
-- this file in `docs/cloud/evidence/`, each new entry carrying its own
-- `retrieved_at` because it was collected on a different day from the fifteen
-- `069` seeded. `licence_identifier` is the publisher's string, verbatim:
-- `openrail` is not normalised to an SPDX id, because which RAIL variant
-- applies is part of the reading, not the recording.
--
-- `artifact_hash` stays NULL: nothing here has downloaded anything.
--
-- ON CONFLICT DO NOTHING because the rollback cycle re-applies this file.
-- =============================================================================

BEGIN;

INSERT INTO model_pull_allowlist
    (source, identifier, revision, purpose,
     manifest_sha256, manifest_method, manifest_source,
     manifest_file_count, manifest_retrieved_at)
VALUES
    ('huggingface', 'Systran/faster-whisper-base',
     'ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66',
     'local STT: low_resource profile or fewer than 4 CPUs',
     'sha256:c9405502dbb86c22996ea972e22c60984ef73eb27ed13daf8c4bd29be92c76b5', 'huggingface-blobs-v1',
     'https://huggingface.co/api/models/Systran/faster-whisper-base/revision/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66?blobs=true',
     6, TIMESTAMPTZ '2026-10-18T10:29:56+00:00'),
    ('huggingface', 'Systran/faster-whisper-small',
     '536b0662742c02347bc0e980a01041f333bce120',
     'local STT: CPU default',
     'sha256:39d8a65301e39e13bcf6eb5b38849c9d235c823836749db7e189f78a79b12318', 'huggingface-blobs-v1',
     'https://huggingface.co/api/models/Systran/faster-whisper-small/revision/536b0662742c02347bc0e980a01041f333bce120?blobs=true',
     6, TIMESTAMPTZ '2026-10-18T10:29:56+00:00'),
    ('huggingface', 'Systran/faster-distil-whisper-large-v3',
     'c3058b475261292e64a0412df1d2681c06260fab',
     'local STT: English-only, opt-in via WHISPER_MODEL',
     'sha256:d852c007ab5e56aec6d1d54094fb916400323089bacec8d382692ba8b89c329f', 'huggingface-blobs-v1',
     'https://huggingface.co/api/models/Systran/faster-distil-whisper-large-v3/revision/c3058b475261292e64a0412df1d2681c06260fab?blobs=true',
     7, TIMESTAMPTZ '2026-10-18T10:29:56+00:00'),
    ('huggingface', 'Systran/faster-whisper-large-v3',
     'edaa852ec7e145841d8ffdb056a99866b5f0a478',
     'local STT: multilingual, opt-in via WHISPER_MODEL; LiveKit agent default',
     'sha256:747d2086901968fe21019b546fcca49b1e1f46b91f9b79581e6c02e0c673ef9e', 'huggingface-blobs-v1',
     'https://huggingface.co/api/models/Systran/faster-whisper-large-v3/revision/edaa852ec7e145841d8ffdb056a99866b5f0a478?blobs=true',
     7, TIMESTAMPTZ '2026-10-18T10:29:56+00:00'),
    ('huggingface', 'Supertone/supertonic-3',
     '724fb5abbf5502583fb520898d45929e62f02c0b',
     'on-device TTS: package not installed; OpenRAIL-M awaiting owner determination',
     'sha256:400509d3c66bbdf668620f5ccc0508a010c3e2e0099ffe4040276725efa18231', 'huggingface-blobs-v1',
     'https://huggingface.co/api/models/Supertone/supertonic-3/revision/724fb5abbf5502583fb520898d45929e62f02c0b?blobs=true',
     26, TIMESTAMPTZ '2026-10-18T10:29:56+00:00')
ON CONFLICT (source, identifier, revision) DO NOTHING;

INSERT INTO model_licence_register
    (source, identifier, reference, licence_identifier, licence_text_sha256,
     artifact_digest, evidence_url, evidence_method, evidence_retrieved_at,
     classification)
VALUES
    ('huggingface', 'Systran/faster-whisper-base', 'ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66', 'mit', NULL, 'ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66', 'https://huggingface.co/api/models/Systran/faster-whisper-base/revision/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66', 'huggingface-model-api', TIMESTAMPTZ '2026-10-18T10:29:37+00:00', 'review_required'),
    ('huggingface', 'Systran/faster-whisper-small', '536b0662742c02347bc0e980a01041f333bce120', 'mit', NULL, '536b0662742c02347bc0e980a01041f333bce120', 'https://huggingface.co/api/models/Systran/faster-whisper-small/revision/536b0662742c02347bc0e980a01041f333bce120', 'huggingface-model-api', TIMESTAMPTZ '2026-10-18T10:29:37+00:00', 'review_required'),
    ('huggingface', 'Systran/faster-distil-whisper-large-v3', 'c3058b475261292e64a0412df1d2681c06260fab', 'mit', NULL, 'c3058b475261292e64a0412df1d2681c06260fab', 'https://huggingface.co/api/models/Systran/faster-distil-whisper-large-v3/revision/c3058b475261292e64a0412df1d2681c06260fab', 'huggingface-model-api', TIMESTAMPTZ '2026-10-18T10:29:37+00:00', 'review_required'),
    ('huggingface', 'Systran/faster-whisper-large-v3', 'edaa852ec7e145841d8ffdb056a99866b5f0a478', 'mit', NULL, 'edaa852ec7e145841d8ffdb056a99866b5f0a478', 'https://huggingface.co/api/models/Systran/faster-whisper-large-v3/revision/edaa852ec7e145841d8ffdb056a99866b5f0a478', 'huggingface-model-api', TIMESTAMPTZ '2026-10-18T10:29:37+00:00', 'review_required'),
    ('huggingface', 'Supertone/supertonic-3', '724fb5abbf5502583fb520898d45929e62f02c0b', 'openrail', NULL, '724fb5abbf5502583fb520898d45929e62f02c0b', 'https://huggingface.co/api/models/Supertone/supertonic-3/revision/724fb5abbf5502583fb520898d45929e62f02c0b', 'huggingface-model-api', TIMESTAMPTZ '2026-10-18T10:29:37+00:00', 'review_required')
ON CONFLICT (source, identifier, reference) DO NOTHING;

-- A data migration that silently matched nothing is indistinguishable from one
-- that worked, so count what is actually there. (A determination nobody signed
-- needs no check here: `068`'s `determination_names_a_human` makes it
-- unrepresentable.)
DO $$
DECLARE
    allowlisted INTEGER;
    recorded    INTEGER;
BEGIN
    SELECT count(*) INTO allowlisted
      FROM model_pull_allowlist
     WHERE source = 'huggingface'
       AND identifier IN ('Systran/faster-whisper-base', 'Systran/faster-whisper-small',
                          'Systran/faster-distil-whisper-large-v3',
                          'Systran/faster-whisper-large-v3', 'Supertone/supertonic-3')
       AND manifest_sha256 IS NOT NULL
       AND artifact_hash IS NULL;
    IF allowlisted <> 5 THEN
        RAISE EXCEPTION '090: expected 5 allowlisted voice artefacts with a manifest and no artifact_hash, found %', allowlisted;
    END IF;

    SELECT count(*) INTO recorded
      FROM model_licence_register
     WHERE source = 'huggingface'
       AND identifier IN ('Systran/faster-whisper-base', 'Systran/faster-whisper-small',
                          'Systran/faster-distil-whisper-large-v3',
                          'Systran/faster-whisper-large-v3', 'Supertone/supertonic-3');
    IF recorded <> 5 THEN
        RAISE EXCEPTION '090: expected 5 licence evidence rows for the voice artefacts, found %', recorded;
    END IF;
END $$;

COMMIT;
