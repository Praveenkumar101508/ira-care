"""Retrieve memories only when the message can use them (P0 performance).

Every chat turn used to run semantic retrieval before anything else: one query
embedding on the CPU, two vector queries, a reranker pass when enabled, and about
ninety database statements, then up to 2,000 characters of memory block in the
prompt (research pass C, 10 November 2026). A question about the world ("what is
the capital of Peru?"), a pasted document, or a greeting gains nothing from it and
pays all of it, on an 8 GB Mac next to a 3.3 GB model.

`retrieval_wanted` says whether a turn should retrieve: yes when the message refers
to the user's own facts or to memory ("my", "our", "remember", "remind me", "what
did I say", "do you know"), when it is a memory command, or when the conversation
already has history (a follow-up may refer back to anything). Otherwise no. The
owner profile is still injected every turn; only the vector retrieval is skipped.

The gate is OFF by default. Measured on the Linux acceptance suite (10 November
2026, 310 requests per leg) it took chat p95 from 8.7 s to 5.9 s with correctness
unchanged, but it has a known cost: a first-turn question that names a stored fact
without referring to the user ("what is the project codename?") no longer recalls
it, and 19 existing recall tests encode exactly that behaviour. Set
`IRA_MEMORY_RETRIEVAL_GATE=true` to enable it where that trade is acceptable, for
example a demo profile; the owner decides the default.
"""
from __future__ import annotations

import os
import re

_SELF_RE = re.compile(
    r"\b(?:my|mine|our|ours|myself|ourselves)\b"
    r"|\b(?:i|i've|i'd|we|we've)\s+(?:told|said|mentioned|gave|saved|noted|asked|set|have|had|was|am|use|live|work|own|like|prefer)\b"
    r"|\bdid i\b|\bwhat did i\b|\bhave i\b|\bam i\b|\bwhen do i\b|\bwhere do i\b",
    re.I,
)
_MEMORY_RE = re.compile(
    r"\b(?:remember|recall|remind|reminder|forget|memor(?:y|ies|ise|ize)|note that|noted|save this|saved|"
    r"earlier|previously|last time|before|again|the other day|yesterday|you know|do you know|you said|"
    r"you told|told you|as i said|as mentioned|i mentioned|personal|profile|about me|who am i|what do you know)\b",
    re.I,
)


def gate_enabled() -> bool:
    return os.environ.get("IRA_MEMORY_RETRIEVAL_GATE", "false").strip().lower() in ("1", "true", "yes", "on")


def message_refers_to_memory(message: str) -> bool:
    """True when the text itself asks for, or adds to, the user's own facts."""
    text = (message or "").strip()
    if not text:
        return False
    return bool(_SELF_RE.search(text) or _MEMORY_RE.search(text))


def retrieval_wanted(message: str, *, has_history: bool) -> bool:
    """Whether this turn should run semantic memory retrieval."""
    if not gate_enabled():
        return True
    if has_history:
        return True
    return message_refers_to_memory(message)


__all__ = ["gate_enabled", "message_refers_to_memory", "retrieval_wanted"]
