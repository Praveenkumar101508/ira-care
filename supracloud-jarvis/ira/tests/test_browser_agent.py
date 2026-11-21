"""Browser agent — unit tests (no playwright, no network, no LLM).

Covers: flight-param extraction (LLM mocked), the read-only click blocklist,
structural guards (lazy playwright import, single click site, SSRF delegation),
timeout handling, voice formatting, URL builders, IATA resolution, the
enabled-flag kill switch, and the provider fallback chain.
"""
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import ast
import asyncio
import inspect
from datetime import date

import pytest

import agents.browser_agent as ba
import utils.browser_agent_tools as bat


@pytest.fixture(autouse=True)
def _egress_allowed(monkeypatch):
    """Browsing now needs the egress master switch as well as its own flag.

    These tests exercise the fallback chain and the capability kill switch, not
    the egress gate (that is covered in test_assist_browse.py), so allow egress
    here and let each test drive BROWSER_AGENT_ENABLED as it always has.
    """
    monkeypatch.setenv("IRA_ALLOW_EXTERNAL_API", "true")


# ── Helpers ───────────────────────────────────────────────────────────────────

class FakeLocator:
    """Stands in for a Playwright locator; records whether click was awaited."""

    def __init__(self, text: str = "", aria: str | None = None):
        self._text = text
        self._aria = aria
        self.clicked = False

    async def inner_text(self):
        return self._text

    async def get_attribute(self, name):
        return self._aria if name == "aria-label" else None

    async def click(self, timeout=None):
        self.clicked = True


def _settings_stub(monkeypatch, **overrides):
    """Patch config.get_settings (and the agent's lazy import of it)."""
    import config as config_mod

    real = config_mod.get_settings()

    class _Stub:
        def __getattr__(self, name):
            if name in overrides:
                return overrides[name]
            return getattr(real, name)

    monkeypatch.setattr(config_mod, "get_settings", lambda: _Stub())
    return _Stub()


# ── Flight param extraction ───────────────────────────────────────────────────

async def test_extract_params_valid_json(monkeypatch):
    async def fake_llm(messages, **kw):
        return ('{"origin": "London", "destination": "Hyderabad", '
                '"depart_date": "2026-08-05", "return_date": "2026-08-19"}')
    monkeypatch.setattr(ba, "chat_complete", fake_llm)
    p = await ba._extract_flight_params("find me flights from London to Hyderabad next month")
    assert p == {"origin": "London", "destination": "Hyderabad",
                 "depart_date": "2026-08-05", "return_date": "2026-08-19"}


async def test_extract_params_fenced_json(monkeypatch):
    async def fake_llm(messages, **kw):
        return ('```json\n{"origin": null, "destination": "Hyderabad", '
                '"depart_date": "2026-08-05", "return_date": null}\n```')
    monkeypatch.setattr(ba, "chat_complete", fake_llm)
    p = await ba._extract_flight_params("flights to Hyderabad")
    assert p["origin"] == "London"        # null origin defaults to London
    assert p["destination"] == "Hyderabad"


async def test_extract_params_garbage_falls_back(monkeypatch):
    async def fake_llm(messages, **kw):
        return "I'm sorry, I can't help with that."
    monkeypatch.setattr(ba, "chat_complete", fake_llm)
    p = await ba._extract_flight_params(
        "flights from Manchester to Hyderabad", today=date(2026, 7, 11))
    assert p["origin"].lower() == "manchester"     # regex fallback
    assert p["destination"].lower() == "hyderabad"
    assert p["depart_date"].startswith("2026-08")  # next month default


async def test_extract_params_llm_down_defaults(monkeypatch):
    async def fake_llm(messages, **kw):
        raise ConnectionError("LLM unreachable")
    monkeypatch.setattr(ba, "chat_complete", fake_llm)
    p = await ba._extract_flight_params("flights to Hyderabad next month",
                                        today=date(2026, 7, 11))
    assert p["origin"] == "London"
    assert p["destination"].lower() == "hyderabad"
    assert p["depart_date"] == "2026-08-05"


def test_next_month_defaults_december_rollover():
    depart, ret = ba._next_month_defaults(date(2026, 12, 15))
    assert depart == "2027-01-05" and ret == "2027-01-19"


# ── Read-only click guard ─────────────────────────────────────────────────────

@pytest.mark.parametrize("text,aria", [
    ("Book now", None),
    ("Buy tickets", None),
    ("Continue to payment", None),
    ("", "Sign in"),
    ("Accept all and pay", None),   # consent word + blocklist word → refuse
    ("Submit", None),
    ("", ""),                       # unlabeled element → refuse
    ("Read more", None),            # not consent-related → refuse
])
async def test_safe_click_refuses(text, aria):
    loc = FakeLocator(text, aria)
    assert await bat._safe_click(loc) is False
    assert loc.clicked is False, f"clicked element labelled {text!r}/{aria!r}"


@pytest.mark.parametrize("text", ["Reject all", "Decline", "Accept all", "I understand"])
async def test_safe_click_allows_consent(text):
    loc = FakeLocator(text)
    assert await bat._safe_click(loc) is True
    assert loc.clicked is True


# ── Structural guards ─────────────────────────────────────────────────────────

def test_no_module_level_playwright_import():
    """requirements-test.txt excludes playwright — the module must import lazily."""
    tree = ast.parse(inspect.getsource(bat))
    for node in tree.body:  # module level only
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = getattr(node, "module", "") or ""
            names += " ".join(a.name for a in node.names)
            assert "playwright" not in names, "playwright must be lazily imported"


def test_click_only_inside_safe_click():
    """Read-only guarantee: _safe_click is the single click site."""
    src = inspect.getsource(bat)
    click_lines = [ln for ln in src.splitlines() if ".click(" in ln]
    safe_src = inspect.getsource(bat._safe_click)
    for ln in click_lines:
        assert ln.strip() in safe_src, f".click( outside _safe_click: {ln.strip()}"


def test_delegates_to_net_safety():
    src = inspect.getsource(bat)
    assert "net_safety" in src, "browser_agent_tools must route URLs through utils.net_safety"
    assert "gethostbyname" not in src


def test_urls_are_ssrf_checked_before_fetch():
    src = inspect.getsource(bat._fetch_page_text)
    assert "check_url" in src


# ── Timeout / error handling in the agent node ────────────────────────────────

def _base_state(query: str) -> dict:
    return {
        "messages": [], "session_id": "s1", "conversation_id": "c1",
        "user_query": query, "active_agent": "browser", "use_deep_model": False,
        "mode": "assistant", "memory_context": "", "final_response": "",
        "stream_tokens": [], "latency_ms": 0, "model_used": "", "is_voice": True,
        "user_id": "tester", "error": None, "is_owner": True,
        "clearance_level": "admin",
    }


async def test_timeout_returns_graceful_message(monkeypatch):
    _settings_stub(monkeypatch, browser_agent_enabled=True, browser_task_timeout_s=0.05)

    async def slow_search(**kw):
        await asyncio.sleep(2)
    monkeypatch.setattr(ba, "search_flights", slow_search)

    async def fake_params(q, today=None):
        return {"origin": "London", "destination": "Hyderabad",
                "depart_date": "2026-08-05", "return_date": None}
    monkeypatch.setattr(ba, "_extract_flight_params", fake_params)

    out = await ba.browser_agent(_base_state("flights to Hyderabad"))
    assert out["final_response"] == ba._TIMEOUT_SPOKEN
    assert "Traceback" not in out["final_response"]


async def test_tool_exception_never_leaks_stack_trace(monkeypatch):
    _settings_stub(monkeypatch, browser_agent_enabled=True, browser_task_timeout_s=5)

    async def exploding_lookup(q):
        raise RuntimeError("chromium exploded at 0xDEADBEEF")
    monkeypatch.setattr(ba, "browser_lookup", exploding_lookup)

    out = await ba.browser_agent(_base_state("look up the weather in Hyderabad"))
    assert out["final_response"] == ba._ERROR_SPOKEN
    assert "DEADBEEF" not in out["final_response"]
    assert "Traceback" not in out["final_response"]


async def test_disabled_flag_short_circuits(monkeypatch):
    _settings_stub(monkeypatch, browser_agent_enabled=False)
    called = False

    async def must_not_run(**kw):
        nonlocal called
        called = True
    monkeypatch.setattr(ba, "search_flights", must_not_run)

    out = await ba.browser_agent(_base_state("flights to Hyderabad"))
    assert out["final_response"] == ba._DISABLED_SPOKEN
    assert called is False


# ── Voice formatting ──────────────────────────────────────────────────────────

def test_voice_format_strips_and_truncates():
    ugly = ("**Great news!** Check [this](https://example.com/deal) out. " +
            "word " * 120 + "END.")
    out = bat._voice_format(ugly, max_words=60)
    assert len(out.split()) <= 60
    for bad in ("*", "#", "[", "]", "http"):
        assert bad not in out


def test_voice_format_short_text_untouched():
    assert bat._voice_format("Cheapest is Air India at 412 pounds.") == \
        "Cheapest is Air India at 412 pounds."


def test_voice_format_speaks_currency_symbols():
    out = bat._voice_format("IndiGo at £333, United at $1,200, Lufthansa at €450.")
    assert out == "IndiGo at 333 pounds, United at 1,200 dollars, Lufthansa at 450 euros."


def test_compose_flight_summary_speakable():
    options = [
        {"airline": "Air India", "price": "412 pounds", "duration": "11h", "stops": "1 stop"},
        {"airline": "British Airways", "price": "630 pounds", "duration": "9h", "stops": "direct"},
    ]
    text = bat.compose_flight_summary(options, {"origin": "London", "destination": "Hyderabad"})
    assert "Air India" in text and "412 pounds" in text
    assert len(text.split()) <= 60
    assert "http" not in text and "*" not in text


def test_compose_flight_summary_empty_is_graceful():
    assert bat.compose_flight_summary([], {}) == bat.NO_RESULTS_SPOKEN


# ── URL builders / IATA ───────────────────────────────────────────────────────

def test_build_kayak_url_return():
    assert bat.build_kayak_url("LON", "HYD", "2026-08-05", "2026-08-19") == \
        "https://www.kayak.co.uk/flights/LON-HYD/2026-08-05/2026-08-19?sort=bestflight_a"


def test_build_kayak_url_one_way():
    assert bat.build_kayak_url("lon", "hyd", "2026-08-05") == \
        "https://www.kayak.co.uk/flights/LON-HYD/2026-08-05?sort=bestflight_a"


def test_build_skyscanner_url():
    assert bat.build_skyscanner_url("LON", "HYD", "2026-08-05", "2026-08-19") == \
        "https://www.skyscanner.net/transport/flights/lon/hyd/260805/260819/"


def test_build_ddg_url_encodes():
    assert bat.build_ddg_url("flights london to hyderabad") == \
        "https://html.duckduckgo.com/html/?q=flights+london+to+hyderabad"


def test_build_bing_url_encodes():
    assert bat.build_bing_url("flights london to hyderabad") == \
        "https://www.bing.com/search?q=flights+london+to+hyderabad"


def test_ddg_block_page_detected():
    assert bat._looks_bot_blocked(
        "If this persists, please email us. Our support email address includes "
        "an anonymized error code that helps us understand the context."
    )


async def test_city_code_static():
    assert await bat._city_code("London") == "LON"
    assert await bat._city_code("  hyderabad ") == "HYD"
    assert await bat._city_code("BOM") == "BOM"   # already a code


async def test_city_code_llm_fallback(monkeypatch):
    import utils.llm

    async def fake_llm(messages, **kw):
        return " XYZ \n"
    monkeypatch.setattr(utils.llm, "chat_complete", fake_llm)
    assert await bat._city_code("Xyzville") == "XYZ"

    async def garbage_llm(messages, **kw):
        return "The IATA code for Xyzville is XYZ."
    monkeypatch.setattr(utils.llm, "chat_complete", garbage_llm)
    assert await bat._city_code("Xyzville") is None


# ── Provider fallback chain ───────────────────────────────────────────────────

async def test_fallback_chain_reaches_duckduckgo(monkeypatch):
    async def fake_codes(city):
        return {"london": "LON", "hyderabad": "HYD"}.get(city.strip().lower())
    monkeypatch.setattr(bat, "_city_code", fake_codes)

    async def fake_fetch(url, settle_ms=0):
        if "duckduckgo" in url:
            return "Flights from London to Hyderabad from £412 with Air India, 1 stop, 11h."
        raise TimeoutError("blocked")
    monkeypatch.setattr(bat, "_fetch_page_text", fake_fetch)

    async def fake_extract(text, o, d):
        return [{"airline": "Air India", "price": "412 pounds",
                 "duration": "11h", "stops": "1 stop"}]
    monkeypatch.setattr(bat, "_llm_extract_options", fake_extract)

    result = await bat.search_flights("London", "Hyderabad", "2026-08-05", "2026-08-19")
    assert result["source"] == "duckduckgo"
    assert len(result["options"]) == 1


async def test_all_providers_fail_graceful(monkeypatch):
    async def fake_codes(city):
        return "LON" if "london" in city.lower() else "HYD"
    monkeypatch.setattr(bat, "_city_code", fake_codes)

    async def always_blocked(url, settle_ms=0):
        raise TimeoutError("blocked")
    monkeypatch.setattr(bat, "_fetch_page_text", always_blocked)

    result = await bat.search_flights("London", "Hyderabad", "2026-08-05")
    assert result["error"] == "no_results"
    assert result["spoken"] == bat.NO_RESULTS_SPOKEN


async def test_bot_blocked_page_falls_through(monkeypatch):
    async def fake_codes(city):
        return "LON" if "london" in city.lower() else "HYD"
    monkeypatch.setattr(bat, "_city_code", fake_codes)

    calls = []

    async def fake_fetch(url, settle_ms=0):
        calls.append(url)
        if "kayak" in url:
            return "Please verify you are a human. Press & hold."
        raise TimeoutError("blocked")
    monkeypatch.setattr(bat, "_fetch_page_text", fake_fetch)

    result = await bat.search_flights("London", "Hyderabad", "2026-08-05")
    assert result.get("error") == "no_results"
    assert any("kayak" in u for u in calls)
    assert any("duckduckgo" in u for u in calls)   # chain continued past the block
