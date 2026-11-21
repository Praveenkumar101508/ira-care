"""Secrets typed into the chat never reach storage (security review, 10 November 2026).

The Armory is unlocked by typing its master key into the chat ("unlock the armory
with key <secret>", "master key: <secret>"). The agent reads the key from the live
message, which is right; but the same message was then saved verbatim to the
`messages` table, embedded into memory, and replayed as history on later turns, so
the key sat in the database and in every later prompt. `redact_master_key` replaces
the key with a fixed placeholder; `memory/store.py::save_message` applies it to every
user message before the INSERT and the embedding, on every route.

The unlock patterns live here, not in `agents/armory.py`, so the agent and the
store cannot drift apart: `_extract_master_key` and `redact_master_key` match the
same text.
"""
from __future__ import annotations

import re

#: "unlock the armory with key …", "open the vault using the master password: …"
UNLOCK_RE = re.compile(
    r"\b(?:unlock|open)\b.*?\b(?:armory|vault)\b\s*"
    r"(?:with|using|,|-)?\s*(?:the\s+)?(?:master\s+)?"
    r"(?:key|password|pass|code)\s*[:=]?\s*(?P<key>\S.*)$",
    re.I,
)
#: "master key: …", "unlock: …", "key = …" as the whole message.
UNLOCK_KEY_ONLY_RE = re.compile(
    r"^\s*(?:master\s+key|unlock|key)\s*[:=]\s*(?P<key>\S.+)$", re.I,
)

MASTER_KEY_PLACEHOLDER = "[armory master key redacted]"


def extract_master_key(text: str) -> str | None:
    """The master key typed in `text`, or None."""
    for pattern in (UNLOCK_RE, UNLOCK_KEY_ONLY_RE):
        m = pattern.search(text.strip())
        if m:
            return m.group("key").strip().strip("\"'")
    return None


def redact_master_key(text: str) -> str:
    """`text` with a typed Armory master key replaced by the placeholder; `text`
    unchanged when it carries none."""
    if not text:
        return text
    stripped = text.strip()
    for pattern in (UNLOCK_RE, UNLOCK_KEY_ONLY_RE):
        m = pattern.search(stripped)
        if m:
            start, end = m.span("key")
            return stripped[:start] + MASTER_KEY_PLACEHOLDER + stripped[end:]
    return text


__all__ = ["UNLOCK_RE", "UNLOCK_KEY_ONLY_RE", "MASTER_KEY_PLACEHOLDER", "extract_master_key", "redact_master_key"]
