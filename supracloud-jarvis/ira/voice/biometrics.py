"""
IRA Offline Biometric Voice Verifier.

Uses SpeechBrain's ECAPA-TDNN model (spkrec-ecapa-voxceleb) — a state-of-the-art
speaker embedding model trained on VoxCeleb. Runs entirely on CPU inside the
ira-voice container. No cloud calls, no external APIs.

Architecture:
  1. Audio chunk (bytes, 16kHz PCM) → resample → extract ECAPA-TDNN embedding
  2. Cosine similarity vs. stored owner voice profile (pulled from DB once, cached)
  3. Return True if similarity ≥ BIOMETRIC_THRESHOLD (default 0.75)

Owner enrolment flow:
  POST /api/v1/voice/enroll  → admin submits ≥3 reference audio segments
  → Embeddings averaged → stored in voice_profiles table

Verification flow (per-utterance in voice/agent.py):
  is_owner_authenticated(audio_bytes) → bool
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import struct
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np

from config import get_settings

logger = logging.getLogger("ira.biometrics")

# Thread pool for CPU-bound embedding computation — keeps async loop responsive
_bio_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="biometrics")

# In-process cache for owner embedding (avoids DB round-trip per utterance)
_owner_embedding_cache: Optional[list[float]] = None

# ECAPA model cache directory, one subfolder per pinned revision. Resolves to
# the OS-appropriate home:
#   Linux/Docker → /home/<user>/.cache/speechbrain/spkrec-ecapa/<revision>
#   Windows      → C:\Users\<user>\.cache\speechbrain\spkrec-ecapa\<revision>
# Portable so the same code runs in the container and on a Windows dev box.
# (On Windows, SpeechBrain symlinks the HF-cached model here — that needs
# Developer Mode enabled, or the symlink raises WinError 1314.)
_ECAPA_SAVEDIR = Path.home() / ".cache" / "speechbrain" / "spkrec-ecapa"


def _ecapa_savedir(revision: str) -> str:
    """Where SpeechBrain links this revision's files.

    Per revision because SpeechBrain reuses a file already in `savedir` by its
    NAME, whatever revision asked for it. With one shared folder, the first
    revision ever fetched kept being served: a changed pin silently did not take
    effect, and a pin the Hub does not have still "loaded" (seen on 22 October
    2026). A folder per revision fetches exactly the pinned one or fails.
    """
    return str(_ECAPA_SAVEDIR / revision)


# ── Model loading ─────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _get_encoder():
    """
    Load ECAPA-TDNN speaker encoder (lazy, cached).
    Downloads ~100MB on first use to the HuggingFace cache volume.
    """
    try:
        from speechbrain.inference.speaker import EncoderClassifier
        from speechbrain.utils.fetching import FetchConfig

        from core.models.artefacts import identifier_for, revision_for

        # Pinned (core/models/artefacts.py). SpeechBrain builds this model from
        # the fetched hyperparams.yaml, whose `!new:` tags construct any
        # importable Python object, so an unpinned fetch lets whoever controls
        # the repository at `main` choose what runs here. The one FetchConfig
        # covers the yaml and every checkpoint the pretrainer collects. A
        # refused pin lands in the except below: no encoder, so no voice
        # matches the owner — closed, not open.
        revision = revision_for("speaker_ecapa")
        encoder = EncoderClassifier.from_hparams(
            source=identifier_for("speaker_ecapa"),
            run_opts={"device": "cpu"},
            savedir=_ecapa_savedir(revision),
            fetch_config=FetchConfig(revision=revision),
        )
        logger.info("ECAPA-TDNN speaker encoder loaded")
        return encoder
    except Exception as e:
        logger.error(f"Failed to load ECAPA-TDNN model: {e}")
        return None


# ── Audio preprocessing ───────────────────────────────────────────────────────

def _pcm_bytes_to_tensor(audio_bytes: bytes, sample_rate: int = 16000):
    """
    Convert raw PCM bytes (16-bit signed, mono) to a torch float tensor
    normalised to [-1, 1] range.
    """
    import torch
    n_samples = len(audio_bytes) // 2
    samples = struct.unpack(f"<{n_samples}h", audio_bytes)
    waveform = np.array(samples, dtype=np.float32) / 32768.0
    return torch.tensor(waveform).unsqueeze(0)  # shape: [1, samples]


def _compute_embedding_sync(audio_bytes: bytes) -> Optional[list[float]]:
    """
    Compute ECAPA-TDNN speaker embedding from raw 16kHz PCM bytes.
    Returns a 192-dimensional unit-normalised vector, or None on failure.
    Runs synchronously — call via run_in_executor.
    """
    try:
        import torch
        encoder = _get_encoder()
        if encoder is None:
            return None

        waveform = _pcm_bytes_to_tensor(audio_bytes)
        with torch.no_grad():
            embedding = encoder.encode_batch(waveform)
            # embedding shape: [1, 1, 192] → flatten to [192]
            vec = embedding.squeeze().cpu().numpy()
            # L2-normalise for cosine similarity via dot product
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
        return vec.tolist()
    except Exception as e:
        logger.warning(f"Embedding computation failed: {e}")
        return None


async def compute_embedding(audio_bytes: bytes) -> Optional[list[float]]:
    """Async wrapper — offloads CPU embedding work to thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_bio_executor, _compute_embedding_sync, audio_bytes)


# ── Cosine similarity ─────────────────────────────────────────────────────────

def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two L2-normalised vectors (= dot product)."""
    va = np.array(a, dtype=np.float32)
    vb = np.array(b, dtype=np.float32)
    return float(np.dot(va, vb))


# ── Owner profile management ──────────────────────────────────────────────────

async def _load_owner_profile() -> Optional[list[float]]:
    """
    Fetch the owner's stored voice embedding from the database.
    Result is cached in-process for the lifetime of the container.
    """
    global _owner_embedding_cache
    if _owner_embedding_cache is not None:
        return _owner_embedding_cache

    try:
        from core.identity.db_context import voiceprint_connection
        async with voiceprint_connection("load owner voiceprint") as conn:
            from core.runtime.plaintext_columns import exists

            # `embedding` is named only while it exists. `063` drops it once
            # every template is encrypted; a statement naming a dropped column
            # raises, and on this path a raise means voice verification is
            # unavailable rather than merely unencrypted.
            legacy = await exists(conn, "voice_profiles", "embedding")
            plaintext = "embedding, " if legacy else "NULL::text AS embedding, "
            row = await conn.fetchrow(
                "SELECT id, enrollment_version, " + plaintext +
                "       migration_status, "
                "       template_ciphertext, template_nonce, wrapped_dek, "
                "       wrap_nonce, kek_id, encryption_key_version "
                "  FROM voice_profiles ORDER BY created_at DESC LIMIT 1"
            )
        if row is None:
            return None

        # Encrypted first, and PLAINTEXT is not a fallback for a row that is
        # marked ENCRYPTED. A row whose ciphertext will not open must fail
        # closed: falling back to `embedding` would silently authenticate
        # against a template somebody could have replaced, which is precisely
        # what encrypting it was for. During the migration window a row is one
        # or the other, never both — `063` drops the plaintext once every row is
        # done, and until then an un-migrated row still has only `embedding`.
        if row["migration_status"] == "ENCRYPTED":
            from security.voice_crypto import use_template

            _owner_embedding_cache = await use_template(row, json.loads)
            logger.info("Owner voice profile loaded and decrypted")
            return _owner_embedding_cache

        if row["embedding"]:
            _owner_embedding_cache = json.loads(row["embedding"])
            logger.info("Owner voice profile loaded from database")
            return _owner_embedding_cache
    except Exception as e:
        # The exception is formatted, not chained, and that is deliberate: on
        # the encrypted path `e` comes from the crypto layer, and a traceback
        # through `use_template` would put the row's identifiers in a log.
        logger.warning(f"Could not load owner voice profile: {type(e).__name__}")
    return None


#: What a voiceprint's consent record says, right now.
#:
#: Deliberately UNCACHED, unlike the template itself. `_owner_embedding_cache`
#: holds the template in memory so verification does not re-decrypt on every
#: utterance; if consent were read from that cache, a revocation would not take
#: effect until the process restarted — and "revoked, but still authenticating
#: until someone reboots" is not a revocation.
#:
#: One indexed read per verification is the price of the revocation meaning
#: what it says.
#: `voice_consent_state()` -> the word `biometric_audit.failure_reason` accepts.
#:
#: `041` constrained that column to a controlled vocabulary and said why: a
#: free-text reason beside an audit row is where somebody eventually writes the
#: username. So the refusal reasons are TRANSLATED into it rather than formatted
#: from the state name — an f-string would have produced `consent_revoked`,
#: which the CHECK rejects, which `_record_biometric_attempt`'s non-fatal
#: `except` then swallows. The refused attempt would have left no audit row at
#: all, which is the exact thing this batch is trying to make impossible.
#:
#: `no_enrolment` and `revoked_enrolment` are `041`'s own words. `consent_missing`
#: is added by `083`, because "enrolled and never consented" is a third fact and
#: recording it as either of the other two would be false.
CONSENT_FAILURE_REASON = {
    "no_profile": "no_enrolment",
    "revoked": "revoked_enrolment",
    "missing": "consent_missing",
}


async def voice_consent_state() -> str:
    """`"granted"`, `"revoked"`, `"missing"` or `"no_profile"`.

    P12-04 finding I. `037` gave `voice_profiles` `consent_granted_at`,
    `consent_reference`, `revoked_at` and `revocation_reason`, and until this
    batch NOTHING in the codebase read or wrote any of them. A voiceprint
    enrolled with no consent authenticated normally, and revoking consent had
    no effect on the only path that reads the template.

    Four distinct answers rather than a boolean, because the caller has to be
    able to tell them apart in the audit trail. `is_owner_authenticated` is
    documented as best-effort — it returns False when the model is unavailable
    so voice degrades to public access rather than crashing — so a consent
    refusal returning the same False down the same channel would be
    indistinguishable from a missing model. That is this phase's recurring
    failure mode, and naming the reason is what removes it.
    """
    from core.identity.db_context import voiceprint_connection

    # Selected the same way `_load_owner_profile` selects — newest live row
    # visible to this connection — and NOT by `owner_name`. The two must agree
    # about WHICH row they are talking about: consent read for one row while a
    # different row is decrypted and matched would be a check that answers a
    # question nobody asked. `owner_name` is a display string that `037`
    # deliberately stripped of identity meaning, so it cannot be the join key
    # between them; RLS on `user_id` is.
    async with voiceprint_connection("read voiceprint consent") as conn:
        row = await conn.fetchrow(
            "SELECT consent_granted_at, revoked_at FROM voice_profiles "
            " WHERE deletion_status = 'live' "
            " ORDER BY created_at DESC LIMIT 1")
    if row is None:
        return "no_profile"
    if row["revoked_at"] is not None:
        return "revoked"
    if row["consent_granted_at"] is None:
        return "missing"
    return "granted"


async def save_owner_profile(embeddings: list[list[float]], *,
                             consent_reference: str) -> bool:
    """
    Average a list of reference embeddings and persist to the database.
    Call this during the voice enrolment flow (POST /voice/enroll).
    Returns True on success.

    `consent_reference` is REQUIRED and keyword-only (P12-04). It is the record
    of what the person agreed to — a policy version, a signed form id, whatever
    the deployment can point at afterwards. Required rather than optional for
    the reason `_make_access_token`'s `ver` is: an optional argument makes the
    unconsented enrolment expressible, and a caller who forgets it gets a
    stored biometric template instead of a TypeError.

    A voiceprint is the most sensitive thing this system holds about a body.
    Enrolling one without recording that it was agreed to is the failure this
    batch exists to close, and `037` has had the column for it since Phase 3B.
    """
    if not (consent_reference or "").strip():
        logger.error("voice enrolment refused: no consent reference supplied")
        return False
    global _owner_embedding_cache

    if not embeddings:
        return False

    # Average and re-normalise
    matrix = np.array(embeddings, dtype=np.float32)
    avg = matrix.mean(axis=0)
    norm = np.linalg.norm(avg)
    if norm > 0:
        avg = avg / norm

    embedding_json = json.dumps(avg.tolist())

    try:
        from core.identity.db_context import (voiceprint_connection,
                                              voiceprint_owner_id)
        from config import get_settings
        cfg = get_settings()
        owner_id = voiceprint_owner_id()
        async with voiceprint_connection("enrol voiceprint") as conn:
            # Written encrypted, in one statement, from the first enrolment
            # onward. The row is INSERTed with a placeholder so the primary key
            # exists — `voice_crypto`'s AAD binds the row id, and an id is not
            # knowable before the INSERT — and then sealed against it. Both
            # halves are in one transaction: a crash between them would leave a
            # row that says PLAINTEXT and holds a placeholder, which is a
            # voiceprint that silently matches nobody.
            #
            # `062` is what makes the second statement safe to rely on: an
            # ENCRYPTED row missing any key column is unrepresentable, so a
            # half-written enrolment cannot be recorded as done.
            from security.voice_crypto import seal_template

            from core.runtime.plaintext_columns import exists

            # ── No ON CONFLICT (owner_name), and that is a bug fix ──────────
            #
            # This statement used `ON CONFLICT (owner_name) DO UPDATE` until
            # Phase 4 batch 03. `037` DROPPED `UNIQUE (owner_name)` in Phase 3B
            # — deliberately, because a global unique owner name is exactly the
            # cross-tenant collision that batch existed to remove — and nothing
            # updated this call. PostgreSQL answers an ON CONFLICT clause with
            # no matching constraint by raising, so every enrolment since `037`
            # has failed with:
            #
            #   there is no unique or exclusion constraint matching the
            #   ON CONFLICT specification
            #
            # It was invisible because the enclosing `except` returns False and
            # logs, and nothing called this function in a test. A broken write
            # path that reports failure quietly is the shape this repository
            # keeps finding, and it survived a whole phase.
            #
            # Read-then-write instead, in the transaction that already exists.
            # `owner_name` is not unique any more and must not be treated as if
            # it were: the newest row wins, which is the same rule
            # `_load_owner_profile` applies when it reads.
            legacy = await exists(conn, "voice_profiles", "embedding")

            async with conn.transaction():
                # Not filtered by `owner_name` any more (P12-04 finding J). RLS
                # on `user_id` is what scopes this now, and it is the same scope
                # the two read paths use — so a re-enrolment updates the row
                # verification will actually load. Filtering on the display name
                # as well would mean renaming yourself silently orphaned your
                # own voiceprint and enrolled a second one beside it, which the
                # `voice_profiles_active_user_key` partial unique index would
                # then refuse in a way nobody could read.
                existing = await conn.fetchrow(
                    "SELECT id, enrollment_version FROM voice_profiles "
                    " WHERE deletion_status = 'live' "
                    " ORDER BY created_at DESC LIMIT 1 FOR UPDATE")

                if existing is not None:
                    # A re-enrolment is a NEW version, not an overwrite. The AAD
                    # binds the enrolment version, so bumping it means the
                    # previous template's key cannot open the replacement —
                    # which is what makes re-enrolling after a suspected
                    # compromise mean anything.
                    row = await conn.fetchrow(
                        # Re-enrolment re-affirms consent and CLEARS any
                        # revocation: the person is enrolling again, now, with
                        # a reference to what they agreed to. Leaving
                        # `revoked_at` set would produce a fresh template that
                        # cannot authenticate, which is the silent-empty shape
                        # in a new place.
                        "UPDATE voice_profiles "
                        "   SET enrollment_version = enrollment_version + 1, "
                        "       updated_at = NOW(), "
                        "       consent_granted_at = NOW(), "
                        "       consent_reference = $2, "
                        "       revoked_at = NULL, "
                        "       revocation_reason = NULL "
                        " WHERE id = $1 RETURNING id, enrollment_version",
                        existing["id"], consent_reference.strip())
                elif legacy:
                    # `embedding` is NOT NULL before `063`, so it cannot simply
                    # be omitted — the INSERT has to differ between the two
                    # schemas. The placeholder is empty, not the template:
                    # nothing writes a plaintext voiceprint any more, in either.
                    row = await conn.fetchrow(
                        "INSERT INTO voice_profiles (owner_name, user_id, embedding, "
                        "                            consent_granted_at, consent_reference) "
                        "VALUES ($1, $3, '', NOW(), $2) RETURNING id, enrollment_version",
                        cfg.owner_name, consent_reference.strip(), owner_id)
                else:
                    # `user_id` is WRITTEN, not defaulted (P12-04 finding J).
                    # `037`'s WITH CHECK compares it against `app.user_id`, so
                    # omitting it — as this statement did since `037` shipped —
                    # makes every enrolment fail the policy. That refusal landed
                    # in the `except` below and was logged as an ordinary save
                    # failure.
                    row = await conn.fetchrow(
                        "INSERT INTO voice_profiles (owner_name, user_id, "
                        "                            consent_granted_at, consent_reference) "
                        "VALUES ($1, $3, NOW(), $2) RETURNING id, enrollment_version",
                        cfg.owner_name, consent_reference.strip(), owner_id)
                sealed = seal_template(
                    row["id"], row["enrollment_version"], embedding_json)
                await conn.execute(
                    """UPDATE voice_profiles
                          SET template_ciphertext = $2, template_nonce = $3,
                              wrapped_dek = $4, wrap_nonce = $5, kek_id = $6,
                              encryption_key_version = $7,
                              encrypted_at = NOW(),
                              migration_status = 'ENCRYPTED'
                        WHERE id = $1""",
                    row["id"], sealed["template_ciphertext"],
                    sealed["template_nonce"], sealed["wrapped_dek"],
                    sealed["wrap_nonce"], sealed["kek_id"],
                    sealed["encryption_key_version"],
                )
        _owner_embedding_cache = avg.tolist()
        logger.info(f"Owner voice profile saved ({len(embeddings)} reference segments)")
        return True
    except Exception as e:
        logger.error(f"Failed to save owner voice profile: {e}")
        return False


async def revoke_voice_consent(reason: str) -> bool:
    """Withdraw consent for the enrolled voiceprint. Returns True if a row changed.

    P12-04. The counterpart to enrolment: `037` has recorded `revoked_at` and
    `revocation_reason` since Phase 3B and nothing ever set them.

    Two properties worth stating, because each is a way this is commonly got
    wrong:

    * **It counts the rows it changed.** A revocation that matched nothing and
      reported success is a person told their voiceprint is withdrawn while it
      still authenticates — the false-clean shape this phase keeps finding.
    * **It does not delete the template.** Revocation and erasure are different
      requests: `core/privacy/erasure.py` destroys the material, this withdraws
      permission to use it, and conflating them would make "stop using this"
      indistinguishable from "destroy the evidence". `voice_consent_state`
      refuses on `revoked_at` regardless of the template still being present.

    The in-memory cache is invalidated so this process stops using a template
    it has already decrypted. That is belt to `voice_consent_state`'s braces —
    consent is re-read per verification and is never cached — so a revocation
    performed by another process still takes effect here immediately.
    """
    if not (reason or "").strip():
        logger.error("voice consent revocation refused: no reason supplied")
        return False

    from core.identity.db_context import voiceprint_connection

    async with voiceprint_connection("revoke voiceprint consent") as conn:
        result = await conn.execute(
            "UPDATE voice_profiles "
            "   SET revoked_at = NOW(), revocation_reason = $1, updated_at = NOW() "
            " WHERE deletion_status = 'live' AND revoked_at IS NULL",
            reason.strip())

    try:
        changed = int(str(result).rsplit(" ", 1)[-1])
    except ValueError:  # pragma: no cover — driver-dependent
        changed = 0

    invalidate_profile_cache()
    if not changed:
        logger.warning("voice consent revocation matched no live, unrevoked profile")
        return False
    logger.info("voice consent revoked (%d profile(s))", changed)
    return True


def invalidate_profile_cache() -> None:
    """Force reload of owner profile on next verification call."""
    global _owner_embedding_cache
    _owner_embedding_cache = None


# ── Main verification entry point ─────────────────────────────────────────────

async def is_owner_authenticated(audio_bytes: bytes, session_id: str = "unknown") -> bool:
    """
    Verify whether the speaker in the audio chunk is the system owner.

    Args:
        audio_bytes: Raw PCM audio data (16-bit signed, mono, 16kHz).
        session_id:  Voice session identifier — written to biometric_audit table.

    Returns:
        True  → speaker matches owner profile with similarity ≥ threshold.
        False → no profile, model unavailable, or speaker does not match.

    Biometric verification is best-effort: if the model is unavailable
    (first-run download pending, out of memory), this returns False
    so the voice session degrades gracefully to public-access mode
    rather than crashing.
    """
    cfg = get_settings()

    # Require at least 1 second of audio for a reliable ECAPA-TDNN embedding. (#35)
    # 16kHz × 16-bit mono = 32 000 bytes/second.
    # 100ms (3 200 bytes) is far too short — embeddings from very short clips have
    # high variance and produce excessive false-positives / false-negatives.
    if not audio_bytes or len(audio_bytes) < 32_000:  # <1s at 16kHz 16-bit mono
        logger.debug(
            f"Audio too short for biometric check: {len(audio_bytes)} bytes "
            f"(need ≥32 000 / 1 s)"
        )
        return False

    # ── Consent, checked BEFORE the template is used ────────────────────
    #
    # P12-04 finding I. `037` has had these columns since Phase 3B and nothing
    # read them, so a voiceprint enrolled without recorded consent
    # authenticated normally and a revocation changed nothing.
    #
    # Checked here rather than folded into `_load_owner_profile`'s query for
    # two reasons: the template is cached in memory and consent must not be,
    # or a revocation would wait for a restart; and the REASON has to reach
    # `biometric_audit`, which a filtered SELECT returning no row cannot say.
    consent = await voice_consent_state()
    if consent != "granted":
        logger.warning("biometric verification refused: consent is %r", consent)
        await _record_biometric_attempt(
            session_id, similarity=0.0, threshold=cfg.biometric_threshold,
            authenticated=False, failure_reason=CONSENT_FAILURE_REASON[consent])
        return False

    # Load the owner's reference profile
    owner_profile = await _load_owner_profile()
    if owner_profile is None:
        logger.debug("No owner voice profile enrolled — biometric auth unavailable")
        return False

    # Compute embedding for incoming audio
    embedding = await compute_embedding(audio_bytes)
    if embedding is None:
        return False

    # Cosine similarity check
    similarity = cosine_similarity(embedding, owner_profile)
    authenticated = similarity >= cfg.biometric_threshold

    logger.info(
        f"Biometric check: similarity={similarity:.3f} "
        f"threshold={cfg.biometric_threshold} "
        f"result={'PASS' if authenticated else 'FAIL'}"
    )

    # Persist every check to biometric_audit for security auditing
    await _record_biometric_attempt(
        session_id, similarity=float(similarity),
        threshold=float(cfg.biometric_threshold), authenticated=authenticated)

    return authenticated


async def _record_biometric_attempt(session_id: str, *, similarity: float,
                                    threshold: float, authenticated: bool,
                                    failure_reason: str | None = None) -> None:
    """One audit row per verification attempt, including the refused ones.

    `failure_reason` is what makes a consent refusal distinguishable from a
    missing model. Both return False to the caller — deliberately, so voice
    degrades to public access rather than crashing — and without a recorded
    reason an operator cannot tell 'this person revoked consent' from 'the
    embedding model did not load'.

    Non-fatal, as it was: an audit outage must not turn a verification into an
    exception. It is a debug log rather than a warning because a failure here
    is an infrastructure problem, not a security event.
    """
    try:
        from core.identity.db_context import voiceprint_connection
        async with voiceprint_connection("record biometric attempt") as conn:
            # ATTRIBUTED, where this used to write NULL/NULL (P12-04 finding J).
            # `049`'s `subject_or_tenant_insert` permits an unattributed row —
            # `(tenant_id IS NULL AND user_id IS NULL)` — so the old unbound
            # INSERT succeeded, and `subject_or_tenant_select` then made every
            # row it wrote invisible to the subject AND to the tenant, because
            # both halves of the predicate compare against NULL. An audit row
            # nobody can read does not distinguish anything from anything.
            await conn.execute(
                """INSERT INTO biometric_audit
                   (session_id, similarity, threshold, result, source,
                    failure_reason, tenant_id, user_id)
                   VALUES ($1, $2, $3, $4, 'voice', $5,
                           NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                           NULLIF(current_setting('app.user_id', true), '')::uuid)""",
                session_id, similarity, threshold, authenticated, failure_reason,
            )
    except Exception as audit_err:
        logger.debug(f"Biometric audit write failed (non-fatal): {audit_err}")