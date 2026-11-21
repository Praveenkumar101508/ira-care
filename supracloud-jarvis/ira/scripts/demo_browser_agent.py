"""Standalone demo for the browser agent — no voice pipeline, no server.

Run from the ira/ directory (or anywhere; sys.path is fixed up):

    python scripts/demo_browser_agent.py "find me flights from London to Hyderabad next month"

Options:
    --lookup                     force the generic browser_lookup tool
    --origin/--dest/--depart/--return-date
                                 bypass LLM param extraction (works with no LLM running)
    --no-headless                show the Chromium window (debugging on Windows)

Prints exactly what IRA would SPEAK through TTS, then exits.
Exit code 0 on success or graceful failure; 1 only on unexpected crash.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Make top-level imports (utils.*, agents.*, config) work from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Placeholder secrets so get_settings() loads without a full .env (same idiom
# as tests/test_browser_ssrf.py)
for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "demo-placeholder")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Browser agent demo (read-only)")
    parser.add_argument("query", help='e.g. "find me flights from London to Hyderabad next month"')
    parser.add_argument("--lookup", action="store_true", help="force generic browser_lookup")
    parser.add_argument("--origin", default=None)
    parser.add_argument("--dest", default=None)
    parser.add_argument("--depart", default=None, help="YYYY-MM-DD")
    parser.add_argument("--return-date", dest="return_date", default=None, help="YYYY-MM-DD")
    parser.add_argument("--no-headless", action="store_true", help="visible Chromium window")
    args = parser.parse_args()

    if args.no_headless:
        os.environ["BROWSER_HEADLESS"] = "false"

    from config import get_settings
    from utils.browser_agent_tools import (
        browser_lookup,
        compose_flight_summary,
        search_flights,
        shutdown_browser_session,
    )

    cfg = get_settings()
    timeout = cfg.browser_task_timeout_s

    try:
        if args.lookup:
            result = await asyncio.wait_for(browser_lookup(args.query), timeout=timeout)
            spoken = result.get("summary") or result.get("spoken") or str(result.get("error"))
            print(spoken)
            return 0

        if args.origin and args.dest and args.depart:
            params = {"origin": args.origin, "destination": args.dest,
                      "depart_date": args.depart, "return_date": args.return_date}
            print(f"[demo] using explicit params: {params}", file=sys.stderr)
        else:
            from agents.browser_agent import _extract_flight_params
            params = await _extract_flight_params(args.query)
            print(f"[demo] extracted params: {params}", file=sys.stderr)
            if not params.get("destination"):
                print("Where would you like to fly to? Tell me the destination "
                      "and I'll check prices.")
                return 0

        result = await asyncio.wait_for(search_flights(**params), timeout=timeout)

        if result.get("error"):
            print(result.get("spoken", "I couldn't get live prices right now."))
            return 0

        print(f"[demo] {len(result['options'])} option(s) from {result['source']}",
              file=sys.stderr)
        # Prefer the LLM-composed sentence; fall back to the deterministic template
        try:
            from agents.browser_agent import _speak_result
            spoken = await _speak_result(
                args.query, "search_flights", result,
                fallback=compose_flight_summary(result["options"], params),
            )
        except Exception:
            spoken = compose_flight_summary(result["options"], params)
        print(spoken)
        return 0

    except asyncio.TimeoutError:
        print("That search took too long, so I stopped it. Want me to try again?")
        return 0
    finally:
        await shutdown_browser_session()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
