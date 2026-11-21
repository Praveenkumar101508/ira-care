"""`/health` probes the model engine this install runs.

On `main` at e193eba5 `/health` always probed the two vLLM URLs. A native install
runs Ollama (`LLM_BACKEND=ollama`) and leaves those URLs at their Docker defaults,
`http://vllm-fast:8001/v1` and `http://vllm-deep:8002/v1`. So a healthy native
install answered `"status": "degraded"`, and each probe sent the vLLM key, as a
bearer token, to whatever host answered to those names.

The probes are replaced here: the question is which ones run and what the
endpoint concludes, not whether a real server answers.
"""
from __future__ import annotations

import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import pytest

import api.routes.health as hmod
from api.routes.health import ServiceStatus


class _Cfg:
    ira_version = "test"
    vllm_fast_url = "http://vllm-fast:8001/v1"
    vllm_deep_url = "http://vllm-deep:8002/v1"
    vllm_api_key = "SYNTHETIC-vllm-key"
    ollama_base_url = "http://127.0.0.1:11434/v1"
    dev_mode = False

    def __init__(self, backend):
        self.llm_backend = backend


@pytest.fixture
def probes(monkeypatch):
    calls: list[tuple] = []

    def _ok(name):
        async def probe(*args):
            calls.append((name, args))
            return ServiceStatus(status="ok", latency_ms=1)
        return probe

    monkeypatch.setattr(hmod, "_check_postgres", _ok("postgres"))
    monkeypatch.setattr(hmod, "_check_redis", _ok("redis"))
    monkeypatch.setattr(hmod, "_check_vllm", _ok("vllm"))
    monkeypatch.setattr(hmod, "_check_ollama", _ok("ollama"))
    return calls


def _use(monkeypatch, backend):
    cfg = _Cfg(backend)
    monkeypatch.setattr(hmod, "get_settings", lambda: cfg)
    monkeypatch.setattr("utils.llm.get_settings", lambda: cfg)


async def test_a_native_install_probes_ollama_and_never_the_vllm_hosts(monkeypatch, probes):
    _use(monkeypatch, "ollama")
    body = await hmod.health()
    names = [c[0] for c in probes]
    assert "vllm" not in names, f"a native install probed vLLM: {probes}"
    assert names.count("ollama") == 1
    assert body.status == "ok"
    assert set(body.services) == {"postgres", "redis", "ollama"}


async def test_the_vllm_key_is_not_sent_anywhere_on_a_native_install(monkeypatch, probes):
    _use(monkeypatch, "ollama")
    await hmod.health()
    assert not any("SYNTHETIC-vllm-key" in map(str, c[1]) for c in probes)


async def test_a_vllm_install_still_probes_both_vllm_hosts(monkeypatch, probes):
    _use(monkeypatch, "vllm")
    body = await hmod.health()
    vllm = [c[1][0] for c in probes if c[0] == "vllm"]
    assert vllm == ["http://vllm-fast:8001/v1", "http://vllm-deep:8002/v1"]
    assert set(body.services) == {"postgres", "redis", "vllm_fast", "vllm_deep"}


async def test_a_down_model_engine_makes_the_install_degraded(monkeypatch, probes):
    _use(monkeypatch, "ollama")

    async def down(*args):
        return ServiceStatus(status="down", latency_ms=0)

    monkeypatch.setattr(hmod, "_check_ollama", down)
    body = await hmod.health()
    assert body.status == "degraded"
