"""Companion Creator endpoints — the catalogue and the user's companion profile.

GET  /api/v1/companion/catalogue        -> avatars + voices + personalities, in one call
GET  /api/v1/companion/catalogue/avatars
GET  /api/v1/companion/catalogue/voices
GET  /api/v1/companion/catalogue/personalities
GET  /api/v1/companion/profile          -> the caller's companion (or the default draft)
PUT  /api/v1/companion/profile          -> save a draft
POST /api/v1/companion/profile/activate -> finish the creator
GET  /api/v1/companion/versions         -> what the companion has been, and when

Three properties this module is responsible for:

  **The catalogue is filtered server-side.** `list_avatars` applies the licence
  gate; the response contains only what may be offered. A client cannot obtain
  a refused entry by asking differently, because it is not in the payload.

  **No provider credential reaches the client.** Responses are built from the
  entries' `public_dict()` allow-lists. A voice carries its provider's NAME
  (disclosure the user is entitled to) but not the provider's voice id and
  certainly not a key. Synthesis is proxied through IRA's existing /voice and
  /audio endpoints, which hold the credentials server-side.

  **Selections are re-validated on write.** See `core/companion/service.py`.

The catalogue read endpoints require authentication like everything else. This
is not a public storefront: which avatars and voices a deployment has installed
is deployment configuration, and the honest default for configuration is that
you have to be logged in to read it.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from api.middleware.auth import require_auth
from api.middleware.workspace import require_tenant_principal
from core.identity.principal import Principal
from core.companion import catalogue as _catalogue
from core.companion.models import (
    AUTONOMY_LEVELS,
    MEMORY_MODES,
)
from core.companion.profile import CompanionValidationError
from core.companion.service import (
    build_companion,
    list_versions,
    load_companion,
    save_companion,
)
from core.editions import current_edition

router = APIRouter(prefix="/companion", tags=["companion"])


class CompanionBody(BaseModel):
    """What the Companion Creator posts.

    Every field is optional so the wizard can save after each step. Validation
    of the values themselves happens in core/companion, not here — a Pydantic
    enum would return a 422 listing the vocabulary, which is fine, but it would
    also bypass the single place that decides what is offerable.
    """

    display_name: Optional[str] = Field(None, max_length=200)
    avatar_slug: Optional[str] = Field(None, max_length=64)
    voice_slug: Optional[str] = Field(None, max_length=64)
    personality_slug: Optional[str] = Field(None, max_length=64)
    language: Optional[str] = Field(None, max_length=20)
    form_of_address: Optional[str] = Field(None, max_length=200)
    memory_mode: Optional[str] = Field(None, max_length=20)
    autonomy_level: Optional[str] = Field(None, max_length=40)




# ── Catalogue ────────────────────────────────────────────────────────────────


@router.get("/catalogue/avatars")
async def get_avatars(_user: str = Depends(require_auth)) -> dict[str, Any]:
    edition = current_edition()
    return {
        "edition": edition,
        "avatars": [a.public_dict() for a in _catalogue.list_avatars(edition=edition)],
    }


@router.get("/catalogue/voices")
async def get_voices(_user: str = Depends(require_auth)) -> dict[str, Any]:
    edition = current_edition()
    return {
        "edition": edition,
        "voices": [v.public_dict() for v in _catalogue.list_voices(edition=edition)],
    }


@router.get("/catalogue/personalities")
async def get_personalities(_user: str = Depends(require_auth)) -> dict[str, Any]:
    return {
        "personalities": [p.public_dict() for p in _catalogue.list_personalities()],
    }


@router.get("/catalogue")
async def get_catalogue(_user: str = Depends(require_auth)) -> dict[str, Any]:
    """Everything the creator needs, in one round trip.

    The wizard needs all three lists before its first screen renders; three
    requests to build one form is three chances to render half a catalogue.
    """
    edition = current_edition()
    return {
        "edition": edition,
        "avatars": [a.public_dict() for a in _catalogue.list_avatars(edition=edition)],
        "voices": [v.public_dict() for v in _catalogue.list_voices(edition=edition)],
        "personalities": [p.public_dict() for p in _catalogue.list_personalities()],
        # The vocabularies the wizard's last two steps offer. Served rather than
        # hard-coded in the client so a change to the ladder cannot leave the UI
        # offering a level the backend rejects.
        "memory_modes": list(MEMORY_MODES),
        "autonomy_levels": list(AUTONOMY_LEVELS),
    }


# ── Profile ──────────────────────────────────────────────────────────────────


@router.get("/profile")
async def get_profile(principal: Principal = Depends(require_tenant_principal)) -> dict[str, Any]:
    """The caller's companion.

    Returns a DEFAULT DRAFT rather than 404 when none exists. A user who has
    never opened the creator still has a companion — it is the one they are
    already talking to — and making the client special-case "no profile yet"
    is how the two paths drift apart.
    """
    edition = current_edition()

    profile = await load_companion(principal)
    if profile is None:
        profile = build_companion(edition=edition)
        created = False
    else:
        created = True

    resolved, decision = _catalogue.resolve_avatar(profile.avatar_slug, edition=edition) \
        if profile.avatar_slug else (None, None)

    return {
        "edition": edition,
        "exists": created,
        "profile": profile.public_dict(),
        # What will actually render, which is not always what was chosen: an
        # avatar can be withdrawn after a user selected it. Saying so is the
        # difference between a fallback and a silent substitution.
        "resolved_avatar": resolved.public_dict() if resolved else None,
        "fallback_reason": (
            decision.reason if decision is not None and not decision.allowed else None
        ),
    }


def _merge(body: CompanionBody, current: Any, *, status_value: str) -> dict[str, Any]:
    """Body fields override the current profile; omitted fields keep their value."""
    return {
        "display_name": body.display_name if body.display_name is not None else current.display_name,
        "avatar_slug": body.avatar_slug if body.avatar_slug is not None else current.avatar_slug,
        "voice_slug": body.voice_slug if body.voice_slug is not None else current.voice_slug,
        "personality_slug": (
            body.personality_slug if body.personality_slug is not None else current.personality_slug
        ),
        "language": body.language if body.language is not None else current.language,
        "form_of_address": (
            body.form_of_address if body.form_of_address is not None else current.form_of_address
        ),
        "memory_mode": body.memory_mode if body.memory_mode is not None else current.memory_mode,
        "autonomy_level": (
            body.autonomy_level if body.autonomy_level is not None else current.autonomy_level
        ),
        "status": status_value,
    }


async def _write(principal: Principal, body: CompanionBody, *, status_value: str,
                 reason: str) -> dict[str, Any]:
    edition = current_edition()

    current = await load_companion(principal) or build_companion(edition=edition)

    try:
        profile = build_companion(edition=edition, **_merge(body, current, status_value=status_value))
    except CompanionValidationError as exc:
        # 422 rather than 400: the request was well-formed and a specific field
        # was unacceptable. The field name is returned so the wizard can mark
        # the right step rather than showing a generic error on the last one.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"field": exc.field, "message": exc.message},
        ) from exc

    saved = await save_companion(principal, profile, reason=reason)
    return {"edition": edition, "profile": saved.public_dict()}


@router.put("/profile")
async def put_profile(
    body: CompanionBody,
    principal: Principal = Depends(require_tenant_principal),
) -> dict[str, Any]:
    """Save a draft. Does not activate — the creator's last step does that."""
    return await _write(principal, body, status_value="draft", reason="companion creator: draft saved")


@router.post("/profile/activate")
async def activate_profile(
    body: CompanionBody,
    principal: Principal = Depends(require_tenant_principal),
) -> dict[str, Any]:
    """Finish the creator: validate everything and make this the live companion."""
    return await _write(principal, body, status_value="active", reason="companion creator: activated")


@router.get("/versions")
async def get_versions(
    limit: int = 50,
    principal: Principal = Depends(require_tenant_principal),
) -> dict[str, Any]:
    return {"versions": await list_versions(principal, limit=limit)}
