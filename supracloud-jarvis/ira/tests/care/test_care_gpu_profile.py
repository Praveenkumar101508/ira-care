"""The GPU profile (D-074): deploy/compose.gpu.yml puts the model server on the GPU, keeps both
models loaded and makes gemma4:12b first, without changing the CPU stack."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

DEPLOY = Path(__file__).resolve().parents[4] / "deploy"


def _default(value: str, env: dict[str, str]) -> str:
    """Resolve ${NAME:-default} the way docker compose does, from `env`."""
    return re.sub(r"\$\{(\w+):-([^}]*)\}", lambda m: env.get(m.group(1)) or m.group(2), value)


def test_the_gpu_profile_uses_the_gpu_keeps_both_models_and_puts_the_12b_first():
    gpu = yaml.safe_load((DEPLOY / "compose.gpu.yml").read_text())["services"]
    assert gpu["ollama"]["environment"]["OLLAMA_MAX_LOADED_MODELS"] == "2"
    device = gpu["ollama"]["deploy"]["resources"]["reservations"]["devices"][0]
    assert device["driver"] == "nvidia" and device["capabilities"] == ["gpu"]
    # A .env written for the CPU names its own models; they must not carry onto the GPU.
    cpu_env = {"IRA_CARE_MODEL_PRIMARY": "gemma4:e4b", "IRA_CARE_MODEL_SECONDARY": "gemma4:e2b"}
    for service in ("api", "worker"):
        env = gpu[service]["environment"]
        assert _default(env["IRA_CARE_MODEL_PRIMARY"], cpu_env) == "gemma4:12b"
        assert _default(env["IRA_CARE_MODEL_SECONDARY"], cpu_env) == "gemma4:e4b"
    assert _default(gpu["ollama-pull"]["command"][0], cpu_env) == "ollama pull gemma4:12b && ollama pull gemma4:e4b"


def test_the_cpu_stack_is_unchanged_without_the_gpu_file():
    base = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())["services"]
    assert base["ollama"]["environment"]["OLLAMA_MAX_LOADED_MODELS"] == "1"
    assert "deploy" not in base["ollama"]
    assert _default(base["api"]["environment"]["IRA_CARE_MODEL_PRIMARY"], {}) == "gemma4:e4b"
