"""
ira/reasoning/model_traits.py — "does the fast model reason before it answers?"

The supervisor asks the fast model for a one-word routing label on a long message
that matches no keyword, with `max_tokens=10`. A model that reasons first spends
all ten tokens reasoning and returns no label. Measured on 1 November 2026, with
the exact routing request (no `think` field, as IRA sends) on the install's Ollama:

    model                              finetune    generated   label
    qwen3:4b (Qwen3 4B Thinking 2507)  Thinking    10, all reasoning   none, "length"
    qwen3:1.7b (hybrid)                (none)      10, all reasoning   none, "length"
    qwen3:4b-instruct-2507             Instruct    2, no reasoning     "security", "stop"

So the call is worth making only on a model that does not reason first. This module
answers that from the model's own metadata (Ollama's `/api/show`): a model whose
capabilities include `thinking` reasons when the request does not switch it off,
unless it is an Instruct finetune. IRA's requests never switch it off.

Design rules:
  * **Bounded.** One request, `PROBE_TIMEOUT_S`, run off the event loop. A slow or
    absent Ollama costs at most that, once, not a chat turn's worth of waiting.
  * **Fail-soft towards main's behaviour.** "Can't tell" (Ollama unreachable, a
    timeout, a vLLM backend, unreadable metadata) is `None`, and the caller then
    makes the call as before. One log line per failure; a failure is remembered
    for `_FAILURE_TTL_S` so a down Ollama is not probed on every message.
  * **No model calls.** `/api/show` reads metadata; nothing is generated or loaded.
"""
from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from typing import Any, Optional

import httpx

logger = logging.getLogger("ira.reasoning.model_traits")

#: Seconds the metadata probe may take before IRA stops waiting for it.
PROBE_TIMEOUT_S = 1.5
#: Seconds a failed probe is remembered before it is tried again.
_FAILURE_TTL_S = 300.0

_ALLOWED_SCHEMES = frozenset({"http", "https"})

#: model name -> (answer, expires_at). A definite answer does not expire.
_CACHE: dict[str, tuple[Optional[bool], float]] = {}


def reasons_first(show: Any) -> bool:
    """True when `/api/show`'s answer describes a model that reasons first."""
    if not isinstance(show, dict):
        return False
    capabilities = {str(c).lower() for c in (show.get("capabilities") or ())}
    info = show.get("model_info") or {}
    finetune = str(info.get("general.finetune") or "").lower() if isinstance(info, dict) else ""
    return "thinking" in capabilities and not finetune.startswith("instruct")


def _show(host: str, model: str, timeout: float) -> dict:
    url = f"{host.rstrip('/')}/api/show"
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"refusing to fetch {scheme or '<none>'}: URL")
    response = httpx.post(url, json={"model": model}, timeout=timeout)
    response.raise_for_status()
    return response.json()


def probe(model: str, host: str, timeout: Optional[float] = None) -> Optional[bool]:
    """`reasons_first` for `model` on the Ollama at `host`, or None if it cannot tell."""
    try:
        show = _show(host, model, PROBE_TIMEOUT_S if timeout is None else timeout)
    except Exception as exc:  # noqa: BLE001 — "can't tell" keeps main's behaviour
        logger.warning("model metadata probe for %s failed (%s); the routing "
                       "call is kept", model, str(exc)[:120])
        return None
    if not isinstance(show, dict) or "capabilities" not in show:
        logger.warning("model metadata for %s names no capabilities; the "
                       "routing call is kept", model)
        return None
    return reasons_first(show)


def _fast_model_and_host() -> tuple[str, str]:
    from reasoning.model_availability import _ollama_host
    from utils.llm import resolve_ollama_model

    return resolve_ollama_model("fast"), _ollama_host()


async def fast_model_reasons_first() -> Optional[bool]:
    """Whether the fast tier's model reasons before it answers.

    None on a backend other than Ollama, or whenever it cannot be told.
    """
    from utils.llm import _use_ollama

    if not _use_ollama():
        return None
    model, host = await asyncio.to_thread(_fast_model_and_host)
    if not model:
        return None
    now = time.monotonic()
    cached = _CACHE.get(model)
    if cached is not None and now < cached[1]:
        return cached[0]
    answer = await asyncio.to_thread(probe, model, host)
    _CACHE[model] = (answer, float("inf") if answer is not None else now + _FAILURE_TTL_S)
    return answer


def reset_cache() -> None:
    """Forget every probe (tests, or after a model is replaced)."""
    _CACHE.clear()


__all__ = ["PROBE_TIMEOUT_S", "fast_model_reasons_first", "probe", "reasons_first",
           "reset_cache"]
