#!/usr/bin/env python3
"""Re-read each licence-register row at its original source (requirements F1.a, F1.b).

    python3 scripts/legal/verify_licence_sources.py            # fetch, compare, write the record
    python3 scripts/legal/verify_licence_sources.py --offline  # check the source list only

`scripts/legal/licence_sources.json` names, for each row of `docs/DATASETS.md` and
`docs/ai/MODEL_WEIGHTS.md` that comes from a published source, where its licence is stated and
what the register says it is. The script reads that place, finds the licence it states, and
compares it with the register. The record, `docs/legal/LICENCE_SOURCE_CHECK.json`, keeps for each
row: the URL read, the HTTP status, the licence found (or the exact text matched), whether it
agrees, and the time. A source that cannot be reached is recorded as `unreachable`, never as
agreeing. The record holds no page content beyond the matched licence words.

Every parser here is a pure function with a test (supracloud-jarvis/ira/tests/readiness/
test_licence_source_check.py); only `fetch` touches the network.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "scripts" / "legal" / "licence_sources.json"
RECORD = ROOT / "docs" / "legal" / "LICENCE_SOURCE_CHECK.json"
USER_AGENT = "ira-care-licence-check/1.0"

#: Licence names as pages write them, to one identifier each.
_ALIASES = [
    (r"cc[- ]?by[- ]?nc[- ]?sa[- ]?4\.0|attribution[- ]noncommercial[- ]sharealike 4\.0", "cc-by-nc-sa-4.0"),
    (r"cc[- ]?by[- ]?nc[- ]?4\.0|attribution[- ]noncommercial 4\.0", "cc-by-nc-4.0"),
    (r"cc[- ]?by[- ]?4\.0|creative commons attribution 4\.0", "cc-by-4.0"),
    (r"cc[- ]?by[- ]?nc", "cc-by-nc"),
    (r"creative commons attribution(?![- ]?non)", "cc-by"),
    (r"cc0[- ]?1\.0|cc0", "cc0-1.0"),
    (r"open data commons open database license|odbl", "odbl-1.0"),
    (r"open data commons attribution license|odc[- ]?by", "odc-by-1.0"),
    (r"apache[- ]?(license[, ]*)?(version )?2\.0|apache-2\.0", "apache-2.0"),
    (r"\bmit\b", "mit"),
    (r"openrail[- ]?m", "openrail-m"),
    (r"\bgemma\b", "gemma"),
    (r"\bopenrail\b", "openrail"),
    (r"review your request|by request|on request|upon request|non-commercial", "by-request"),
]


def normalise(text: str | None) -> str | None:
    """One identifier for a licence as a page or an API writes it, or None if none is recognised."""
    if not text:
        return None
    t = text.strip().lower()
    for pattern, ident in _ALIASES:
        if re.search(pattern, t):
            return ident
    return None


def from_zenodo(body: str) -> str | None:
    lic = (json.loads(body).get("metadata") or {}).get("license")
    if isinstance(lic, dict):
        lic = lic.get("id")
    return normalise(lic) or (lic.lower() if isinstance(lic, str) else None)


def from_huggingface(body: str) -> str | None:
    d = json.loads(body)
    lic = (d.get("cardData") or {}).get("license")
    if isinstance(lic, list):
        lic = lic[0] if lic else None
    if not lic:
        lic = next((t.split(":", 1)[1] for t in d.get("tags", []) if t.startswith("license:")), None)
    return normalise(lic) or (lic.lower() if isinstance(lic, str) else None)


def from_text(body: str, pattern: str) -> tuple[str | None, str | None]:
    """The licence a page states, read where `pattern` (a regex the source list gives for that
    page) finds it, and the words matched. Tags are stripped first."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
    m = re.search(pattern, text, re.I)
    if not m:
        return None, None
    words = m.group(0).strip()[:200]
    return normalise(words), words


def compare(expected: str, found: str | None, status: int | None) -> str:
    """`agrees`, `differs`, `not_stated` (reached, no licence found) or `unreachable`."""
    # 202 is what a bot-check page answers (figshare): the page was not served, so nothing was read.
    if status is None or status >= 400 or status == 202:
        return "unreachable"
    if found is None:
        return "not_stated"
    return "agrees" if found == expected else "differs"


def fetch(url: str, timeout: int = 30) -> tuple[int | None, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - the listed sources only
            return r.status, r.read(3_000_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, ""


def check(entry: dict, fetcher=fetch) -> dict:
    status, body = fetcher(entry["url"])
    found, words = None, None
    if status is not None and status < 400 and body:
        try:
            if entry["kind"] == "zenodo":
                found = from_zenodo(body)
            elif entry["kind"] == "huggingface":
                found = from_huggingface(body)
            else:
                found, words = from_text(body, entry["pattern"])
        except (json.JSONDecodeError, AttributeError, KeyError):
            found = None
    rec = {"register": entry["register"], "row": entry["row"], "url": entry["url"], "kind": entry["kind"],
           "expected": entry["expected"], "found": found, "http_status": status,
           "result": compare(entry["expected"], found, status)}
    if words:
        rec["matched_words"] = words
    return rec


def load_sources(path: Path = SOURCES) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["sources"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--offline", action="store_true", help="validate the source list only")
    a = ap.parse_args(argv)
    sources = load_sources()
    if a.offline:
        print(f"{len(sources)} sources listed")
        return 0
    results = [check(e) for e in sources]
    counts = {k: sum(r["result"] == k for r in results) for k in ("agrees", "differs", "not_stated", "unreachable")}
    RECORD.write_text(json.dumps({"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                  "counts": counts, "results": results}, indent=1, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    for r in results:
        print(f"{r['result']:<12} {r['register']:<14} {r['row']:<45} found={r['found']} expected={r['expected']}")
    print(json.dumps(counts))
    return 1 if counts["differs"] else 0


if __name__ == "__main__":
    sys.exit(main())
