# Browser Agent — read-only flight search & web lookup

Voice command → headless Chromium → spoken summary through the existing SSE → TTS pipeline.

> "find me flights from London to Hyderabad next month"
> → *"I found three options for London to Hyderabad in August. Cheapest is Air India at four hundred twelve pounds, one stop, eleven hours. Want more details on any of these?"*

## How it works

```
voice/chat → classify (routing.yaml keywords) → browser agent node (LangGraph)
             ├─ flight query → search_flights():  Kayak → Skyscanner → DuckDuckGo HTML
             └─ anything else → browser_lookup(): DuckDuckGo HTML → top result → summary
→ final_response word-streamed over /api/v1/chat/stream → TTS
```

- **Module layout**: tools in `utils/browser_agent_tools.py`, LangGraph node in
  `agents/browser_agent.py`. Routing keywords in `config/routing.yaml`
  (`agent_rules.browser`, placed *before* `researcher` — first match wins).
- **Browser session**: one shared headless Chromium (lazy singleton), a fresh
  isolated context per task, always closed in `finally`. Existing
  `utils/browser_tools.py` (digital agent) is untouched.
- **Flight URLs are built directly from params** (no UI driving) — more
  reliable than clicking through search forms.
- Origin/destination/dates are LLM-extracted from natural speech (origin
  defaults to London; "next month" resolved against today's date), with a
  regex fallback when the LLM is unreachable.

## Read-only guarantee

- Permitted browser actions: navigate, wait, extract text, screenshot.
- `_safe_click()` is the **only** click site in the module (enforced by a
  structural unit test). It refuses any element whose text/aria-label matches
  the blocklist (`book`, `buy`, `pay`, `checkout`, `sign in`, `submit`, …) and
  only permits cookie-consent labels, preferring "Reject all" over "Accept".
- No form submissions, no personal data entry, no logins, no payments.
- Every URL passes the consolidated SSRF guard (`utils.net_safety.check_url`)
  before navigation.
- Errors and timeouts surface as short spoken sentences — never stack traces.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `BROWSER_AGENT_ENABLED` | `true` | Kill switch — `false` gives a polite "turned off" reply |
| `BROWSER_TASK_TIMEOUT_S` | `60` | Hard ceiling per browser task |
| `BROWSER_HEADLESS` | `true` | `false` shows the Chromium window (debugging) |

Setup (one-time): `pip install playwright` (already in requirements.txt) then
`playwright install chromium`.

## Demo without the voice pipeline

```powershell
cd supracloud-jarvis/ira
python scripts/demo_browser_agent.py "find me flights from London to Hyderabad next month"

# No LLM running? Bypass extraction:
python scripts/demo_browser_agent.py --origin London --dest Hyderabad --depart 2026-08-05 --return-date 2026-08-19 "flights"

# Generic lookup:
python scripts/demo_browser_agent.py --lookup "what is the capital of Telangana"

# Watch the browser work:
python scripts/demo_browser_agent.py --no-headless "flights from London to Hyderabad"
```

## Tests

```powershell
# Unit (no playwright/network/LLM needed):
python -m pytest tests/test_browser_agent.py -q

# Integration (real Chromium against DuckDuckGo, opt-in):
playwright install chromium
$env:RUN_BROWSER_IT="1"; python -m pytest tests/test_browser_agent_integration.py -q
```

## Extending with new browser tools

1. Add an async function to `utils/browser_agent_tools.py` returning a dict
   (`"error"` key on failure, never raise). Fetch pages via
   `_fetch_page_text(url)` — it handles SSRF checks, consent banners, and
   resource blocking for you.
2. Route to it from `agents/browser_agent.py` (add an intent regex).
3. Add keywords under `agent_rules.browser` in `config/routing.yaml`.
4. Keep output speakable: pass it through `_voice_format()`.

## Known limitations & roadmap

- **Bot-blocking**: Kayak/Skyscanner block headless browsers a meaningful
  fraction of the time. That's expected — the chain degrades to the
  DuckDuckGo HTML endpoint and, if everything fails, a graceful spoken
  fallback ("the sites are blocking automated access"). The graceful path is
  part of the design, not a bug.
- Prices from search snippets (DDG fallback) are indicative, not live quotes.
- Roadmap: replace scraping with an official API (e.g. Amadeus Self-Service)
  or move to computer-use-style visual browser control for resilient
  extraction.
