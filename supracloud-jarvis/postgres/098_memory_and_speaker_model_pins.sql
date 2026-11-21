-- =============================================================================
-- 098_memory_and_speaker_model_pins.sql
--
-- Allowlists the memory embedder, the memory reranker and the voice-print
-- encoder at the revisions core/models/artefacts.py now pins, and records
-- licence EVIDENCE — no determination — for each.
--
-- ── Why ────────────────────────────────────────────────────────────────────
--
-- Three loaders fetched from the Hugging Face Hub at `main` (MOD-02), unseen
-- by tests/architecture/test_p5_artefacts_are_pinned.py, which only knew the
-- spellings `from_pretrained`, `snapshot_download`, `hf_hub_download` and
-- `WhisperModel`:
--
--   BAAI/bge-large-en-v1.5             memory/embeddings.py   SentenceTransformer
--   BAAI/bge-reranker-v2-m3            memory/reranker.py     CrossEncoder
--   speechbrain/spkrec-ecapa-voxceleb  voice/biometrics.py    from_hparams
--
-- Setup pre-downloaded the first two the same way. The encoder matters most:
-- SpeechBrain builds it from the fetched hyperparams.yaml, whose `!new:` tags
-- construct any importable Python object, so the repository's `main` decided
-- what ran at voice enrolment. The embedder matters differently: every stored
-- memory vector was made by it.
--
-- An artefact that is pinned must be allowlisted (readiness refuses otherwise)
-- and must have licence evidence (test_every_pinned_artefact_has_licence_evidence),
-- which is what this file seeds.
--
-- ── Numbering ──────────────────────────────────────────────────────────────
--
-- 096 is the durable "log out everywhere" change (auth_token_versions) and
-- 097 is LangGraph's checkpoint tables. This file depends on neither and
-- touches none of their objects. It was 097 until #154 took that number.
--
-- ── Provenance ─────────────────────────────────────────────────────────────
--
-- Every value was RETRIEVED, not typed: the allowlist manifest columns by
-- scripts/models/collect_artefact_manifests.py's _huggingface() (recomputable
-- offline with --verify), the register rows by collect_licence_evidence.py's
-- collect_huggingface() at the pinned revision. Both are recorded in
-- docs/cloud/evidence/ with their own retrieved_at; the rows below were
-- generated from those two files. `licence_identifier` is the publisher's
-- string, verbatim.
--
-- None of the three revisions has changed since 2024–25, so an install that
-- already downloaded them holds exactly these bytes and fetches nothing new.
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
    ('huggingface', 'BAAI/bge-large-en-v1.5',
     'd4aa6901d3a41ba39fb536a557fa166f842b0e09',
     'memory embeddings: EMBEDDING_MODEL default; stored vectors depend on it',
     'sha256:89d42bd8177c57547b13219fd22d928be84d4678239d8195380a9574282d229b', 'huggingface-blobs-v1',
     'https://huggingface.co/api/models/BAAI/bge-large-en-v1.5/revision/d4aa6901d3a41ba39fb536a557fa166f842b0e09?blobs=true',
     14, TIMESTAMPTZ '2026-10-22T01:47:55+00:00'),
    ('huggingface', 'BAAI/bge-reranker-v2-m3',
     '953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e',
     'memory recall reranker: RERANKER_MODEL default',
     'sha256:925afda7622e132368cebff366f5cb425629dda6cfb3fb164520d5811adadda4', 'huggingface-blobs-v1',
     'https://huggingface.co/api/models/BAAI/bge-reranker-v2-m3/revision/953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e?blobs=true',
     13, TIMESTAMPTZ '2026-10-22T01:47:55+00:00'),
    ('huggingface', 'speechbrain/spkrec-ecapa-voxceleb',
     '0f99f2d0ebe89ac095bcc5903c4dd8f72b367286',
     'voice-print lock: speaker embeddings for enrolment and verification',
     'sha256:175718821eac1e5ab98d165485de3a9d71da160d6ecaa86abdbfca5bf56830da', 'huggingface-blobs-v1',
     'https://huggingface.co/api/models/speechbrain/spkrec-ecapa-voxceleb/revision/0f99f2d0ebe89ac095bcc5903c4dd8f72b367286?blobs=true',
     10, TIMESTAMPTZ '2026-10-22T01:47:55+00:00')
ON CONFLICT (source, identifier, revision) DO NOTHING;

INSERT INTO model_licence_register
    (source, identifier, reference, licence_identifier, licence_text_sha256,
     artifact_digest, evidence_url, evidence_method, evidence_retrieved_at,
     classification)
VALUES
    ('huggingface', 'BAAI/bge-large-en-v1.5', 'd4aa6901d3a41ba39fb536a557fa166f842b0e09', 'mit', NULL, 'd4aa6901d3a41ba39fb536a557fa166f842b0e09', 'https://huggingface.co/api/models/BAAI/bge-large-en-v1.5/revision/d4aa6901d3a41ba39fb536a557fa166f842b0e09', 'huggingface-model-api', TIMESTAMPTZ '2026-10-22T01:47:55+00:00', 'review_required'),
    ('huggingface', 'BAAI/bge-reranker-v2-m3', '953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e', 'apache-2.0', NULL, '953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e', 'https://huggingface.co/api/models/BAAI/bge-reranker-v2-m3/revision/953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e', 'huggingface-model-api', TIMESTAMPTZ '2026-10-22T01:47:55+00:00', 'review_required'),
    ('huggingface', 'speechbrain/spkrec-ecapa-voxceleb', '0f99f2d0ebe89ac095bcc5903c4dd8f72b367286', 'apache-2.0', NULL, '0f99f2d0ebe89ac095bcc5903c4dd8f72b367286', 'https://huggingface.co/api/models/speechbrain/spkrec-ecapa-voxceleb/revision/0f99f2d0ebe89ac095bcc5903c4dd8f72b367286', 'huggingface-model-api', TIMESTAMPTZ '2026-10-22T01:47:55+00:00', 'review_required')
ON CONFLICT (source, identifier, reference) DO NOTHING;

-- A data migration that silently matched nothing is indistinguishable from one
-- that worked, so count what is actually there.
DO $$
DECLARE
    allowlisted INTEGER;
    recorded    INTEGER;
BEGIN
    SELECT count(*) INTO allowlisted
      FROM model_pull_allowlist
     WHERE source = 'huggingface'
       AND (identifier, revision) IN (
            ('BAAI/bge-large-en-v1.5',            'd4aa6901d3a41ba39fb536a557fa166f842b0e09'),
            ('BAAI/bge-reranker-v2-m3',           '953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e'),
            ('speechbrain/spkrec-ecapa-voxceleb', '0f99f2d0ebe89ac095bcc5903c4dd8f72b367286'))
       AND manifest_sha256 IS NOT NULL
       AND artifact_hash IS NULL;
    IF allowlisted <> 3 THEN
        RAISE EXCEPTION '098: expected 3 allowlisted memory/voice-print artefacts with a manifest and no artifact_hash, found %', allowlisted;
    END IF;

    SELECT count(*) INTO recorded
      FROM model_licence_register
     WHERE source = 'huggingface'
       AND identifier IN ('BAAI/bge-large-en-v1.5', 'BAAI/bge-reranker-v2-m3',
                          'speechbrain/spkrec-ecapa-voxceleb');
    IF recorded <> 3 THEN
        RAISE EXCEPTION '098: expected 3 licence evidence rows for the memory/voice-print artefacts, found %', recorded;
    END IF;
END $$;

COMMIT;
