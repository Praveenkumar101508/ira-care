"""Privacy Centre — see everything IRA keeps, and control all of it.

GET    /api/v1/privacy/overview          -> every section, in one call
GET    /api/v1/privacy/memories          -> every retained memory, explained
GET    /api/v1/privacy/memories/pending  -> memories awaiting approval
POST   /api/v1/privacy/memories/{id}/approve
POST   /api/v1/privacy/memories/{id}/reject
PATCH  /api/v1/privacy/memories/{id}     -> edit content / expiry / sharing
DELETE /api/v1/privacy/memories/{id}
GET    /api/v1/privacy/settings          -> retention settings
PUT    /api/v1/privacy/settings
POST   /api/v1/privacy/settings/enforce  -> apply them now, report what went
GET    /api/v1/privacy/events            -> the privacy timeline
GET    /api/v1/privacy/deletions         -> what was deleted, and what wasn't
POST   /api/v1/privacy/delete            -> erase, and report per store

Two things this module refuses to do:

**It never reports a deletion as complete unless every store succeeded.** The
outcome carries a per-store breakdown and a summary written by
`core.privacy.disclosure`, which is where the "do not claim more than was done"
rule is enforced once rather than at each call site.

**It never emits IRA Private's local-only wording on a cloud deployment.** The
storage disclosure is derived from the edition, not written into the response.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from api.middleware.auth import require_auth
from api.middleware.workspace import TENANT_HEADER, WORKSPACE_HEADER
from core.editions import current_edition
from core.privacy import disclosure as _disclosure
from core.privacy import erasure as _erasure
from core.privacy import export as _export
from core.privacy import retention as _retention
from core.privacy import store as _store
# NOTE: the API field is `sensitivity` — the user-facing CATEGORY. The
# database column is `sensitivity_category`, because 017_temporal_memory.sql
# already owns a `sensitivity` column meaning confidentiality. store.py maps
# between the two; nothing outside it needs to know.
from core.privacy.memory import (
    EXTERNAL_POLICIES,
    MEMORY_MODES,
    SENSITIVITIES,
    RetentionSettings,
)
from core.identity.resolver import CloudIdentityNotAvailable, resolve_principal

router = APIRouter(prefix="/privacy", tags=["privacy"])

#: Scopes a user may delete. 'account' is the widest and is deliberately a
#: separate word from 'everything' — deleting your data and closing your account
#: are different decisions.
DELETE_SCOPES = ("memories", "conversations", "voice", "everything", "account")


class SettingsBody(BaseModel):
    memory_enabled: Optional[bool] = None
    memory_mode: Optional[str] = Field(None, max_length=20)
    conversation_retention_days: Optional[int] = Field(None, ge=1, le=3650)
    memory_retention_days: Optional[int] = Field(None, ge=1, le=3650)
    voice_retention_hours: Optional[int] = Field(None, ge=0, le=8760)
    disabled_categories: Optional[list[str]] = None


class MemoryPatchBody(BaseModel):
    content: Optional[str] = Field(None, max_length=8000)
    expires_at: Optional[datetime] = None
    clear_expiry: bool = False
    sensitivity: Optional[str] = Field(None, max_length=20)
    allowed_connectors: Optional[list[str]] = None
    external_provider_policy: Optional[str] = Field(None, max_length=20)


class DeleteBody(BaseModel):
    scope: str = Field(..., max_length=20)
    #: Typing the scope back is required for every scope. A single click
    #: should not be able to destroy someone's entire history, and "all my
    #: conversations" is that history as surely as "everything" is.
    confirm: str = Field("", max_length=40)


async def _principal(
    username: str = Depends(require_auth),
    x_workspace_id: Optional[str] = Header(default=None, alias=WORKSPACE_HEADER),
    x_tenant_id: Optional[str] = Header(default=None, alias=TENANT_HEADER),
):
    """A principal carrying a VERIFIED WORKSPACE.

    The privacy surface reads and deletes `memory_embeddings`, which is
    workspace-scoped, so a tenant-only principal would match nothing once the
    policies land — a privacy review page that silently shows an empty vault.
    Erasure is different and stays tenant-wide: `erase_postgres` enumerates the
    tenant's workspaces and binds each one in turn.

    P9-03 made it a FastAPI dependency rather than a function the routes call.
    That is the whole of the change here: a plain function has nowhere for
    `X-Tenant-ID` to arrive, so a caller with several memberships was refused
    before any of these endpoints ran — and the refusal was reported as 503.
    `resolve_workspace_principal` already maps the three failures onto 400/403/
    503, so this no longer catches anything itself.

    ── P12-R1, FINDING CB: the OTHER header was still missing ──────────────

    P9-03 added `X-Tenant-ID` and stopped there. `requested_workspace_id` stayed
    hard-coded `None`, and in the cloud edition `resolve_workspace_principal`
    answers that with `WorkspaceRequired` — *"the server does not select one on
    your behalf"* — so **every route in this module refused with 400 in cloud**:
    overview, memories, deletion and export alike. The one surface a user is
    told to visit to exercise their data rights did not work in the edition that
    has data rights to exercise.

    Proved by execution against real PostgreSQL, not by reading: the same
    seeded cloud identity is refused 400 here and resolves through
    `require_workspace_principal`, which reads the header.

    It survived because `tests/tenancy/test_p1205_data_export.py` builds its
    ASGI client by **overriding this dependency** — a route test whose predicate
    excludes the one thing that was wrong. Phase 12's own third lesson, on a
    third control: *an audit is only as wide as its predicate.*
    """
    from api.middleware.workspace import resolve_workspace_principal

    return await resolve_workspace_principal(username, x_workspace_id, x_tenant_id)


# ── Overview ─────────────────────────────────────────────────────────────────


@router.get("/overview")
async def overview(principal = Depends(_principal)) -> dict[str, Any]:
    """Every Privacy Centre section in one call.

    One round trip because the page is a single statement about a user's data;
    assembling it from six requests is six chances to render a half-truth.
    """
    edition = current_edition()
    settings = await _store.load_settings(principal)
    memories = await _store.list_memories(principal, limit=500)
    pending = [m for m in memories if m.review_state == "pending"]
    storage = _disclosure.storage_disclosure(edition)

    return {
        "edition": edition,
        "storage": {
            "headline": storage.headline,
            "detail": storage.detail,
            "local_processing": storage.local_processing,
        },
        "memory": {
            "total": len(memories),
            "pending_approval": len(pending),
            "mode": settings.memory_mode,
            "enabled": settings.memory_enabled,
            "explanation": _disclosure.memory_mode_disclosure(settings.memory_mode),
            "disabled_categories": sorted(settings.disabled_categories),
        },
        "retention": {
            "conversation_days": settings.conversation_retention_days,
            "memory_days": settings.memory_retention_days,
            "voice_hours": settings.voice_retention_hours,
            "voice_explanation": _disclosure.voice_retention_disclosure(
                settings.voice_retention_hours
            ),
        },
        # The vocabularies the UI offers, served rather than hard-coded so the
        # page cannot present an option the backend rejects.
        "vocabularies": {
            "memory_modes": list(MEMORY_MODES),
            "sensitivities": list(SENSITIVITIES),
            "external_policies": list(EXTERNAL_POLICIES),
            "delete_scopes": list(DELETE_SCOPES),
        },
        "stores_cleared_on_delete": list(_erasure.KNOWN_STORES),
    }


# ── Memories ─────────────────────────────────────────────────────────────────


@router.get("/memories")
async def list_memories(
    include_deleted: bool = False,
    limit: int = 200,
    principal = Depends(_principal),
) -> dict[str, Any]:
    memories = await _store.list_memories(
        principal, include_deleted=include_deleted, limit=limit
    )
    return {"memories": [m.public_dict() for m in memories]}


@router.get("/memories/pending")
async def list_pending(principal = Depends(_principal)) -> dict[str, Any]:
    return {"memories": [m.public_dict() for m in await _store.pending_memories(principal)]}


@router.post("/memories/{memory_id}/approve")
async def approve_memory(
    memory_id: uuid.UUID, principal = Depends(_principal)
) -> dict[str, Any]:
    if not await _store.set_review_state(principal, memory_id, "accepted"):
        raise HTTPException(status_code=404, detail="No such memory")
    await _store.record_privacy_event(principal, "memory_approved", {"id": str(memory_id)})
    return {"id": str(memory_id), "review_state": "accepted"}


@router.post("/memories/{memory_id}/reject")
async def reject_memory(
    memory_id: uuid.UUID, principal = Depends(_principal)
) -> dict[str, Any]:
    if not await _store.set_review_state(principal, memory_id, "rejected"):
        raise HTTPException(status_code=404, detail="No such memory")
    await _store.record_privacy_event(principal, "memory_rejected", {"id": str(memory_id)})
    return {"id": str(memory_id), "review_state": "rejected"}


@router.patch("/memories/{memory_id}")
async def edit_memory(
    memory_id: uuid.UUID,
    body: MemoryPatchBody,
    principal = Depends(_principal),
) -> dict[str, Any]:
    if body.sensitivity is not None and body.sensitivity not in SENSITIVITIES:
        raise HTTPException(
            status_code=422,
            detail=f"sensitivity must be one of {', '.join(SENSITIVITIES)}",
        )
    if (
        body.external_provider_policy is not None
        and body.external_provider_policy not in EXTERNAL_POLICIES
    ):
        raise HTTPException(
            status_code=422,
            detail=f"external_provider_policy must be one of {', '.join(EXTERNAL_POLICIES)}",
        )

    changed = await _store.update_memory(
        principal, memory_id,
        content=body.content,
        expires_at=body.expires_at,
        clear_expiry=body.clear_expiry,
        sensitivity=body.sensitivity,
        allowed_connectors=body.allowed_connectors,
        external_provider_policy=body.external_provider_policy,
    )
    if not changed:
        raise HTTPException(status_code=404, detail="No such memory, or nothing to change")
    await _store.record_privacy_event(principal, "memory_edited", {"id": str(memory_id)})
    return {"id": str(memory_id), "updated": True}


@router.delete("/memories/{memory_id}")
async def delete_memory(
    memory_id: uuid.UUID, principal = Depends(_principal)
) -> dict[str, Any]:
    if not await _store.delete_memory(principal, memory_id):
        raise HTTPException(status_code=404, detail="No such memory")
    await _store.record_privacy_event(principal, "memory_deleted", {"id": str(memory_id)})
    return {"id": str(memory_id), "deleted": True}


# ── Settings ─────────────────────────────────────────────────────────────────


@router.get("/settings")
async def get_settings(principal = Depends(_principal)) -> dict[str, Any]:
    settings = await _store.load_settings(principal)
    return {
        "memory_enabled": settings.memory_enabled,
        "memory_mode": settings.memory_mode,
        "conversation_retention_days": settings.conversation_retention_days,
        "memory_retention_days": settings.memory_retention_days,
        "voice_retention_hours": settings.voice_retention_hours,
        "disabled_categories": sorted(settings.disabled_categories),
        "explanations": {
            "memory": _disclosure.memory_mode_disclosure(settings.memory_mode),
            "voice": _disclosure.voice_retention_disclosure(settings.voice_retention_hours),
        },
    }


@router.put("/settings")
async def put_settings(
    body: SettingsBody, principal = Depends(_principal)
) -> dict[str, Any]:
    if body.memory_mode is not None and body.memory_mode not in MEMORY_MODES:
        raise HTTPException(
            status_code=422, detail=f"memory_mode must be one of {', '.join(MEMORY_MODES)}"
        )
    unknown = set(body.disabled_categories or ()) - set(SENSITIVITIES)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown categories: {', '.join(sorted(unknown))}",
        )

    current = await _store.load_settings(principal)
    updated = RetentionSettings(
        memory_enabled=(
            body.memory_enabled if body.memory_enabled is not None
            else current.memory_enabled
        ),
        memory_mode=body.memory_mode or current.memory_mode,
        conversation_retention_days=(
            body.conversation_retention_days
            if body.conversation_retention_days is not None
            else current.conversation_retention_days
        ),
        memory_retention_days=(
            body.memory_retention_days if body.memory_retention_days is not None
            else current.memory_retention_days
        ),
        voice_retention_hours=(
            body.voice_retention_hours if body.voice_retention_hours is not None
            else current.voice_retention_hours
        ),
        disabled_categories=(
            frozenset(body.disabled_categories)
            if body.disabled_categories is not None
            else current.disabled_categories
        ),
    )
    await _store.save_settings(principal, updated)
    await _store.record_privacy_event(
        principal, "settings_changed", {"memory_mode": updated.memory_mode}
    )
    return {"saved": True, "memory_mode": updated.memory_mode}


@router.post("/settings/enforce")
async def enforce_retention(principal = Depends(_principal)) -> dict[str, Any]:
    """Apply this caller's retention settings now, and report what went.

    This endpoint exists because until Phase 4 batch 06 nothing applied them:
    `conversation_retention_days` was read only by the page above, and
    `memory_retention_days` became a per-row `expires_at` whose one consumer had
    no callers. A setting the user was shown and that never ran is a promise the
    product did not keep.

    The response repeats what the pass could NOT do rather than reporting a
    round number: `auth_events` past retention are counted and deliberately left
    (`061` makes the log append-only for every role), and a pass covers the
    caller's bound workspace only, so any others are named as uncovered.
    """
    outcome = await _retention.enforce_for(principal)
    await _store.record_privacy_event(
        principal, "retention_enforced",
        {"conversations": outcome.conversations_deleted,
         "messages": outcome.messages_deleted,
         "memories": outcome.memories_deleted},
    )
    return {
        "conversations_deleted": outcome.conversations_deleted,
        "messages_deleted": outcome.messages_deleted,
        "memories_expired": outcome.memories_expired,
        "memories_deleted": outcome.memories_deleted,
        "auth_events_past_retention_not_deleted":
            outcome.auth_events_past_retention,
        "workspaces_not_covered": outcome.workspaces_not_covered,
        "notes": list(outcome.notes),
    }


# ── History ──────────────────────────────────────────────────────────────────


@router.get("/events")
async def events(limit: int = 50, principal = Depends(_principal)) -> dict[str, Any]:
    return {"events": await _store.recent_privacy_events(principal, limit=limit)}


@router.get("/deletions")
async def deletions(limit: int = 20, principal = Depends(_principal)) -> dict[str, Any]:
    """Past deletions, including which stores were NOT reached."""
    return {"deletions": await _store.recent_deletions(principal, limit=limit)}


# ── Export ───────────────────────────────────────────────────────────────────


@router.get("/export")
async def export_data(
    scope: str = "everything", principal = Depends(_principal)
) -> dict[str, Any]:
    """The caller's own data, scoped by RLS rather than by a WHERE clause.

    Deliberately the same `_principal` dependency the rest of this module uses —
    a VERIFIED workspace principal, resolved from membership, never from
    anything the client sent. There is no export-specific authority: an export
    is a person asking for their own data, so it needs exactly the authority
    they already have and not one byte more.

    `GET` rather than `POST` because it creates nothing the user can lose by
    repeating it. It IS recorded — `export_requests` has had a table since `026`
    and no writer until now — but the record is of the request, not of an
    artefact, and re-requesting is not a destructive act the way re-deleting is.
    So no `confirm` field: this is the one privacy operation that cannot go
    wrong by being performed twice.

    **The response is never a bare list of rows.** It carries every table in
    scope with its count and the boundary it was read under, including the
    zeros, so an empty account is distinguishable from an export that bound the
    wrong context and silently read nothing. That distinction is the entire
    reason this endpoint reports the way it does.
    """
    if scope not in DELETE_SCOPES:
        # Same vocabulary as deletion, on purpose. A user who can delete a scope
        # should be able to see what deleting it would take, in the same words;
        # two overlapping sets of scope names would be a question about which
        # one this page means.
        raise HTTPException(
            status_code=422, detail=f"scope must be one of {', '.join(DELETE_SCOPES)}"
        )

    result = await _export.export_subject_data(principal, scope=scope)
    await _export.record_export_request(principal, scope=scope, result=result)
    await _store.record_privacy_event(
        principal, "data_exported",
        {"scope": scope, "ok": result.ok, "rows": result.total_rows},
    )

    payload = result.as_dict()
    if not result.ok:
        # 500, not 200-with-an-error-field. A client that renders the rows and
        # ignores the envelope must not be able to present an incomplete export
        # as the complete record of what is held about somebody.
        raise HTTPException(status_code=500, detail=payload)
    return payload


# ── Deletion ─────────────────────────────────────────────────────────────────


@router.post("/delete")
async def delete_data(
    body: DeleteBody, principal = Depends(_principal)
) -> dict[str, Any]:
    """Erase data across every store, and report honestly what was reached.

    The response is never a bare success. It carries the per-store breakdown,
    the count of audit records that could not be erased, and the date backup
    copies are expected to age out — because a user told "your data has been
    deleted" who later finds a backup copy has been misled, even if every live
    store really was cleared.
    """
    if body.scope not in DELETE_SCOPES:
        raise HTTPException(
            status_code=422, detail=f"scope must be one of {', '.join(DELETE_SCOPES)}"
        )
    # Every scope needs the word typed back (P0-03). Until 31 October 2026 only
    # 'everything' and 'account' did, and 'memories', 'conversations' and
    # 'voice' erased every row of their kind, in every store, on one request
    # carrying nothing but the scope. Each of them is irreversible for the live
    # stores, so each is confirmed the same way.
    if body.confirm != body.scope:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Deleting {body.scope!r} is irreversible for the live stores. "
                f"Send confirm={body.scope!r} to proceed."
            ),
        )

    outcome = await _erasure.run_erasure(
        scope=body.scope,
        erasers=_erasure.default_erasers(principal, scope=body.scope),
    )
    await _store.record_deletion(principal, outcome)
    await _store.record_privacy_event(
        principal, "data_deleted",
        {"scope": body.scope, "status": outcome.status},
    )

    payload = outcome.public_dict()
    # Surfaced at the top level as well as inside the summary: a client that
    # renders only a status must not be able to show a tick for a partial
    # deletion.
    payload["complete"] = outcome.complete
    return payload
