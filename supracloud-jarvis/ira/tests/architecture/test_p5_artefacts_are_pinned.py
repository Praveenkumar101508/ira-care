"""Phase 5 batch 03 — no model artefact is fetched at a moving reference.

`BASELINE_MODEL_INVENTORY.md` §8, finding MOD-02:

    `voice/tts_indic.py` calls `from_pretrained()` **without revision pinning**
    (Bandit B615, ×2) — it fetches whatever the Hub serves at that moment.

Bandit found two of them. There were four, across three files, and a linter that
counts occurrences cannot tell whether the fix stayed. So this reads the tree's
AST and requires every `from_pretrained` call to pass a `revision`.

── Why the AST and not a grep ──────────────────────────────────────────────

`grep 'from_pretrained' | grep -v revision` is one line and wrong in both
directions: it misses a call split across lines, and it matches the sentence in
this docstring. The parser knows the difference between a call and a mention.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

IRA_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Loader methods that fetch a remote artefact. Each takes `revision=`.
#:
#: `WhisperModel` was missing, and the gap was real rather than theoretical:
#: `voice/stt.py` constructed `WhisperModel("distil-large-v3")`, which
#: faster-whisper resolves to a Hub repository at `main` — an unpinned fetch
#: this suite could not see because it only knew the transformers and
#: huggingface_hub spellings. It went unnoticed because faster-whisper was not
#: installed on the native path; the day it was, the hole was live.
#:
#: The second gap was the same shape. sentence-transformers' `SentenceTransformer`
#: and `CrossEncoder` (memory embeddings and reranker) and SpeechBrain's
#: `from_hparams` (the voice-print encoder) all fetched at `main` until 22
#: October 2026, unseen because this set only knew the spellings above.
#: SpeechBrain takes its pin inside `fetch_config=FetchConfig(revision=...)`,
#: which `_revision_value` reads.
FETCHERS = frozenset({"from_pretrained", "snapshot_download", "hf_hub_download",
                      "WhisperModel", "SentenceTransformer", "CrossEncoder",
                      "from_hparams"})


def _revision_value(node: ast.Call):
    """The expression a fetcher call pins its revision with, or None.

    `revision=` on the call itself, or, for SpeechBrain, the `revision=` of a
    `FetchConfig(...)` passed as `fetch_config=`: one config there covers the
    hyperparameters file and every checkpoint the pretrainer collects.
    """
    for kw in node.keywords:
        if kw.arg == "revision":
            return kw.value
    for kw in node.keywords:
        value = kw.value
        if (kw.arg == "fetch_config" and isinstance(value, ast.Call)
                and getattr(value.func, "id", getattr(value.func, "attr", None)) == "FetchConfig"):
            for inner in value.keywords:
                if inner.arg == "revision":
                    return inner.value
    return None


def _calls():
    """Every fetcher call in the application tree, with its file and line."""
    for path in sorted(IRA_ROOT.rglob("*.py")):
        s = str(path)
        if "__pycache__" in s or "/tests/" in s:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else getattr(func, "id", None))
            if name in FETCHERS:
                yield path, node


def test_every_remote_artefact_fetch_pins_a_revision():
    """The finding, as a guard.

    An unpinned fetch is not a warning: it means the bytes IRA loads today and
    the bytes it loads tomorrow can differ with nothing recording that they did.
    """
    unpinned = []
    for path, node in _calls():
        if _revision_value(node) is None:
            unpinned.append(f"{path.relative_to(IRA_ROOT)}:{node.lineno}")
    assert not unpinned, (
        "these calls fetch a model artefact without pinning a revision, so "
        "they load whatever the source serves at that moment — MOD-02:\n  "
        + "\n  ".join(unpinned))


def test_no_call_site_pins_a_literal_revision():
    """A hash typed at the call site is pinned and unmaintainable.

    Four call sites with four literals is four places to update and four ways to
    disagree with the pull allowlist — and `verify_against_allowlist` checks the
    table, not the call site, so a drifted literal would pass readiness and
    fetch the wrong artefact.
    """
    literals = []
    for path, node in _calls():
        value = _revision_value(node)
        if isinstance(value, ast.Constant):
            literals.append(
                f"{path.relative_to(IRA_ROOT)}:{node.lineno} "
                f"pins {value.value!r} inline")
    assert not literals, (
        "revisions belong in core/models/artefacts.py, which is what "
        "verify_against_allowlist checks against:\n  " + "\n  ".join(literals))


def test_the_three_sites_the_finding_named_are_covered():
    """MOD-02 names `voice/tts_indic.py`. If a refactor moved these calls
    somewhere this test stopped looking, the two tests above would pass by
    finding nothing — the shape of an integrity check that cannot fail."""
    found = {str(p.relative_to(IRA_ROOT)) for p, _ in _calls()}
    for expected in ("voice/tts_indic.py", "voice/omnivoice_sidecar.py"):
        assert expected in found, (
            f"{expected} no longer contains a model fetch. If that is "
            f"deliberate, remove it from this list; if it moved, this suite is "
            f"no longer looking where the artefacts are fetched.")
    assert len(list(_calls())) >= 4, (
        "fewer than four fetcher calls found; MOD-02 counted four")


def test_the_whisper_loader_is_covered():
    """Non-vacuity for the `WhisperModel` entry above. If the STT loader moved
    or was renamed, the pin tests would pass by finding nothing in it."""
    found = {str(p.relative_to(IRA_ROOT)) for p, node in _calls()
             if getattr(node.func, "id", None) == "WhisperModel"
             or getattr(node.func, "attr", None) == "WhisperModel"}
    assert "voice/stt.py" in found, (
        "voice/stt.py no longer constructs WhisperModel where this suite can "
        "see it. If the loader moved, follow it; an unseen fetch is unpinned "
        "until proven otherwise.")


def test_the_memory_and_voice_print_loaders_are_covered():
    """Non-vacuity for the three loader names added on 22 October 2026. If one
    of these files stopped constructing its model where this suite looks, the
    pin tests above would pass by finding nothing in it."""
    names = {}
    for path, node in _calls():
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        names.setdefault(str(path.relative_to(IRA_ROOT)), set()).add(name)
    for expected, loader in (("memory/embeddings.py", "SentenceTransformer"),
                             ("memory/reranker.py", "CrossEncoder"),
                             ("voice/biometrics.py", "from_hparams"),
                             ("scripts/prefetch_models.py", "SentenceTransformer"),
                             ("scripts/prefetch_models.py", "CrossEncoder")):
        assert loader in names.get(expected, set()), (
            f"{expected} no longer calls {loader} where this suite can see it. "
            f"If the loader moved, follow it; an unseen fetch is unpinned until "
            f"proven otherwise.")


def test_a_speechbrain_pin_is_read_from_its_fetch_config():
    """`_revision_value` reads `FetchConfig(revision=...)`, and only that.
    Checked on synthetic calls so the rule is pinned down whatever the tree
    holds today."""
    def call(src):
        return ast.parse(src).body[0].value

    assert _revision_value(call("E.from_hparams(source=s, fetch_config=FetchConfig(revision=r))")) is not None
    assert _revision_value(call("E.from_hparams(source=s, fetch_config=FetchConfig())")) is None
    assert _revision_value(call("E.from_hparams(source=s)")) is None
    assert _revision_value(call("E.from_hparams(source=s, other=FetchConfig(revision=r))")) is None
    literal = _revision_value(call("E.from_hparams(source=s, fetch_config=FetchConfig(revision='abc'))"))
    assert isinstance(literal, ast.Constant)


# ─────────────────────────────────────────────────────────────────────────────
# The pin table itself
# ─────────────────────────────────────────────────────────────────────────────


def test_every_recorded_revision_looks_like_a_commit():
    from core.models import artefacts

    for key, artefact in artefacts.ARTEFACTS.items():
        assert len(artefact.revision) == 40 and all(
            c in "0123456789abcdef" for c in artefact.revision), (
            f"{key} is pinned to {artefact.revision!r}, which is not a commit "
            f"hash. A tag can be moved; a branch always moves.")


@pytest.mark.parametrize("moving", ["main", "latest", "HEAD", "master", ""])
def test_a_moving_reference_is_refused_rather_than_returned(monkeypatch, moving):
    """The override path is where a pin quietly stops pinning: an operator sets
    `IRA_INDIC_TTS_REVISION=main` to work around a fetch failure and the control
    is gone with no error anywhere."""
    from core.models import artefacts

    monkeypatch.setitem(
        artefacts.ARTEFACTS, "indic_tts",
        artefacts.Artefact(identifier="ai4bharat/indic-parler-tts",
                           revision=moving))
    with pytest.raises(artefacts.UnpinnedArtefact) as exc:
        artefacts.revision_for("indic_tts")
    assert "pin" in str(exc.value).lower()


def test_an_unrecorded_model_name_is_refused(monkeypatch):
    """The sidecar takes `--model` from the command line. An unrecognised name
    must be refused, not fetched at HEAD — that path is how an operator-supplied
    string would have become an arbitrary pull."""
    from core.models import artefacts

    with pytest.raises(artefacts.UnpinnedArtefact):
        artefacts.revision_for_named("someone-else/whatever")


def test_a_recorded_model_name_resolves(monkeypatch):
    from core.models import artefacts

    assert artefacts.revision_for_named("ai4bharat/indic-parler-tts") == \
        artefacts.ARTEFACTS["indic_tts"].revision
