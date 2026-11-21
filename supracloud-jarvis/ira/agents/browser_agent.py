"""
Browser Agent — read-only live web browsing for voice queries (flight search
and generic lookups).

  search_flights()  → Kayak → Skyscanner → DuckDuckGo, top 3 options
  browser_lookup()  → DuckDuckGo + top result, 2-3 sentence summary

Output is always voice-first: one short spoken paragraph, max ~60 words, no
markdown, no URLs — it flows unchanged through SSE → TTS. Errors and timeouts
surface as graceful spoken sentences, never stack traces.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import logging
import re
import time
from datetime import date

from langchain_core.messages import AIMessage

from agents.state import IRAState
from utils.llm import chat_complete
from utils.browser_agent_tools import (
    NO_RESULTS_SPOKEN,
    _voice_format,
    browser_lookup,
    compose_flight_summary,
    search_flights,
)

logger = logging.getLogger("ira.browser_agent")

_SYSTEM = """\
You are IRA, a voice-first assistant. You just ran a live web browsing tool
(results in context below). Speak the outcome in ONE short paragraph, at most
60 words, as if talking aloud: no markdown, no bullet points, no URLs, prices
as speakable numbers (say "pounds", never symbols). For flights, lead with the
cheapest option, mention the fastest if different, and end with a short offer
to give more details. You are READ-ONLY: never offer to book, buy, or reserve
anything — offering more details is the only follow-up you may suggest.
"""

_FLIGHT_RE = re.compile(r"\b(flights?|fly|flying|airfare|air fare|plane tickets?)\b", re.I)
_ROUTE_RE = re.compile(r"\bfrom\s+([a-zA-Z ]{2,30}?)\s+to\s+([a-zA-Z ]{2,30}?)(?:\s|$|,|\.)", re.I)
_TO_ONLY_RE = re.compile(r"\bto\s+([a-zA-Z ]{2,30}?)(?:\s+(?:next|in|on|this|by|for)\b|$|,|\.)", re.I)

_TIMEOUT_SPOKEN = "That search took too long, so I stopped it. Want me to try again?"
_ERROR_SPOKEN = "I hit a snag while browsing for that. Let's try again in a moment."
_DISABLED_SPOKEN = "Browser lookups are currently turned off. You can enable them in my settings."


def _next_month_defaults(today: date) -> tuple[str, str]:
    """Default travel window: 5th to 19th of next calendar month."""
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    last_day = calendar.monthrange(year, month)[1]
    return (
        date(year, month, min(5, last_day)).isoformat(),
        date(year, month, min(19, last_day)).isoformat(),
    )


def _regex_flight_params(query: str, today: date) -> dict:
    """No-LLM fallback: 'from X to Y' regex + next-month default dates."""
    depart, ret = _next_month_defaults(today)
    origin, destination = "London", ""
    if m := _ROUTE_RE.search(query):
        origin, destination = m.group(1).strip(), m.group(2).strip()
    elif m := _TO_ONLY_RE.search(query):
        destination = m.group(1).strip()
    return {"origin": origin, "destination": destination,
            "depart_date": depart, "return_date": ret}


async def _extract_flight_params(query: str, today: date | None = None) -> dict:
    """LLM-extract origin/destination/dates from natural speech; regex+defaults
    when the LLM is unreachable or returns garbage. Never raises."""
    today = today or date.today()
    fallback = _regex_flight_params(query, today)
    try:
        raw = await chat_complete(
            [{
                "role": "user",
                "content": (
                    f"Today is {today.isoformat()}. Extract flight search parameters "
                    f'from this request: "{query}"\n'
                    'Reply with ONLY a JSON object: {"origin": string or null, '
                    '"destination": string, "depart_date": "YYYY-MM-DD", '
                    '"return_date": "YYYY-MM-DD" or null}. '
                    "Resolve relative dates like \"next month\" against today's date. "
                    "If no origin is stated, use null."
                ),
            }],
            use_deep=False,
            temperature=0.0,
            # No max_tokens: thinking models (qwen3) return empty content when
            # the budget is consumed by the hidden reasoning phase.
        )
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or not parsed.get("destination"):
            return fallback
        return {
            "origin": (parsed.get("origin") or "London").strip(),
            "destination": str(parsed["destination"]).strip(),
            "depart_date": parsed.get("depart_date") or fallback["depart_date"],
            "return_date": parsed.get("return_date"),
        }
    except Exception as e:
        logger.warning("Flight param extraction fell back to regex: %s", e)
        return fallback


async def _speak_result(query: str, tool_name: str, result: dict, fallback: str) -> str:
    """Compose the spoken paragraph via LLM; deterministic fallback if it fails."""
    try:
        response = await chat_complete(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "system",
                 "content": f"Tool execution results ({tool_name}):\n"
                            f"{json.dumps(result, indent=2)[:4000]}"},
                {"role": "user", "content": query},
            ],
            use_deep=False,
            temperature=0.3,
        )
        return _voice_format(response)
    except Exception as e:
        logger.warning("browser_agent: spoken composition failed, using template: %s", e)
        return _voice_format(fallback)


async def browser_agent(state: IRAState) -> IRAState:
    t0 = time.monotonic()
    query = state["user_query"]

    from config import get_settings
    cfg = get_settings()

    response = _ERROR_SPOKEN
    if not cfg.browser_agent_enabled:
        response = _DISABLED_SPOKEN
    else:
        try:
            if _FLIGHT_RE.search(query):
                params = await _extract_flight_params(query)
                if not params.get("destination"):
                    response = ("Where would you like to fly to? "
                                "Tell me the destination and I'll check prices.")
                else:
                    result = await asyncio.wait_for(
                        search_flights(**params),
                        timeout=cfg.browser_task_timeout_s,
                    )
                    if result.get("error"):
                        response = result.get("spoken", NO_RESULTS_SPOKEN)
                    else:
                        response = await _speak_result(
                            query, "search_flights", result,
                            fallback=compose_flight_summary(
                                result.get("options", []), params),
                        )
            else:
                result = await asyncio.wait_for(
                    browser_lookup(query),
                    timeout=cfg.browser_task_timeout_s,
                )
                if result.get("error"):
                    response = result.get("spoken", _ERROR_SPOKEN)
                else:
                    response = result.get("summary") or _ERROR_SPOKEN
        except asyncio.TimeoutError:
            logger.warning("browser_agent: task exceeded %ss budget", cfg.browser_task_timeout_s)
            response = _TIMEOUT_SPOKEN
        except Exception as e:
            # Full detail to logs only — the user never hears a stack trace.
            logger.error("browser_agent failed for %r: %s", query[:80], e, exc_info=True)
            response = _ERROR_SPOKEN

    latency = int((time.monotonic() - t0) * 1000)
    return {
        **state,
        "final_response": response,
        "messages": [AIMessage(content=response)],
        "latency_ms": latency,
        "model_used": "qwen3-fast",
    }
