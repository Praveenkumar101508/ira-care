"""
memory/contradiction_guard.py — the save-path integration for temporal
memory's contradiction detection (PR 71 wiring).

A single entry point, `guarded_save`, that the chat/memory-save flow calls
instead of writing a typed memory blindly:

  * runs temporal.check_before_save first;
  * if the new content conflicts with an existing active memory, it does
    NOT write — it returns a "needs_owner_decision" result carrying the
    owner-facing question(s), so the UI/flow can ask the owner
    (constitution: "Do not silently overwrite contradictory memories");
  * on the owner's answer it either saves-and-supersedes ('replace') or
    saves both with separate provenance ('retain_both').

Fail-soft on the DETECTION read (a down DB must not block saving), fail-loud
on the WRITE (a lost memory must be loud). When detection is unavailable the
save proceeds and the result flags contradiction_checked=False (honest).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from memory import temporal

logger = logging.getLogger("ira.memory.contradiction_guard")


async def guarded_save(principal, content: str, *, user_id: str,
                       memory_type: str = "fact",
                       source: str = "owner_chat",
                       sensitivity: str = "private",
                       confidence: float = 1.0,
                       auto_save_when_clear: bool = True,
                       **save_kwargs: Any) -> dict[str, Any]:
    """Check for contradictions, then save when clear.

    Returns one of:
      {"status": "saved", "memory_id", "contradiction_checked": bool}
      {"status": "needs_owner_decision", "questions": [...],
       "candidates": [{existing_id, existing_content, question}], "pending": {...}}
    """
    checked = True
    try:
        conflicts = await temporal.check_before_save(
        principal,
            content, user_id=user_id, memory_type=memory_type)
    except Exception as exc:
        logger.warning("contradiction check unavailable: %s", exc)
        conflicts, checked = [], False

    if conflicts:
        return {
            "status": "needs_owner_decision",
            "contradiction_checked": True,
            "questions": [c.question() for c in conflicts],
            "candidates": [
                {"existing_id": c.existing_id,
                 "existing_content": c.existing_content,
                 "conflicting_values": c.conflicting_values,
                 "question": c.question()} for c in conflicts],
            "pending": {"content": content, "user_id": user_id,
                        "memory_type": memory_type, "source": source,
                        "sensitivity": sensitivity, "confidence": confidence},
        }

    if not auto_save_when_clear:
        return {"status": "clear", "contradiction_checked": checked,
                "content": content}

    memory_id = await temporal.save_memory(
        principal,
        content, user_id=user_id, memory_type=memory_type, source=source,
        sensitivity=sensitivity, confidence=confidence, **save_kwargs)
    return {"status": "saved", "memory_id": memory_id,
            "contradiction_checked": checked}


async def resolve_decision(principal, resolution: str, *, pending: dict[str, Any],
                           existing_id: Optional[str] = None,
                           reason: str = "owner resolved contradiction",
                           **save_kwargs: Any) -> dict[str, Any]:
    """Apply the owner's answer to a needs_owner_decision result.

      resolution == 'replace'      → save new, supersede the old memory
      resolution == 'retain_both'  → save new alongside (both keep provenance)
      resolution == 'save'         → save new: the owner approved a proposal
                                     that had no conflict
      resolution == 'discard'      → do not save the new content
    """
    user_id = pending["user_id"]
    if resolution == "discard":
        return {"status": "discarded"}
    if resolution not in ("replace", "retain_both", "save"):
        raise ValueError(f"unknown resolution: {resolution!r}")

    memory_id = await temporal.save_memory(
        principal,
        pending["content"], user_id=user_id,
        memory_type=pending.get("memory_type", "fact"),
        source=pending.get("source", "owner_chat"),
        sensitivity=pending.get("sensitivity", "private"),
        confidence=pending.get("confidence", 1.0),
        owner_confirmed=True, **save_kwargs)

    superseded = False
    if resolution == "replace" and existing_id:
        superseded = await temporal.supersede(principal, existing_id, memory_id,
                                              user_id=user_id, reason=reason)
    return {"status": "saved", "memory_id": memory_id,
            "resolution": resolution, "superseded_old": superseded}


__all__ = ["guarded_save", "resolve_decision"]
