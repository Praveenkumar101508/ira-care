"""
IRA Digital Eyes — Browser automation tools (Phase 6).

browse_and_summarize_website(url, query) → Playwright headless Chromium + LLM summary.
Navigates to any URL, extracts visible text, answers the user's specific query.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse, urlunparse

from utils.net_safety import check_url

logger = logging.getLogger("ira.browser_tools")


def _sanitize_url(url: str) -> str:
    """Fix #41: strip embedded credentials and URL fragments before use.

    A URL like http://user:pass@internal-host/ leaks credentials into logs
    and bypasses hostname checks if the netloc includes auth info. Rebuilding
    without credentials and without a fragment prevents both issues.
    """
    try:
        p = urlparse(url)
        # Reconstruct netloc without user:pass — keep host and port only
        netloc = p.hostname or ""
        if p.port:
            netloc = f"{netloc}:{p.port}"
        return urlunparse((p.scheme, netloc, p.path, p.params, p.query, ""))
    except Exception:
        return url


_MAX_PAGE_TEXT = 8_000  # characters fed to LLM


def _clean_text(raw: str) -> str:
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"[\t ]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


async def browse_and_summarize_website(url: str, query: str) -> dict:
    """
    Navigate to `url` with a headless Chromium browser, extract visible text,
    and use IRA's fast LLM to answer `query` based on what's on the page.
    Returns a structured dict with the answer, page title, and a brief excerpt.
    """
    # Fix #41: strip embedded credentials and fragments before any checks or logging
    url = _sanitize_url(url)

    # The master switch, before anything leaves the box: the URL check below
    # resolves the host name, and that lookup is itself traffic. This tool
    # launches its own browser and does not pass through the browser agent's
    # navigation, so it has to ask the switch itself.
    from utils.external_gate import BROWSING_BLOCKED_DETAIL, EgressBlocked, require_browsing

    try:
        require_browsing()
    except EgressBlocked:
        return {"error": BROWSING_BLOCKED_DETAIL, "url": url}
    # Route through the consolidated SSRF guard (utils.net_safety) rather than a
    # divergent local copy: it resolves ALL A/AAAA records (not IPv4-only) and
    # rejects IPv4-mapped IPv6 / non-standard IP literals before the browser launches.
    ok, reason = check_url(url)
    if not ok:
        return {
            "error": f"URL blocked for security reasons: {reason}",
            "url": url,
        }

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {
            "error": (
                "Playwright not installed. "
                "Run: pip install playwright && playwright install chromium"
            )
        }

    page_text = ""
    page_title = ""

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    # Fix #40: --no-sandbox is required when Chromium runs as root
                    # inside a Docker container. The container itself is the sandbox;
                    # removing this flag would crash Chromium on startup in that env.
                    "--no-sandbox",
                    "--disable-dev-shm-usage",  # prevent /dev/shm exhaustion in Docker
                    "--disable-gpu",
                ],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()

            # No resource-blocking page.route here: aborting image/font requests
            # makes some sites (proven: bing.com) serve an empty results shell.

            await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            page_title = await page.title()
            raw = await page.evaluate("() => document.body.innerText || ''")
            page_text = _clean_text(raw)[:_MAX_PAGE_TEXT]
            await browser.close()

    except Exception as e:
        logger.error(f"Playwright navigation failed for {url}: {type(e).__name__}")
        return {"error": f"Could not load page: {e}", "url": url}

    if not page_text.strip():
        return {"error": "No text content found on this page.", "url": url, "title": page_title}

    prompt = (
        f'You are an expert at extracting precise answers from web pages.\n'
        f'The user asks: "{query}"\n\n'
        f"Page: {url}\n"
        f"Title: {page_title}\n\n"
        f"Page content:\n---\n{page_text}\n---\n\n"
        f"Answer the user's question using ONLY information on this page. "
        f"Be specific — quote prices, names, and dates when available. "
        f"If the answer is not on the page, say so clearly."
    )

    try:
        from utils.llm import chat_complete
        answer = await chat_complete(
            [{"role": "user", "content": prompt}],
            use_deep=False,
            temperature=0.1,
            # No max_tokens: thinking models (qwen3) spend their budget on the
            # hidden reasoning phase and return empty content if capped low.
        )
        logger.info(f"Browse & summarize: {url[:60]} → answer generated")
        return {
            "url": url,
            "title": page_title,
            "query": query,
            "answer": answer,
            "page_excerpt": page_text[:400],
        }
    except Exception as e:
        return {
            "url": url,
            "title": page_title,
            "query": query,
            "raw_excerpt": page_text[:2000],
            "error": f"LLM summarization failed: {e}",
        }
