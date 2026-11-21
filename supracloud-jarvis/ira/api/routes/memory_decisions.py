"""Owner-only endpoint that applies the owner's choice on a chat memory: approve a
proposal, or resolve a contradiction."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.middleware.auth import is_owner, require_auth
from api.middleware.workspace import require_workspace_principal
from core.identity.principal import Principal

router = APIRouter(prefix="/chat/memory", tags=["chat", "memory"])


class PendingMemoryDecision(BaseModel):
    content: str = Field(..., min_length=1, max_length=32_000)
    memory_type: str = "fact"
    sensitivity: str = "private"
    confidence: float = Field(1.0, ge=0.0, le=1.0)


class MemoryResolutionRequest(BaseModel):
    resolution: Literal["replace", "retain_both", "save", "discard"]
    pending: PendingMemoryDecision
    existing_id: str | None = None


@router.post("/resolve")
async def resolve_memory_decision(
    body: MemoryResolutionRequest,
    _user: str = Depends(require_auth),
    _p: Principal = Depends(require_workspace_principal),
):
    """Apply an explicit owner choice to a contradiction-blocked memory save."""
    if not is_owner(_user):
        raise HTTPException(status_code=403, detail="Owner access required")
    from memory.contradiction_guard import resolve_decision

    from memory.namespaces import VAULT_NAMESPACE

    # Filed in the Memory Vault, where the owner can see it and forget it, and
    # where deleting a conversation does not reach it (decided 2026-10-19).
    # Under the chat username the vault could neither list nor forget it.
    pending = body.pending.model_dump()
    pending.update({"user_id": VAULT_NAMESPACE, "source": "owner_chat"})
    return await resolve_decision(
        _p,
        body.resolution,
        pending=pending,
        existing_id=body.existing_id,
    )


__all__ = ["router", "MemoryResolutionRequest", "resolve_memory_decision"]
