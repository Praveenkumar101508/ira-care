"""reasoning/capacity.py — the runtime inference-capacity record (D §6 step 1).

What these pin:
  - the memory rule: a tier is admitted only when its MEASURED resident footprint
    fits within MEMORY_SHARE of the memory available now
  - the speed rule: with a probe result, the projected tokens/s must meet the
    tier floor; without one, speed is "unknown" and admission is memory-only,
    and the record says so
  - the probe reads Ollama's own eval timings (eval_count / eval_duration in ns)
    and reports None, never a number, when they are missing
  - the probe is OFF unless IRA_CAPACITY_PROBE is set, is cached, and a failing
    probe is recorded, not raised
  - the Ollama URL scheme is checked before any request is made
  - the trust status carries the record and stays fail-soft when it breaks
"""
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import asyncio

import pytest

from reasoning import capacity as cap

GIB = 1024 ** 3


@pytest.fixture(autouse=True)
def _fresh_cache():
    cap.reset_probe_cache()
    yield
    cap.reset_probe_cache()


def _mem(total_gib, avail_gib):
    return lambda: (int(total_gib * GIB), int(avail_gib * GIB))


def _no_vram():
    return None


# ── admission: memory rule ──────────────────────────────────────────────────

def test_fast_tier_fits_an_eight_gib_machine_with_six_available():
    rec = cap.capacity_record({}, memory=_mem(8, 6.9), vram=_no_vram)
    fast = next(t for t in rec["tiers"] if t["tier"] == "fast")
    assert fast["memory"]["ok"] is True
    assert fast["admitted"] is True
    assert fast["admission_basis"] == "memory only; speed unknown"


def test_the_advanced_tier_is_refused_on_a_sixteen_gib_machine():
    rec = cap.capacity_record({}, memory=_mem(16, 14.8), vram=_no_vram)
    adv = next(t for t in rec["tiers"] if t["tier"] == "advanced")
    assert adv["memory"]["ok"] is False
    assert adv["admitted"] is False
    assert adv["admission_basis"] == "refused on memory"
    assert "27.4 GiB" in adv["memory"]["reason"]
    assert rec["highest_admitted_tier"] == "balanced"


def test_the_advanced_tier_is_admitted_with_forty_gib_available():
    rec = cap.capacity_record({}, memory=_mem(64, 40.0), vram=_no_vram)
    assert rec["highest_admitted_tier"] == "advanced"


def test_memory_share_is_applied_not_the_raw_available_figure():
    # 6.0 GiB available: 70% is 4.2 GiB, below the 4.7 GiB the fast tier needs.
    rec = cap.capacity_record({}, memory=_mem(8, 6.0), vram=_no_vram)
    fast = next(t for t in rec["tiers"] if t["tier"] == "fast")
    assert fast["memory"]["budget_gib"] == 4.2
    assert fast["admitted"] is False
    assert rec["highest_admitted_tier"] is None


def test_unreadable_memory_is_unknown_not_admitted():
    rec = cap.capacity_record({}, memory=lambda: (None, None), vram=_no_vram)
    assert rec["ram_total_gib"] is None
    assert all(t["memory"]["ok"] is None for t in rec["tiers"])
    assert all(t["admitted"] is False for t in rec["tiers"])
    assert rec["highest_admitted_tier"] is None


# ── admission: speed rule ───────────────────────────────────────────────────

def _post_with(eval_count, eval_duration_ns):
    def post(url, body, timeout):
        assert url.endswith("/api/generate")
        assert body["stream"] is False and body["options"]["num_predict"] == cap.PROBE_TOKENS
        return {"eval_count": eval_count, "eval_duration": eval_duration_ns, "load_duration": 1}
    return post


def test_probe_result_projects_speed_for_every_tier():
    # 32 tokens in 2.5 s -> 12.8 tok/s on the fast model.
    env = {cap.PROBE_ENV: "1", "OLLAMA_BASE_URL": "http://localhost:11434/v1"}
    rec = cap.capacity_record(env, post=_post_with(32, 2_500_000_000), memory=_mem(64, 40), vram=_no_vram)
    assert rec["probe"]["tokens_per_second"] == 12.8
    by = {t["tier"]: t for t in rec["tiers"]}
    assert by["fast"]["speed"]["projected_tps"] == 12.8 and by["fast"]["speed"]["ok"] is True
    assert by["balanced"]["speed"]["projected_tps"] == round(12.8 * 0.59, 2)
    assert by["advanced"]["speed"]["projected_tps"] == round(12.8 * 1.12, 2)
    assert by["advanced"]["admission_basis"] == "memory and speed"


def test_a_slow_probe_refuses_the_fast_tier_on_speed_even_with_memory_to_spare():
    # 32 tokens in 8 s -> 4 tok/s, below the fast floor of 8.
    env = {cap.PROBE_ENV: "1"}
    rec = cap.capacity_record(env, post=_post_with(32, 8_000_000_000), memory=_mem(64, 40), vram=_no_vram)
    fast = next(t for t in rec["tiers"] if t["tier"] == "fast")
    assert fast["memory"]["ok"] is True
    assert fast["speed"]["ok"] is False
    assert fast["admitted"] is False
    # the 30B-A3B class projects 4.48 tok/s against a 2 tok/s floor and passes
    adv = next(t for t in rec["tiers"] if t["tier"] == "advanced")
    assert adv["admitted"] is True


# ── the probe itself ────────────────────────────────────────────────────────

def test_tokens_per_second_uses_ollama_nanoseconds():
    assert cap.tokens_per_second({"eval_count": 100, "eval_duration": 10_000_000_000}) == 10.0


@pytest.mark.parametrize("resp", [
    {}, {"eval_count": 0, "eval_duration": 5}, {"eval_count": 5, "eval_duration": 0},
    {"eval_count": "x", "eval_duration": 1}, None,
])
def test_missing_or_zero_timings_give_none_not_a_number(resp):
    assert cap.tokens_per_second(resp) is None


def test_probe_is_off_by_default():
    calls = []
    def post(url, body, timeout):
        calls.append(url)
        return {"eval_count": 32, "eval_duration": 1_000_000_000}
    rec = cap.capacity_record({}, post=post, memory=_mem(16, 10), vram=_no_vram)
    assert rec["probe_enabled"] is False and rec["probe"] is None and calls == []


def test_a_failing_probe_is_recorded_not_raised():
    def post(url, body, timeout):
        raise ConnectionError("Ollama is down")
    rec = cap.capacity_record({cap.PROBE_ENV: "1"}, post=post, memory=_mem(16, 10), vram=_no_vram)
    assert rec["probe"]["tokens_per_second"] is None
    assert "Ollama is down" in rec["probe"]["error"]
    assert all(t["speed"]["ok"] is None for t in rec["tiers"])


def test_probe_result_is_cached_for_the_ttl():
    calls = []
    def post(url, body, timeout):
        calls.append(1)
        return {"eval_count": 32, "eval_duration": 4_000_000_000}
    first = cap.cached_probe("qwen3:4b", "http://localhost:11434", post=post, now=1000.0)
    second = cap.cached_probe("qwen3:4b", "http://localhost:11434", post=post, now=1000.0 + cap.PROBE_TTL_S - 1)
    third = cap.cached_probe("qwen3:4b", "http://localhost:11434", post=post, now=1000.0 + cap.PROBE_TTL_S + 1)
    assert first["cached"] is False and second["cached"] is True and third["cached"] is False
    assert len(calls) == 2


def test_the_probe_model_and_host_come_from_the_environment():
    seen = {}
    def post(url, body, timeout):
        seen["url"], seen["model"] = url, body["model"]
        return {"eval_count": 32, "eval_duration": 1_000_000_000}
    env = {cap.PROBE_ENV: "1", cap.PROBE_MODEL_ENV: "qwen3:1.7b", "OLLAMA_BASE_URL": "http://ollama:11434/v1/"}
    cap.capacity_record(env, post=post, memory=_mem(16, 10), vram=_no_vram)
    assert seen == {"url": "http://ollama:11434/api/generate", "model": "qwen3:1.7b"}


def test_the_real_poster_refuses_a_non_network_scheme():
    with pytest.raises(ValueError, match="http/https only"):
        cap._default_post("file:///etc/passwd", {}, 1.0)


# ── the trust status carries it, fail-soft ──────────────────────────────────

def test_trust_status_carries_the_record_and_survives_its_failure(monkeypatch):
    pytest.importorskip("fastapi")
    import api.routes.trust as trust

    class _Cfg:
        dev_mode = False
        ira_secret_key = "a-real-secret"
        ira_admin_password = "a-real-password"
        ira_allow_external_api = False
        ira_require_api_consent = True
        ira_privacy_mode = "local_first"
        web_search_enabled = False
        llm_backend = "ollama"
        ollama_base_url = "http://localhost:11434/v1"
        ira_model_profile = "balanced_local"
        postgres_host = "localhost"
        embedding_model = "BAAI/bge-large-en-v1.5"
        embedding_device = "cpu"

    monkeypatch.setattr(trust, "get_settings", lambda: _Cfg())
    monkeypatch.setattr(trust, "is_owner", lambda user: True)
    monkeypatch.setattr(trust, "cortex_local_only_warning", lambda: None)
    monkeypatch.setattr(trust, "research_backends_warning", lambda: None)

    async def _none():
        return None
    monkeypatch.setattr(trust, "_voice_enrolled", _none)
    monkeypatch.setattr(trust, "_last_login", _none)
    monkeypatch.setattr(trust, "_pending_actions", lambda: 0)

    out = asyncio.run(trust.trust_status(_user="owner"))
    assert set(out["inference_capacity"]) >= {"ram_total_gib", "ram_available_gib", "cpu_threads",
                                              "accelerator", "tiers", "highest_admitted_tier"}
    assert out["inference_capacity"]["probe_enabled"] is False  # nothing loaded a model
    assert out["status"] == "local_only"  # informational: never degrades the posture

    def boom():
        raise RuntimeError("meminfo exploded")
    monkeypatch.setattr("reasoning.capacity.capacity_record", boom)
    out = asyncio.run(trust.trust_status(_user="owner"))
    assert out["inference_capacity"] is None
    assert out["status"] == "local_only"
