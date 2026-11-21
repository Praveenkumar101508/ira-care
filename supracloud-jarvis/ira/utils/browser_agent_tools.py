"""
IRA Browser Agent tools — read-only flight search + generic web lookup.

search_flights(origin, destination, depart_date, return_date) → Kayak URL
pattern → Skyscanner URL pattern → DuckDuckGo HTML fallback, extracting up
to 3 options (airline, price, duration, stops).
browser_lookup(query) → DuckDuckGo HTML search, optionally follows the top
result, returns a 2-3 sentence spoken-style summary.

READ-ONLY GUARANTEE: the only permitted browser actions are navigate, wait,
extract text, and screenshot. Every click goes through _safe_click, which
refuses any element whose text/aria-label matches the booking/payment/auth
blocklist. Cookie-consent banners are the sole click target (reject-first).
All URLs pass the consolidated SSRF guard (utils.net_safety.check_url)
before the browser navigates.

Unlike utils.browser_tools (per-call launch), this module keeps ONE Chromium
instance alive across calls (lazy singleton) and opens a fresh, isolated
context per task, always closed in ``finally``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import quote_plus

from utils.net_safety import check_url

logger = logging.getLogger("ira.browser_agent_tools")

_MAX_PAGE_TEXT = 8_000  # characters fed to LLM

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Spoken-style message when every provider fails or blocks us.
NO_RESULTS_SPOKEN = (
    "I couldn't get live prices right now — the sites are blocking automated "
    "access. Want me to try again or open the search in your browser?"
)


# ── City → IATA ───────────────────────────────────────────────────────────────

_CITY_TO_IATA: dict[str, str] = {
    "london": "LON", "hyderabad": "HYD", "new york": "NYC", "delhi": "DEL",
    "mumbai": "BOM", "bangalore": "BLR", "bengaluru": "BLR", "chennai": "MAA",
    "kolkata": "CCU", "paris": "PAR", "amsterdam": "AMS", "frankfurt": "FRA",
    "dubai": "DXB", "abu dhabi": "AUH", "doha": "DOH", "singapore": "SIN",
    "hong kong": "HKG", "tokyo": "TYO", "sydney": "SYD", "melbourne": "MEL",
    "los angeles": "LAX", "san francisco": "SFO", "chicago": "CHI",
    "boston": "BOS", "toronto": "YTO", "vancouver": "YVR", "manchester": "MAN",
    "birmingham": "BHX", "edinburgh": "EDI", "glasgow": "GLA", "dublin": "DUB",
    "madrid": "MAD", "barcelona": "BCN", "rome": "ROM", "milan": "MIL",
    "berlin": "BER", "munich": "MUC", "zurich": "ZRH", "vienna": "VIE",
    "istanbul": "IST", "bangkok": "BKK", "kuala lumpur": "KUL", "seoul": "SEL",
    "beijing": "BJS", "shanghai": "SHA", "lisbon": "LIS", "athens": "ATH",
    "cairo": "CAI", "johannesburg": "JNB", "nairobi": "NBO",
    "ahmedabad": "AMD", "kochi": "COK", "goa": "GOI", "pune": "PNQ",
}


async def _city_code(city: str) -> str | None:
    """Resolve a city name to an IATA city code: static map, then LLM fallback."""
    key = city.strip().lower()
    if key in _CITY_TO_IATA:
        return _CITY_TO_IATA[key]
    # Already a 3-letter code?
    if re.fullmatch(r"[A-Za-z]{3}", key):
        return key.upper()
    try:
        from utils.llm import chat_complete
        raw = await chat_complete(
            [{
                "role": "user",
                "content": (
                    f"What is the 3-letter IATA city code for {city.strip()}? "
                    "Reply with ONLY the 3-letter code, nothing else."
                ),
            }],
            use_deep=False,
            temperature=0.0,
            # No max_tokens: thinking models (qwen3) spend their budget on the
            # hidden reasoning phase and return empty content if capped low.
        )
        code = raw.strip().upper()
        if re.fullmatch(r"[A-Z]{3}", code):
            return code
    except Exception as e:
        logger.warning("IATA lookup via LLM failed for %r: %s", city, e)
    return None


# ── URL builders (pure) ───────────────────────────────────────────────────────

def build_kayak_url(orig: str, dest: str, depart: str, ret: str | None = None) -> str:
    """Kayak UK flight-results URL built directly from params (no UI driving)."""
    base = f"https://www.kayak.co.uk/flights/{orig.upper()}-{dest.upper()}/{depart}"
    if ret:
        base += f"/{ret}"
    return base + "?sort=bestflight_a"


def build_skyscanner_url(orig: str, dest: str, depart: str, ret: str | None = None) -> str:
    """Skyscanner flight-results URL. Dates are YYMMDD, codes lowercase."""
    def _yymmdd(iso: str) -> str:
        return iso.replace("-", "")[2:]

    url = (
        f"https://www.skyscanner.net/transport/flights/"
        f"{orig.lower()}/{dest.lower()}/{_yymmdd(depart)}/"
    )
    if ret:
        url += f"{_yymmdd(ret)}/"
    return url


def build_ddg_url(query: str) -> str:
    """Static (JS-free) DuckDuckGo HTML search."""
    return f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"


def build_bing_url(query: str) -> str:
    """Bing web search — fallback when DuckDuckGo bot-blocks headless browsers."""
    return f"https://www.bing.com/search?q={quote_plus(query)}"


# Privacy-preferred order: DuckDuckGo first, Bing only when DDG blocks us.
_SEARCH_ENGINES = (
    ("duckduckgo", build_ddg_url),
    ("bing", build_bing_url),
)


# ── Read-only click guard ─────────────────────────────────────────────────────

# Any element whose text or aria-label contains one of these is NEVER clicked.
_CLICK_BLOCKLIST = (
    "book", "buy", "pay", "purchase", "checkout", "check out",
    "sign in", "sign up", "log in", "login", "register", "subscribe",
    "submit", "apply", "add to", "continue to payment", "select fare",
    "select this", "order",
)

# Cookie-consent labels we ARE allowed to click — reject-variants first.
_CONSENT_ALLOWLIST = (
    "reject all", "reject", "decline", "refuse", "necessary only",
    "only essential", "essential only", "manage without",
    "accept all", "accept", "agree", "got it", "i understand", "ok",
)


async def _safe_click(locator) -> bool:
    """The ONLY place this module clicks anything.

    Reads the element's text and aria-label; refuses (returns False) unless the
    label matches the consent allowlist and matches no booking/payment/auth
    blocklist term. Enforced structurally by tests: `.click(` must not appear
    anywhere else in this file.
    """
    try:
        text = ((await locator.inner_text()) or "").strip().lower()
    except Exception:
        text = ""
    try:
        aria = ((await locator.get_attribute("aria-label")) or "").strip().lower()
    except Exception:
        aria = ""
    label = f"{text} {aria}".strip()
    if not label:
        return False
    if any(term in label for term in _CLICK_BLOCKLIST):
        logger.warning("Blocked click on element labelled %r (read-only guard)", label[:80])
        return False
    if not any(term in label for term in _CONSENT_ALLOWLIST):
        return False
    try:
        await locator.click(timeout=2_000)
        return True
    except Exception:
        return False


async def _dismiss_cookie_banner(page) -> None:
    """Best-effort consent dismissal; prefers reject over accept; never raises."""
    selectors = [
        'button:has-text("Reject all")',
        'button:has-text("Reject")',
        'button:has-text("Decline")',
        '[aria-label*="reject" i]',
        'button:has-text("Accept all")',
        'button:has-text("Accept")',
        'button:has-text("I understand")',
        'button:has-text("OK")',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            if not await loc.is_visible(timeout=1_000):
                continue
            if await _safe_click(loc):
                logger.info("Dismissed consent banner via %s", sel)
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue


# ── Browser session singleton ─────────────────────────────────────────────────

_session_lock = asyncio.Lock()
_pw = None       # playwright driver handle
_browser = None  # shared Chromium instance


async def _get_browser(headless: bool = True):
    """Return the shared Chromium instance, (re)launching it if needed.

    Raises ImportError if playwright isn't installed — callers convert that
    into a friendly error dict.
    """
    global _pw, _browser
    from playwright.async_api import async_playwright

    async with _session_lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        if _pw is None:
            _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(
            headless=headless,
            args=[
                # Same rationale as browser_tools.py Fix #40: --no-sandbox is
                # required when Chromium runs as root inside Docker.
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                # Reduce trivial headless fingerprinting (navigator.webdriver)
                "--disable-blink-features=AutomationControlled",
            ],
        )
        logger.info("Browser agent: launched shared Chromium (headless=%s)", headless)
        return _browser


async def shutdown_browser_session() -> None:
    """Close the shared browser + driver (demo script / tests cleanup)."""
    global _pw, _browser
    async with _session_lock:
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:
                pass
            _browser = None
        if _pw is not None:
            try:
                await _pw.stop()
            except Exception:
                pass
            _pw = None


def _clean_text(raw: str) -> str:
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"[\t ]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


async def _fetch_page_text(url: str, *, settle_ms: int = 0) -> str:
    """Navigate the shared browser to `url` (SSRF-guarded) and return page text.

    Read-only: navigate → dismiss consent → wait → extract innerText.
    Raises on navigation failure; callers catch and fall through.
    """
    # Egress ceiling first: fetching a public page sends the query and this
    # machine's IP off the device, so IRA_ALLOW_EXTERNAL_API governs it as well
    # as BROWSER_AGENT_ENABLED. This is the single navigation chokepoint, so
    # every browsing path is covered here rather than per caller.
    from utils.external_gate import require_browsing
    require_browsing()

    ok, reason = check_url(url)
    if not ok:
        raise ValueError(f"URL blocked for security reasons: {reason}")

    from config import get_settings
    browser = await _get_browser(headless=get_settings().browser_headless)
    context = await browser.new_context(user_agent=_USER_AGENT, locale="en-GB")
    try:
        page = await context.new_page()
        # NOTE: no request interception here — Bing (and others) detect
        # route-aborted requests and serve an empty results shell, so we accept
        # the extra bandwidth in exchange for real content.
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await _dismiss_cookie_banner(page)
        if settle_ms:
            # Result pages hydrate via JS after DOMContentLoaded
            await page.wait_for_timeout(settle_ms)
        raw = await page.evaluate("() => document.body.innerText || ''")
        return _clean_text(raw)[:_MAX_PAGE_TEXT]
    finally:
        await context.close()


# ── Flight-option extraction ──────────────────────────────────────────────────

_PRICE_RE = re.compile(r"£\s?(\d[\d,]*)")

_BOT_BLOCK_MARKERS = (
    "are you a human", "unusual traffic", "verify you are", "captcha",
    "access denied", "robot check", "press & hold",
    # DuckDuckGo's anomaly-detection page (seen with headless Chromium)
    "anonymized error code", "unexpected error. please try again",
)


def _looks_bot_blocked(page_text: str) -> bool:
    low = page_text.lower()
    return any(m in low for m in _BOT_BLOCK_MARKERS)


async def _llm_extract_options(page_text: str, origin: str, destination: str) -> list[dict]:
    """LLM-extract up to 3 flight options from raw page text; [] on failure."""
    from utils.llm import chat_complete
    prompt = (
        f"Below is raw text from a flight search results page for "
        f"{origin} to {destination}.\n---\n{page_text[:6000]}\n---\n"
        'Extract up to 3 flight options as a JSON array of objects with keys '
        '"airline", "price", "duration", "stops". Use short values, e.g. '
        '{"airline": "Air India", "price": "412 pounds", "duration": "11h 20m", '
        '"stops": "1 stop"}. If no flight options are present, reply with [].\n'
        "Reply with ONLY the JSON array."
    )
    try:
        raw = await chat_complete(
            [{"role": "user", "content": prompt}],
            use_deep=False,
            temperature=0.0,
        )
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        options = json.loads(raw)
        if isinstance(options, list):
            return [o for o in options if isinstance(o, dict) and o.get("price")][:3]
    except Exception as e:
        logger.warning("LLM flight-option extraction failed: %s", e)
    # Regex fallback: at least surface prices found on the page
    prices = _PRICE_RE.findall(page_text)[:3]
    return [{"airline": "unknown", "price": f"{p} pounds", "duration": "", "stops": ""}
            for p in prices]


_BROWSING_BLOCKED_SPOKEN = (
    "Browsing is switched off right now, so I didn't look anything up. "
    "Turn on external access if you'd like me to."
)


def _browsing_blocked() -> dict | None:
    """An honest error dict when the egress gate forbids browsing, else None.

    Checked upfront so a blocked call reports the real reason instead of the
    generic "couldn't reach the search engines" fallback.
    """
    from utils.external_gate import BROWSING_BLOCKED_DETAIL, browsing_allowed

    if browsing_allowed():
        return None
    return {"error": "browsing_blocked", "detail": BROWSING_BLOCKED_DETAIL,
            "spoken": _BROWSING_BLOCKED_SPOKEN}


async def search_flights(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: str | None = None,
) -> dict:
    """Search flights via Kayak → Skyscanner → DuckDuckGo; read-only.

    Returns {"options": [...], "source": ..., "params": {...}} or a dict with
    "error" and a graceful "spoken" message. Never raises.
    """
    blocked = _browsing_blocked()
    if blocked:
        return {**blocked, "params": {
            "origin": origin, "destination": destination,
            "depart_date": depart_date, "return_date": return_date,
        }}

    params = {
        "origin": origin, "destination": destination,
        "depart_date": depart_date, "return_date": return_date,
    }

    try:
        orig_code = await _city_code(origin)
        dest_code = await _city_code(destination)
    except Exception:
        orig_code = dest_code = None

    attempts: list[tuple[str, str, int]] = []
    if orig_code and dest_code:
        attempts.append(("kayak", build_kayak_url(orig_code, dest_code, depart_date, return_date), 8_000))
        attempts.append(("skyscanner", build_skyscanner_url(orig_code, dest_code, depart_date, return_date), 8_000))
    search_q = f"flights {origin} to {destination} {depart_date} price"
    for engine, builder in _SEARCH_ENGINES:
        attempts.append((engine, builder(search_q), 2_500))

    try:
        for source, url, settle_ms in attempts:
            try:
                page_text = await _fetch_page_text(url, settle_ms=settle_ms)
            except ImportError:
                return {
                    "error": (
                        "Playwright not installed. "
                        "Run: pip install playwright && playwright install chromium"
                    ),
                    "spoken": "My browsing tools aren't set up yet on this machine.",
                    "params": params,
                }
            except Exception as e:
                logger.warning("search_flights: %s failed (%s): %s", source, url[:80], e)
                continue

            if not page_text or _looks_bot_blocked(page_text):
                logger.warning("search_flights: %s returned empty/bot-blocked page", source)
                continue

            options = await _llm_extract_options(page_text, origin, destination)
            if options:
                logger.info("search_flights: %d option(s) from %s", len(options), source)
                return {"options": options, "source": source, "params": params}
    except Exception as e:
        logger.error("search_flights: unexpected failure: %s", e)

    return {"error": "no_results", "spoken": NO_RESULTS_SPOKEN, "params": params}


# ── Generic web lookup ────────────────────────────────────────────────────────

_DDG_RESULT_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


async def browser_lookup(query: str) -> dict:
    """Read-only web lookup: DuckDuckGo HTML search, optionally follow the top
    result, summarize to 2-3 spoken-style sentences. Never raises."""
    blocked = _browsing_blocked()
    if blocked:
        return blocked
    try:
        serp_text = ""
        for engine, builder in _SEARCH_ENGINES:
            try:
                candidate_text = await _fetch_page_text(builder(query), settle_ms=2_500)
            except ImportError:
                return {
                    "error": (
                        "Playwright not installed. "
                        "Run: pip install playwright && playwright install chromium"
                    ),
                    "spoken": "My browsing tools aren't set up yet on this machine.",
                }
            except Exception as e:
                logger.warning("browser_lookup: %s failed: %s", engine, e)
                continue
            if candidate_text and not _looks_bot_blocked(candidate_text):
                serp_text = candidate_text
                break
            logger.warning("browser_lookup: %s empty/bot-blocked, trying next engine", engine)

        if not serp_text:
            return {"error": "no_results",
                    "spoken": "I couldn't reach the search engines just now. Want me to try again?"}

        # Optionally follow the first organic result for richer content.
        followed_url: str | None = None
        page_text = ""
        for candidate in _DDG_RESULT_URL_RE.findall(serp_text)[:3]:
            if "duckduckgo.com" in candidate:
                continue
            ok, _reason = check_url(candidate)
            if not ok:
                continue
            try:
                page_text = await _fetch_page_text(candidate)
                followed_url = candidate
                break
            except Exception:
                continue

        corpus = page_text or serp_text
        from utils.llm import chat_complete
        summary = await chat_complete(
            [{
                "role": "user",
                "content": (
                    f'The user asked: "{query}"\n\n'
                    f"Web content:\n---\n{corpus[:6000]}\n---\n\n"
                    "Answer in 2-3 short spoken sentences, as if talking aloud. "
                    "No markdown, no bullet points, no URLs."
                ),
            }],
            use_deep=False,
            temperature=0.2,
        )
        return {"summary": _voice_format(summary), "source_url": followed_url}
    except Exception as e:
        logger.error("browser_lookup failed for %r: %s", query, e)
        return {"error": str(e),
                "spoken": "I hit a snag while looking that up. Let's try again in a moment."}


# ── Voice-first formatting (pure) ─────────────────────────────────────────────

def _voice_format(text: str, max_words: int = 60) -> str:
    """Make text safe to speak: strip markdown/URLs, speak currency symbols,
    cap at ~max_words preferring a sentence boundary."""
    text = re.sub(r"https?://\S+", "", text)
    _amount = r"(\d(?:[\d,]*\d)?(?:\.\d+)?)"  # ends on a digit — leaves punctuation alone
    text = re.sub(rf"£\s?{_amount}", r"\1 pounds", text)
    text = re.sub(rf"\$\s?{_amount}", r"\1 dollars", text)
    text = re.sub(rf"€\s?{_amount}", r"\1 euros", text)
    text = re.sub(r"[*#_`\[\]()|>~]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split(" ")
    if len(words) <= max_words:
        return text
    clipped = " ".join(words[:max_words])
    # Prefer ending on the last full sentence within the budget
    cut = max(clipped.rfind(". "), clipped.rfind("? "), clipped.rfind("! "))
    if cut > 40:
        return clipped[: cut + 1]
    return clipped.rstrip(",;: ") + "."


def compose_flight_summary(options: list[dict], params: dict) -> str:
    """Deterministic no-LLM spoken summary — fallback when the LLM is down."""
    if not options:
        return NO_RESULTS_SPOKEN
    dest = params.get("destination", "your destination")
    orig = params.get("origin", "London")
    parts = [f"I found {len(options)} option{'s' if len(options) != 1 else ''} "
             f"for {orig} to {dest}."]
    for i, opt in enumerate(options[:3]):
        airline = (opt.get("airline") or "an airline").strip()
        price = (opt.get("price") or "price unknown").strip()
        bits = [f"{airline} at {price}"]
        if opt.get("stops"):
            bits.append(str(opt["stops"]).strip())
        if opt.get("duration"):
            bits.append(str(opt["duration"]).strip())
        prefix = "Cheapest is" if i == 0 else "Next," if i == 1 else "Also,"
        parts.append(f"{prefix} {', '.join(bits)}.")
    parts.append("Want more details on any of these?")
    return _voice_format(" ".join(parts))
