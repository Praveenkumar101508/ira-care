"""The reranker is off unless the owner turns it on (10 November 2026).

`config.py` defaulted `reranker_enabled` to True while `.env.example` ships
`RERANKER_ENABLED=false`; an install without that line loaded a 2.2 GB
cross-encoder at startup next to the chat model on an 8 GB Mac. The code default
now matches the example file, which this test pins in both directions.
"""
from __future__ import annotations

import os
import pathlib

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")


def test_the_code_default_is_off_and_matches_env_example(monkeypatch):
    monkeypatch.delenv("RERANKER_ENABLED", raising=False)
    from config import Settings
    assert Settings.model_fields["reranker_enabled"].default is False
    example = (pathlib.Path(__file__).resolve().parents[2] / ".env.example").read_text()
    assert "RERANKER_ENABLED=false" in example


def test_the_owner_can_still_turn_it_on(monkeypatch):
    monkeypatch.setenv("RERANKER_ENABLED", "true")
    from config import Settings
    assert Settings().reranker_enabled is True
