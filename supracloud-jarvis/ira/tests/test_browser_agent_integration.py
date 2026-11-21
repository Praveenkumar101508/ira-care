"""Browser agent — real-Chromium integration test (opt-in).

requirements-test.txt deliberately excludes playwright, so this file is
skipped unless BOTH conditions hold:
  1. playwright is importable (pip install playwright)
  2. RUN_BROWSER_IT=1 is set in the environment

Run:  playwright install chromium
      RUN_BROWSER_IT=1 python -m pytest tests/test_browser_agent_integration.py -q

Exercises the real search-engine chain (DuckDuckGo HTML → Bing) with a real
Chromium: passes if at least one engine returns usable page text. Makes no
LLM calls.
"""
import importlib.util
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("playwright") is None
    or os.environ.get("RUN_BROWSER_IT") != "1",
    reason="requires playwright installed and RUN_BROWSER_IT=1",
)


async def test_search_engine_chain_returns_text():
    from utils.browser_agent_tools import (
        _SEARCH_ENGINES,
        _fetch_page_text,
        _looks_bot_blocked,
        shutdown_browser_session,
    )

    results: dict[str, str] = {}
    try:
        for engine, builder in _SEARCH_ENGINES:
            url = builder("flights london to hyderabad")
            try:
                text = await _fetch_page_text(url, settle_ms=2_500)
            except Exception as e:
                if "Executable doesn't exist" in str(e):
                    pytest.skip("chromium not installed — run: playwright install chromium")
                results[engine] = f"ERROR: {e}"
                continue
            if _looks_bot_blocked(text):
                results[engine] = "bot-blocked"
                continue
            results[engine] = f"ok ({len(text)} chars)"
            if len(text) > 500:
                return  # at least one engine yields real content — chain works
        pytest.fail(f"every search engine failed or bot-blocked: {results}")
    finally:
        await shutdown_browser_session()
