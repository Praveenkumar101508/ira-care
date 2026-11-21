"""Lightweight contradiction-aware handling for explicit chat memory commands.

This module deliberately avoids importing the chat graph, embedding stack, or
model runtime so its deterministic command boundary stays independently
testable and available when optional model dependencies are offline.
"""
from __future__ import annotations


def memory_save_content(message: str) -> str | None:
    """Extract only explicit natural-language memory-save commands."""
    try:
        from commands.parser import parse_command
        parsed = parse_command(message)
        if parsed.get("intent") == "save_memory":
            content = str((parsed.get("params") or {}).get("content") or "").strip()
            if message.strip().lower().startswith("remember to ") and content.lower().startswith("to "):
                content = content[3:].strip()
            return content or None
    except Exception:
        return None
    return None


async def guard_chat_memory(principal, message: str, *, user: str, is_owner: bool) -> dict | None:
    """Turn an explicit owner "remember ..." into a proposal for the Memory Vault.

    Nothing is saved here. The owner decided on 2026-10-19 that a fact said in
    chat goes into the Memory Vault only after they approve it, so that it is
    visible in the vault, can be forgotten there, and survives deleting the
    conversation it was said in. The contradiction check still runs first:

      * a conflict returns ``needs_owner_decision`` (Replace old / Keep both /
        Discard), exactly as before;
      * otherwise ``needs_owner_approval`` carries the proposed entry, and the
        chat shows Save / Not now. Save resolves it through
        ``POST /api/v1/chat/memory/resolve`` with ``resolution="save"``.

    Either way the pending entry is filed under the Memory Vault's namespace,
    not under the chat username, where the vault could neither list nor
    forget it. ``user`` is kept for the callers' signature.
    """
    if not is_owner:
        return None
    content = memory_save_content(message)
    if not content:
        return None
    from memory.contradiction_guard import guarded_save
    from memory.namespaces import VAULT_NAMESPACE

    result = await guarded_save(principal, content, user_id=VAULT_NAMESPACE,
                                source="owner_chat", auto_save_when_clear=False)
    if result.get("status") == "clear":
        return {
            "status": "needs_owner_approval",
            "contradiction_checked": result.get("contradiction_checked", False),
            "questions": [],
            "candidates": [],
            "pending": {"content": content, "memory_type": "fact",
                        "sensitivity": "private", "confidence": 1.0},
        }
    return result


def memory_reply(result: dict) -> str:
    if result.get("status") == "needs_owner_approval":
        content = (result.get("pending") or {}).get("content", "")
        return f"Shall I save this to your Memory Vault? \u201c{content}\u201d"
    if result.get("status") == "needs_owner_decision":
        questions = result.get("questions") or [
            "This conflicts with an existing memory. What should I keep?"
        ]
        return "I found a contradiction before saving. " + " ".join(questions)
    if result.get("status") == "saved":
        return "Saved to your Memory Vault."
    return "I could not save that memory."


#: The statuses that ask the owner to choose, and travel to the client as
#: ``memory_decision``.
NEEDS_OWNER = frozenset({"needs_owner_decision", "needs_owner_approval"})


__all__ = ["memory_save_content", "guard_chat_memory", "memory_reply", "NEEDS_OWNER"]
