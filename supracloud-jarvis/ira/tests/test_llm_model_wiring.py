"""M1 + M4 — the model router is wired into the live Ollama path (utils.llm).

Proves that utils.llm resolves each Ollama tier (and the vision model) through the
smart model-selection layer, with a fail-safe fallback to the legacy config so the
existing chat flow is never broken.
"""
import pytest

from reasoning import model_availability as ma
from reasoning.model_availability import ModelAvailability
from reasoning.model_profiles import reload_config
from utils import llm as L


class FakeCfg:
    ollama_model_fast = "qwen3:8b"
    ollama_model_deep = "qwen3:14b"
    ollama_model_reasoning = "qwen3:14b"
    ollama_vision_model = "qwen2.5vl"
    ira_use_model_router = True


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    reload_config()
    cfg = FakeCfg()
    monkeypatch.setattr(L, "get_settings", lambda: cfg)
    yield cfg
    reload_config()


def _avail(*names, reachable=True):
    return ModelAvailability(reachable=reachable, models=frozenset(names), source="test")


def test_router_off_uses_legacy_models(_setup, monkeypatch):
    _setup.ira_use_model_router = False
    assert L.resolve_ollama_model("reasoning") == "qwen3:14b"
    assert L.resolve_ollama_model("deep") == "qwen3:14b"
    assert L.resolve_ollama_model("fast") == "qwen3:8b"


def test_router_on_unreachable_prefers_profile(monkeypatch):
    # No Ollama probe -> optimistic -> the profile's preferred model is used.
    monkeypatch.setattr(ma, "get_availability", lambda **k: _avail(reachable=False))
    assert L.resolve_ollama_model("reasoning") == "deepseek-r1:14b"   # the upgrade
    assert L.resolve_ollama_model("deep") == "qwen3:14b"
    assert L.resolve_ollama_model("fast") == "qwen3:8b"


def test_router_on_missing_reasoning_falls_back_to_main(monkeypatch):
    # deepseek not installed -> falls back to local_main (qwen3:14b) == legacy behaviour.
    monkeypatch.setattr(ma, "get_availability", lambda **k: _avail("qwen3:14b", "qwen3:8b"))
    assert L.resolve_ollama_model("reasoning") == "qwen3:14b"


def test_env_override_changes_live_model(monkeypatch):
    monkeypatch.setattr(ma, "get_availability", lambda **k: _avail(reachable=False))
    monkeypatch.setenv("IRA_LOCAL_FAST_MODEL", "my-fast:latest")
    assert L.resolve_ollama_model("fast") == "my-fast:latest"


def test_router_on_whole_chain_missing_uses_the_installed_configured_model(_setup, monkeypatch):
    # A machine set up with a smaller model: none of the profile's chain
    # (qwen3:8b/14b, gemma3n:e4b) is installed, OLLAMA_MODEL_* = qwen3:4b is.
    # Before the fix every tier resolved to gemma3n:e4b and chat failed.
    for tier in ("fast", "deep", "reasoning"):
        setattr(_setup, f"ollama_model_{tier}", "qwen3:4b")
    monkeypatch.setattr(ma, "get_availability", lambda **k: _avail("qwen3:4b"))
    for tier in ("fast", "deep", "reasoning"):
        assert L.resolve_ollama_model(tier) == "qwen3:4b"


def test_router_on_installed_profile_model_still_wins(_setup, monkeypatch):
    _setup.ollama_model_fast = "qwen3:4b"
    monkeypatch.setattr(ma, "get_availability", lambda **k: _avail("qwen3:8b", "qwen3:4b"))
    assert L.resolve_ollama_model("fast") == "qwen3:8b"


def test_router_on_nothing_installed_keeps_the_router_answer(_setup, monkeypatch):
    # Neither is installed: no better answer exists; keep the router's own.
    _setup.ollama_model_fast = "qwen3:4b"
    monkeypatch.setattr(ma, "get_availability", lambda **k: _avail("llama3:8b"))
    assert L.resolve_ollama_model("fast") == "gemma3n:e4b"


def test_vision_prefers_profile_when_installed(monkeypatch):
    monkeypatch.setattr(ma, "get_availability", lambda **k: _avail("gemma3:12b", "qwen2.5vl"))
    assert L.resolve_ollama_vision_model() == "gemma3:12b"


def test_vision_keeps_legacy_when_profile_missing(monkeypatch):
    # gemma3:12b not installed but the legacy vision model is -> don't break vision.
    monkeypatch.setattr(ma, "get_availability", lambda **k: _avail("qwen2.5vl"))
    assert L.resolve_ollama_vision_model() == "qwen2.5vl"


def test_vision_never_falls_back_to_text_model(monkeypatch):
    # Only text models installed -> never return a text model for vision.
    monkeypatch.setattr(ma, "get_availability", lambda **k: _avail("qwen3:14b", "qwen3:8b"))
    chosen = L.resolve_ollama_vision_model()
    assert chosen not in {"qwen3:14b", "qwen3:8b"}


def test_resolution_never_raises(monkeypatch):
    def boom(**k):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(ma, "get_availability", boom)
    # Any failure must degrade to the legacy config value, not raise.
    assert L.resolve_ollama_model("fast") == "qwen3:8b"
    assert L.resolve_ollama_vision_model() == "qwen2.5vl"
