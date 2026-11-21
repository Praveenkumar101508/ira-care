"""The memory models and the voice-print encoder are fetched at a pinned revision.

Until 22 October 2026 three loaders fetched from the Hugging Face Hub at `main`:
the memory embedder (`SentenceTransformer`), the memory reranker
(`CrossEncoder`) and the voice-print encoder (SpeechBrain's `from_hparams`),
plus setup's pre-download of the first two. The architecture guard reads the
call sites; these tests run them, with the libraries replaced by recorders, and
check what each loader actually asks for:

  * the revision `core/models/artefacts.py` pins, for the default models;
  * a refusal, with nothing constructed, for a Hub name the table does not
    record;
  * no revision for a local folder, which fetches nothing.

The voice-print encoder must also fail CLOSED: a refused pin leaves no encoder,
so no voice matches the owner.
"""
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import importlib.util
import pathlib
import sys
import types
from types import SimpleNamespace

import pytest

from core.models import artefacts

IRA = pathlib.Path(__file__).resolve().parents[1]
EMBEDDER = artefacts.ARTEFACTS["memory_embedding"]
RERANKER = artefacts.ARTEFACTS["memory_reranker"]
ECAPA = artefacts.ARTEFACTS["speaker_ecapa"]


def _real(relpath: str):
    """The module at `relpath`, loaded from its own file under a private name.

    Not `import memory.embeddings`: other suites replace `sys.modules` entries
    for these modules with fakes and leave them there (tests/test_anticipate.py
    swaps `memory.embeddings` at import time), so in a full run the imported
    name can be somebody's stub. A test of the loader has to hold the loader.
    """
    path = IRA / relpath
    spec = importlib.util.spec_from_file_location(f"_pinned_test_{path.stem}", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    # Registered under its private name: a @dataclass looks its own module up
    # in sys.modules while the class is being built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Recorder:
    """Stands in for a model class: records each construction, loads nothing."""

    calls: list = []

    def __init__(self, name, **kwargs):
        type(self).calls.append((name, kwargs))


def _recorder():
    return type("Recorder", (_Recorder,), {"calls": []})


@pytest.fixture
def st(monkeypatch):
    """The `sentence_transformers` stub, with both classes recording."""
    module = sys.modules.setdefault("sentence_transformers", types.ModuleType("sentence_transformers"))
    st_cls, ce_cls = _recorder(), _recorder()
    monkeypatch.setattr(module, "SentenceTransformer", st_cls, raising=False)
    monkeypatch.setattr(module, "CrossEncoder", ce_cls, raising=False)
    return SimpleNamespace(SentenceTransformer=st_cls, CrossEncoder=ce_cls)


# ── The pin table ─────────────────────────────────────────────────────────────


def test_the_three_are_recorded_at_the_revisions_resolved_from_the_hub():
    assert (EMBEDDER.identifier, EMBEDDER.revision) == (
        "BAAI/bge-large-en-v1.5", "d4aa6901d3a41ba39fb536a557fa166f842b0e09")
    assert (RERANKER.identifier, RERANKER.revision) == (
        "BAAI/bge-reranker-v2-m3", "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e")
    assert (ECAPA.identifier, ECAPA.revision) == (
        "speechbrain/spkrec-ecapa-voxceleb", "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286")


def test_the_config_defaults_are_the_recorded_models():
    """A default that drifted from the table would be refused on every install."""
    from config import Settings

    fields = Settings.model_fields
    assert fields["embedding_model"].default == EMBEDDER.identifier
    assert fields["reranker_model"].default == RERANKER.identifier


def test_a_local_folder_needs_no_revision(tmp_path):
    assert artefacts.hub_revision_or_local(str(tmp_path)) is None


def test_an_unrecorded_hub_name_is_refused():
    with pytest.raises(artefacts.UnpinnedArtefact):
        artefacts.hub_revision_or_local("someone-else/some-embedder")


# ── The memory embedder ───────────────────────────────────────────────────────


def _embeddings(monkeypatch, st, name):
    embeddings = _real("memory/embeddings.py")
    monkeypatch.setattr(embeddings, "SentenceTransformer", st.SentenceTransformer)
    monkeypatch.setattr(embeddings, "_model_instance", None)
    monkeypatch.setattr(embeddings, "get_settings",
                        lambda: SimpleNamespace(embedding_model=name, embedding_device="cpu"))
    return embeddings


def test_the_embedder_loads_at_the_pinned_revision(monkeypatch, st):
    _embeddings(monkeypatch, st, EMBEDDER.identifier)._get_model()
    # local_files_only: from this machine's cache first (load_cached_first).
    assert st.SentenceTransformer.calls == [
        (EMBEDDER.identifier, {"device": "cpu", "revision": EMBEDDER.revision,
                               "local_files_only": True})]


def test_an_unrecorded_embedder_is_refused_before_anything_is_fetched(monkeypatch, st):
    embeddings = _embeddings(monkeypatch, st, "someone-else/some-embedder")
    with pytest.raises(artefacts.UnpinnedArtefact):
        embeddings._get_model()
    assert st.SentenceTransformer.calls == []


def test_an_embedder_in_a_local_folder_loads_from_it(monkeypatch, st, tmp_path):
    _embeddings(monkeypatch, st, str(tmp_path))._get_model()
    assert st.SentenceTransformer.calls == [
        (str(tmp_path), {"device": "cpu", "revision": None, "local_files_only": True})]


# ── The memory reranker ───────────────────────────────────────────────────────


def _reranker(monkeypatch, st, name):
    try:
        import torch  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(nn=SimpleNamespace(Sigmoid=lambda: "sigmoid")))
    reranker = _real("memory/reranker.py")
    monkeypatch.setattr(reranker, "CrossEncoder", st.CrossEncoder)
    monkeypatch.setattr(reranker, "_model_instance", None)
    monkeypatch.setattr(reranker, "get_settings",
                        lambda: SimpleNamespace(reranker_model=name, reranker_device="cpu"))
    return reranker


def test_the_reranker_loads_at_the_pinned_revision(monkeypatch, st):
    _reranker(monkeypatch, st, RERANKER.identifier)._get_model()
    [(name, kwargs)] = st.CrossEncoder.calls
    assert name == RERANKER.identifier
    assert kwargs["revision"] == RERANKER.revision


def test_an_unrecorded_reranker_is_refused_before_anything_is_fetched(monkeypatch, st):
    reranker = _reranker(monkeypatch, st, "someone-else/some-reranker")
    with pytest.raises(artefacts.UnpinnedArtefact):
        reranker._get_model()
    assert st.CrossEncoder.calls == []


# ── The local-only semantic matcher ───────────────────────────────────────────



# ── The voice-print encoder ───────────────────────────────────────────────────


@pytest.fixture
def speechbrain(monkeypatch):
    """SpeechBrain replaced by recorders: what `from_hparams` and `FetchConfig`
    were given, and nothing downloaded."""
    calls = []

    class FetchConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class EncoderClassifier:
        @classmethod
        def from_hparams(cls, **kwargs):
            calls.append(kwargs)
            return "encoder"

    speaker = types.ModuleType("speechbrain.inference.speaker")
    speaker.EncoderClassifier = EncoderClassifier
    fetching = types.ModuleType("speechbrain.utils.fetching")
    fetching.FetchConfig = FetchConfig
    for name, module in (("speechbrain", types.ModuleType("speechbrain")),
                         ("speechbrain.inference", types.ModuleType("speechbrain.inference")),
                         ("speechbrain.inference.speaker", speaker),
                         ("speechbrain.utils", types.ModuleType("speechbrain.utils")),
                         ("speechbrain.utils.fetching", fetching)):
        monkeypatch.setitem(sys.modules, name, module)

    biometrics = _real("voice/biometrics.py")
    yield SimpleNamespace(calls=calls, biometrics=biometrics)


def test_the_voice_print_encoder_is_fetched_at_the_pinned_revision(speechbrain):
    assert speechbrain.biometrics._get_encoder() == "encoder"
    [kwargs] = speechbrain.calls
    assert kwargs["source"] == ECAPA.identifier
    # One FetchConfig covers hyperparams.yaml AND every checkpoint the
    # pretrainer collects, so the pin has to be on it, not beside it.
    assert kwargs["fetch_config"].kwargs == {"revision": ECAPA.revision}
    # SpeechBrain reuses a file already in `savedir` by name, whatever revision
    # asked for it, so each revision needs its own folder or an old one is served.
    assert pathlib.Path(kwargs["savedir"]).name == ECAPA.revision


def test_a_changed_pin_is_fetched_into_its_own_folder(speechbrain, monkeypatch):
    other = "1" * 40
    monkeypatch.setitem(artefacts.ARTEFACTS, "speaker_ecapa",
                        artefacts.Artefact(identifier=ECAPA.identifier, revision=other))
    speechbrain.biometrics._get_encoder()
    [kwargs] = speechbrain.calls
    assert kwargs["fetch_config"].kwargs == {"revision": other}
    assert pathlib.Path(kwargs["savedir"]).name == other


def test_a_refused_voice_print_pin_fails_closed_without_fetching(speechbrain, monkeypatch):
    monkeypatch.setitem(artefacts.ARTEFACTS, "speaker_ecapa",
                        artefacts.Artefact(identifier=ECAPA.identifier, revision="main"))
    assert speechbrain.biometrics._get_encoder() is None
    assert speechbrain.calls == []


# ── Setup's pre-download ──────────────────────────────────────────────────────


def _prefetch():
    return _real("scripts/prefetch_models.py")


def test_setup_caches_both_models_at_their_pinned_revisions(st):
    rc = _prefetch().main(["--embedder", EMBEDDER.identifier, "--reranker", RERANKER.identifier])
    assert rc == 0
    assert st.SentenceTransformer.calls == [
        (EMBEDDER.identifier, {"device": "cpu", "revision": EMBEDDER.revision})]
    assert st.CrossEncoder.calls == [
        (RERANKER.identifier, {"device": "cpu", "revision": RERANKER.revision})]


def test_setup_refuses_an_unrecorded_model_and_says_so(st, capsys):
    assert _prefetch().main(["--embedder", "someone-else/some-embedder"]) == 1
    assert st.SentenceTransformer.calls == []
    assert "refused" in capsys.readouterr().err


def test_neither_setup_script_fetches_a_model_inline():
    """Both used to construct the model in an inline Python string, at `main`;
    setup-native.sh also pasted the .env value into that string."""
    native = (IRA.parent / "scripts" / "setup-native.sh").read_text(encoding="utf-8")
    windows = (IRA.parent / "scripts" / "setup-windows.ps1").read_text(encoding="utf-8")
    for name, text in (("setup-native.sh", native), ("setup-windows.ps1", windows)):
        assert "prefetch_models.py" in text, f"{name} no longer pre-downloads through the pinned script"
        for loader in ("SentenceTransformer(", "CrossEncoder(", "SentenceTransformer as"):
            assert loader not in text, f"{name} constructs a model inline again: {loader!r}"
    # A native command that exits non-zero does not throw in PowerShell, so the
    # result must come from the exit code.
    assert "$LASTEXITCODE" in windows.split("prefetch_models.py", 1)[1].split("Write-Ok", 1)[0]
