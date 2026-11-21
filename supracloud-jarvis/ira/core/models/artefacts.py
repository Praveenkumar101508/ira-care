"""Every model artefact IRA fetches, pinned (Phase 5, MOD-02).

`BASELINE_MODEL_INVENTORY.md` §8:

    FINDING MOD-02 — model artefacts are neither hashed nor pinned.
    `voice/tts_indic.py` calls `from_pretrained()` **without revision pinning**
    (Bandit B615, ×2) — it fetches whatever the Hub serves at that moment.
    No allowlist prevents an arbitrary `ollama pull`.

Three call sites fetched from the Hub with no revision: `voice/tts_indic.py`,
`voice/omnivoice_sidecar.py` and `meetings/diarise.py`. Each now asks this
module which revision to use, and this module refuses to answer for anything it
does not know about.

── Why the pin lives here and not in each call site ────────────────────────

A revision written inline at three call sites is three places to forget, and
`tests/architecture/test_p5_artefacts_are_pinned.py` cannot tell a hard-coded
revision from a pinned one that has drifted from the allowlist. One table, one
lookup, and the loader has nothing to get wrong.

── Why the pin is not read from the database on the hot path ───────────────

These loaders are synchronous and run inside a thread — `_load()` in
`tts_indic.py` is called lazily behind a lock. The registry API is async, and
driving an event loop from inside a worker thread to answer "which revision"
would be a genuinely dangerous piece of machinery to put on a lazy-import path.

So the split is:

  * **This module** holds the pin, and is the single place a call site reads.
  * **`verify_against_allowlist()`** is async, runs at readiness, and asserts
    every pin here is on `model_pull_allowlist` at that exact revision.

A pin that disagrees with the allowlist therefore fails readiness — the process
does not come up — rather than being caught on the first lazy load, hours later,
in a thread. That is the fail-closed direction and the one an operator can act
on.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

logger = logging.getLogger("ira.models.artefacts")

__all__ = ["Artefact", "ARTEFACTS", "revision_for", "UnpinnedArtefact",
           "load_cached_first", "verify_against_allowlist"]

_Model = TypeVar("_Model")


class UnpinnedArtefact(RuntimeError):
    """Raised rather than defaulting to `main`.

    Defaulting is what MOD-02 describes: the fetch succeeds, the model loads,
    and nobody learns that the artefact changed under them.
    """


@dataclass(frozen=True, slots=True)
class Artefact:
    identifier: str
    revision: str
    source: str = "huggingface"
    purpose: str = ""


def _pin(env_var: str, identifier: str, default_revision: str, purpose: str) -> Artefact:
    """One artefact, with its revision overridable by environment.

    Overridable because upgrading a model must not require a code change — but
    NOT defaultable: an operator who sets the variable to an empty string, or to
    `main`, is refused by `revision_for` rather than silently unpinned.
    """
    return Artefact(
        identifier=os.getenv(f"{env_var}_MODEL", identifier).strip(),
        revision=os.getenv(f"{env_var}_REVISION", default_revision).strip(),
        purpose=purpose,
    )


#: The pins.
#:
#: Each revision below was RESOLVED from the Hub's model API on 2026-08-09 and
#: recorded, not guessed. That distinction is the whole exercise: a plausible
#: forty-hex string in this file would satisfy every "is it pinned" check while
#: pinning nothing, and would fail only at fetch time on somebody else's
#: machine.
#:
#: A revision that is wrong fails LOUDLY — the Hub returns 404 for an unknown
#: revision — which is the right failure. The dangerous one is the silent
#: success against a different artefact, which is what no pin at all produces.
ARTEFACTS: dict[str, Artefact] = {
    "indic_tts": _pin(
        "IRA_INDIC_TTS", "ai4bharat/indic-parler-tts",
        "7b527af5ee8ed1f9a28d80b19703ed9bb8ba10ca",
        "Indic TTS for ta/te/kn/ml"),
    "omnivoice": _pin(
        "IRA_OMNIVOICE", "k2-fsa/OmniVoice",
        "c5fdb5ccb189668d56333f77ba2629f4cd7535f4",
        "OmniVoice sidecar"),
    "diarisation": _pin(
        "IRA_DIARISATION", "pyannote/speaker-diarization-3.1",
        "84fd25912480287da0247647c3d2b4853cb3ee5d",
        "speaker diarisation for meetings"),
    # ── Local speech-to-text (voice/stt.py), resolved 2026-10-18 ────────────
    #
    # faster-whisper maps a bare name ("small") to these repositories at
    # `main`. voice/stt.py hands it the repository AND the revision below, so
    # these four are the only Whisper checkpoints IRA will fetch.
    "whisper_base": _pin(
        "IRA_WHISPER_BASE", "Systran/faster-whisper-base",
        "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
        "local STT: low_resource profile or fewer than 4 CPUs"),
    "whisper_small": _pin(
        "IRA_WHISPER_SMALL", "Systran/faster-whisper-small",
        "536b0662742c02347bc0e980a01041f333bce120",
        "local STT: CPU default"),
    "whisper_distil_large_v3": _pin(
        "IRA_WHISPER_DISTIL_LARGE_V3", "Systran/faster-distil-whisper-large-v3",
        "c3058b475261292e64a0412df1d2681c06260fab",
        "local STT: English-only, opt-in via WHISPER_MODEL"),
    "whisper_large_v3": _pin(
        "IRA_WHISPER_LARGE_V3", "Systran/faster-whisper-large-v3",
        "edaa852ec7e145841d8ffdb056a99866b5f0a478",
        "local STT: multilingual, opt-in via WHISPER_MODEL; LiveKit agent default"),
    # ── On-device TTS (voice/tts_supertonic.py) — NOT installed ─────────────
    #
    # Pinned so its licence evidence is RECORDED, not so it is fetched. The
    # `supertonic` package (MIT) is deliberately absent from requirements.txt:
    # its weights declare BigScience OpenRAIL-M, a use-restricted licence whose
    # clause 1(g) treats hosted/API access as Distribution, and that reading is
    # the owner's (MODEL_LICENCE_DETERMINATIONS_REQUIRED.md, L-06). The
    # revision is the one supertonic 1.3.1 itself pins in its MODEL_CONFIGS.
    "supertonic": _pin(
        "IRA_SUPERTONIC", "Supertone/supertonic-3",
        "724fb5abbf5502583fb520898d45929e62f02c0b",
        "on-device TTS: package not installed; OpenRAIL-M awaiting owner determination"),
    # ── Memory and voice-print models, resolved 2026-10-22 ──────────────────
    #
    # All three were fetched at `main` by loaders the pin guard could not see:
    # sentence-transformers' `SentenceTransformer` and `CrossEncoder`
    # (memory/embeddings.py, memory/reranker.py and setup's pre-download) and
    # SpeechBrain's `from_hparams` (voice/biometrics.py). The last matters most.
    # SpeechBrain builds the model from the fetched `hyperparams.yaml`, whose
    # `!new:` tags construct any importable Python object, so whoever controls
    # the repository at `main` decides what runs when someone enrols a voice.
    # The embedder matters differently: every stored memory vector was made by
    # it, and a silent change upstream would move recall under all of them.
    "memory_embedding": _pin(
        "IRA_MEMORY_EMBEDDING", "BAAI/bge-large-en-v1.5",
        "d4aa6901d3a41ba39fb536a557fa166f842b0e09",
        "memory embeddings: EMBEDDING_MODEL default; stored vectors depend on it"),
    "memory_reranker": _pin(
        "IRA_MEMORY_RERANKER", "BAAI/bge-reranker-v2-m3",
        "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
        "memory recall reranker: RERANKER_MODEL default"),
    "speaker_ecapa": _pin(
        "IRA_SPEAKER_ECAPA", "speechbrain/spkrec-ecapa-voxceleb",
        "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286",
        "voice-print lock: speaker embeddings for enrolment and verification"),
}


def revision_for(key: str) -> str:
    """The pinned revision, or a refusal.

    Never returns `main`, `latest` or an empty string — those are the three ways
    a pin stops pinning, and returning one would satisfy every caller's
    `revision=` argument while restoring exactly the behaviour MOD-02 names.
    """
    artefact = ARTEFACTS.get(key)
    if artefact is None:
        raise UnpinnedArtefact(
            f"{key!r} is not a known artefact. Add it to core/models/artefacts.py "
            f"and to the pull allowlist; IRA does not fetch models it has not "
            f"recorded.")
    revision = (artefact.revision or "").strip()
    if not revision or revision.lower() in ("latest", "main", "master", "head"):
        raise UnpinnedArtefact(
            f"{artefact.identifier!r} has revision {revision!r}, which does not "
            f"pin anything — it fetches whatever the source serves at that "
            f"moment. Set a commit hash.")
    return revision


def identifier_for(key: str) -> str:
    artefact = ARTEFACTS.get(key)
    if artefact is None:
        raise UnpinnedArtefact(f"{key!r} is not a known artefact")
    return artefact.identifier


def revision_for_named(identifier: str) -> str:
    """The pinned revision for an artefact named by its Hub identifier.

    For the call sites whose model name is operator-supplied rather than fixed —
    the OmniVoice sidecar takes `--model` on the command line, and the Indic TTS
    description encoder is named by the model it just loaded. Those cannot use a
    fixed key, and the alternative that suggests itself is to fetch at HEAD when
    the name is unrecognised. That is the finding.

    So an unrecorded identifier is REFUSED. An operator pointing the sidecar at a
    different model has to record it here and allowlist it, which is one more
    step and the entire point of MOD-02.
    """
    for artefact in ARTEFACTS.values():
        if artefact.identifier == identifier:
            return revision_for(
                next(k for k, v in ARTEFACTS.items() if v is artefact))
    raise UnpinnedArtefact(
        f"{identifier!r} has no recorded revision. IRA does not fetch a model "
        f"it has not pinned — record it in core/models/artefacts.py and add it "
        f"to the pull allowlist.")


def hub_revision_or_local(name: str) -> Optional[str]:
    """`revision=` for a loader that takes a Hub identifier OR a local folder.

    The memory models are named by configuration (EMBEDDING_MODEL,
    RERANKER_MODEL), and an operator may point either at a folder on disk. A
    folder fetches nothing, so there is nothing to pin: None, which the loaders
    read as "use what is on disk". Any other name is a Hub identifier and gets
    `revision_for_named`'s answer, so an unrecorded one is refused rather than
    fetched at `main`.
    """
    if name and os.path.isdir(name):
        return None
    return revision_for_named(name)


def load_cached_first(load: Callable[..., _Model]) -> _Model:
    """Load a pinned model from this machine's cache; reach the Hub only on a miss.

    `load` is the loader call with everything except `local_files_only`, which
    it passes through: `lambda **cache: SentenceTransformer(name, revision=pin, **cache)`.

    A pinned revision is a commit, whose files cannot change. Loading one the
    usual way still asked huggingface.co about it at every start: eight requests
    for the memory embedder (optional files the repository does not have, its
    file list and its model page), seen on the clean-Linux install of 31 October
    2026 and reproduced on 1 November
    (supracloud-jarvis/docs/testing/evidence/onnxruntime-telemetry-off-2026-11-01/).
    `local_files_only=True` loads the same bytes with no request. When the model
    is not cached (setup's prefetch failed or was skipped) the library raises
    OSError, and the loader runs again without it: the first start still
    fetches, pinned, as before. HF_HUB_OFFLINE=1, huggingface_hub's own switch
    (see .env.example), forbids that fetch too.
    """
    try:
        return load(local_files_only=True)
    except OSError as exc:
        logger.info("model not in this machine's cache (%s); fetching it at its "
                    "pinned revision", type(exc).__name__)
        return load()


async def verify_against_allowlist() -> tuple[str, ...]:
    """Every pin, checked against `model_pull_allowlist`. Returns the problems.

    Empty tuple means every artefact IRA is configured to fetch is allowlisted
    at exactly the revision it would fetch. Anything else is a list of strings
    an operator can act on.

    Called from readiness rather than from the loaders — see the module
    docstring. An artefact that is pinned but not allowlisted is a real and
    likely state during an upgrade: somebody changed the pin and has not yet
    added the allowlist row. Failing readiness makes that a five-second fix
    instead of a lazy-load failure in a worker thread three hours later.
    """
    from core.models import registry

    problems: list[str] = []
    for key, artefact in sorted(ARTEFACTS.items()):
        try:
            revision = revision_for(key)
        except UnpinnedArtefact as exc:
            problems.append(f"{key}: {exc}")
            continue
        try:
            await registry.require_allowlisted(
                artefact.source, artefact.identifier, revision)
        except registry.ArtefactNotAllowlisted as exc:
            problems.append(f"{key}: {exc}")
        except Exception as exc:  # noqa: BLE001
            # An unreadable allowlist is a problem, not a pass. Same reasoning
            # as the gateway's unreadable-registry refusal.
            problems.append(
                f"{key}: the pull allowlist could not be read "
                f"({type(exc).__name__})")
    return tuple(problems)
