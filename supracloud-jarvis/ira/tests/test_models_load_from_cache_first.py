"""The memory models load from this machine's cache; a start reaches the Hub
only while a model is not cached yet.

At every start the embedding warm-up loaded BAAI/bge-large-en-v1.5 at its pinned
commit and still sent eight requests to huggingface.co (optional files the
repository does not have, its file list, its model page). Seen on the
clean-Linux install of 31 October 2026 (pull request #190) and reproduced with
the same library versions on 1 November: with the cache full, the usual load
made eight requests and `local_files_only=True` made none; with the cache empty,
`local_files_only=True` raised OSError and the usual load fetched. Evidence:
supracloud-jarvis/docs/testing/evidence/onnxruntime-telemetry-off-2026-11-01/.

sentence-transformers is not installed in the test environment, so these tests
record the calls IRA makes; the evidence above is the library's side.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.models import artefacts
from core.models.artefacts import load_cached_first

IRA_DIR = Path(__file__).resolve().parents[1]


def _real(relpath: str):
    """A fresh copy of an IRA module, so the conftest stubs of its imports
    stay as they are for everything else."""
    path = IRA_DIR / relpath
    spec = importlib.util.spec_from_file_location(f"_cached_first_{path.stem}", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Loader:
    """Stands in for a model class. `miss=True` makes a cache-only load raise
    the OSError the library raises when the files are not cached."""

    def __init__(self, miss: bool = False, error: Exception | None = None):
        self.calls: list[dict] = []
        self.miss, self.error = miss, error

    def __call__(self, name, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("local_files_only") and self.error is not None:
            raise self.error
        if kwargs.get("local_files_only") and self.miss:
            raise OSError("not in the local cache")
        return SimpleNamespace(name=name, kwargs=kwargs)


# ── The helper ───────────────────────────────────────────────────────────────

def test_a_cached_model_loads_with_no_hub_request():
    load = _Loader()

    model = load_cached_first(lambda **cache: load("m", revision="abc", **cache))

    assert load.calls == [{"revision": "abc", "local_files_only": True}]
    assert model.kwargs["local_files_only"] is True


def test_a_model_not_cached_yet_is_still_fetched_at_its_pin():
    """The first start after a setup whose prefetch failed must still work."""
    load = _Loader(miss=True)

    model = load_cached_first(lambda **cache: load("m", revision="abc", **cache))

    assert load.calls == [{"revision": "abc", "local_files_only": True}, {"revision": "abc"}]
    assert model.kwargs == {"revision": "abc"}


def test_an_error_other_than_a_cache_miss_is_not_retried_as_a_fetch():
    load = _Loader(error=RuntimeError("out of memory"))

    with pytest.raises(RuntimeError):
        load_cached_first(lambda **cache: load("m", revision="abc", **cache))
    assert load.calls == [{"revision": "abc", "local_files_only": True}]


# ── The two memory loaders use it ────────────────────────────────────────────

@pytest.fixture
def st(monkeypatch):
    module = sys.modules.setdefault("sentence_transformers",
                                    types.ModuleType("sentence_transformers"))
    st_load, ce_load = _Loader(miss=True), _Loader(miss=True)
    monkeypatch.setattr(module, "SentenceTransformer", st_load, raising=False)
    monkeypatch.setattr(module, "CrossEncoder", ce_load, raising=False)
    return SimpleNamespace(SentenceTransformer=st_load, CrossEncoder=ce_load)


def test_the_embedder_tries_the_cache_first_then_fetches_at_the_pin(monkeypatch, st):
    embeddings = _real("memory/embeddings.py")
    monkeypatch.setattr(embeddings, "_model_instance", None)
    name = "BAAI/bge-large-en-v1.5"
    monkeypatch.setattr(embeddings, "get_settings",
                        lambda: SimpleNamespace(embedding_model=name, embedding_device="cpu"))
    pin = artefacts.hub_revision_or_local(name)

    embeddings._get_model()

    assert st.SentenceTransformer.calls == [
        {"device": "cpu", "revision": pin, "local_files_only": True},
        {"device": "cpu", "revision": pin},
    ]


def test_the_reranker_tries_the_cache_first_then_fetches_at_the_pin(monkeypatch, st):
    try:
        import torch  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "torch",
                            SimpleNamespace(nn=SimpleNamespace(Sigmoid=lambda: "sigmoid")))
    reranker = _real("memory/reranker.py")
    monkeypatch.setattr(reranker, "_model_instance", None)
    name = "BAAI/bge-reranker-v2-m3"
    monkeypatch.setattr(reranker, "get_settings",
                        lambda: SimpleNamespace(reranker_model=name, reranker_device="cpu"))
    pin = artefacts.hub_revision_or_local(name)

    reranker._get_model()

    first, second = st.CrossEncoder.calls
    assert first["local_files_only"] is True and first["revision"] == pin
    assert "local_files_only" not in second and second["revision"] == pin


# ── The strict switch is offered, not shipped on ─────────────────────────────

def test_the_template_offers_hf_hub_offline_but_does_not_turn_it_on():
    """HF_HUB_OFFLINE=1 stops every Hub download, including the first-use
    downloads of the voice models that setup does not prefetch, so turning it
    on is the owner's choice, not the template's."""
    text = (IRA_DIR.parent / ".env.example").read_text(encoding="utf-8")

    assert not re.search(r"^\s*HF_HUB_OFFLINE\s*=", text, re.M), (
        ".env.example turns HF_HUB_OFFLINE on: a fresh install could not download "
        "the models it has not cached yet")
    assert re.search(r"^#\s*HF_HUB_OFFLINE=1\s*$", text, re.M), (
        ".env.example no longer documents the switch that forbids Hub downloads")
