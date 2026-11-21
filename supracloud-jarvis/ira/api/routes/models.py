"""Model gateway endpoints — where requests go, what they cost, who looked.

GET  /api/v1/models/providers        -> every provider and its disclosure
GET  /api/v1/models/privacy-modes    -> the modes, described for a settings screen
GET  /api/v1/models/usage            -> where this tenant's requests went
GET  /api/v1/models/admin-access     -> who read my data, when, and why
POST /api/v1/models/admin-access/{id}/revoke -> withdraw a grant

Three properties worth stating.

**Unknown retention is returned as `null`, not `false`.** The provider catalogue
passes NULL through untouched, and `retention_known` is a separate boolean so a
client cannot mistake "not stated" for "does not retain". That distinction is the
whole reason `model_providers.retains_prompts` is nullable.

**There is no endpoint that changes a provider's disclosure.** A retention claim
is a fact about a third party's terms, not a user setting, and an API that let it
be edited would let a deployment mark a retaining provider as clean and route
health notes to it.

**There is no endpoint that grants admin access.** Creating a grant requires the
USER's authorisation, so it belongs to a consent flow, not to an administrative
API. What exists here is the user's view of grants over their own data, and their
ability to revoke — the direction that only ever reduces access.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from api.middleware.auth import require_auth
from api.middleware.workspace import require_tenant_principal
from core.identity.principal import Principal
from core.editions import current_edition
from core.models import store as _store
from core.models.policy import PrivacyMode, describe_mode

logger = logging.getLogger("ira.api.models")

router = APIRouter(prefix="/models", tags=["models"])


@router.get("/providers")
async def providers(principal: Principal = Depends(require_tenant_principal)) -> dict[str, Any]:
    """Every provider this edition enables, with what it does with data.

    `retains_prompts: null` means the provider has not stated its posture.
    `retention_known` is returned alongside so a client cannot render null as
    "No" by accident — which is exactly what the Phase 1 seam prohibits.
    """
    catalogue = await _store.provider_catalogue(principal, edition=current_edition())
    rows = [
        {
            **row,
            "retention_known": row["retains_prompts"] is not None,
            "training_known": row["trains_on_data"] is not None,
            "leaves_this_machine": row["locality"] != "local",
            "serves_requests": False,
        }
        for row in catalogue
    ]

    # ── P8-07: the provider that ACTUALLY serves must appear here ──────────
    #
    # This endpoint answers "where do my requests go?", and until Phase 8 it
    # answered it from the catalogue alone. That was fine while nothing routed
    # anywhere. It stopped being fine the moment the gateway went on the live
    # path, because the two can disagree — `029` seeds `ollama-local` with
    # `enabled_in_cloud = FALSE`, so a cloud deployment serving from a local
    # Ollama would show a provider list that did not contain the provider
    # answering every one of its requests.
    #
    # The fix is not to edit the seed: whether cloud MAY serve from a local
    # Ollama is a product decision, and quietly flipping a flag would make the
    # catalogue agree with the code by changing what the catalogue claims. What
    # is not a product decision is that a user must be able to see where their
    # words went. So the gateway's live providers are merged in, `serves_
    # requests` marks them, and a provider present in one and not the other is
    # visible rather than hidden.
    for disclosure in _live_disclosures():
        existing = next((r for r in rows
                         if r["provider_id"] == disclosure.provider_id), None)
        if existing is not None:
            existing["serves_requests"] = True
            continue
        rows.append({
            "provider_id": disclosure.provider_id,
            "display_name": disclosure.display_name,
            "locality": disclosure.locality.value,
            "retains_prompts": disclosure.retains_prompts,
            "trains_on_data": disclosure.trains_on_data,
            "disclosure_source": "the running configuration",
            "disclosure_checked_at": None,
            "region": disclosure.region,
            "tiers": [],
            "cost_per_1k_prompt_tokens": "0",
            "cost_per_1k_completion_tokens": "0",
            "retention_known": disclosure.retains_prompts is not None,
            "training_known": disclosure.trains_on_data is not None,
            "leaves_this_machine": disclosure.leaves_the_machine,
            "serves_requests": True,
            "not_in_catalogue": True,
        })

    return {
        "providers": rows,
        "note": (
            "Where 'retains prompts' is unknown, IRA treats it as 'may retain' "
            "and will not send anything sensitive there. 'serves_requests' "
            "marks the providers this deployment actually routes to; a "
            "catalogue entry without it is described but unused."
        ),
    }


def _live_disclosures():
    """What the gateway would actually route to, or nothing.

    Never raises: a Privacy Centre that 500s because the gateway could not be
    built tells the user less than one that shows the catalogue alone.
    """
    try:
        from core.models.composition import gateway

        return list(gateway().available_providers())
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read live provider disclosures: %s", exc)
        return []


@router.get("/privacy-modes")
async def privacy_modes(_principal: Principal = Depends(require_tenant_principal)) -> dict[str, Any]:
    """The modes and what each one means, served rather than hardcoded in the
    client so a settings screen cannot promise something the router does not do."""
    return {
        "modes": [
            {"mode": mode.value, "description": describe_mode(mode)}
            for mode in PrivacyMode
        ],
        "strictest": PrivacyMode.LOCAL_ONLY.value,
    }


@router.get("/usage")
async def usage(limit: int = 50, principal: Principal = Depends(require_tenant_principal)) -> dict[str, Any]:
    """Where this tenant's requests went, and what they cost.

    `successful_calls_that_left_this_machine` is a count rather than something a
    user has to infer from provider names they may not recognise.
    """
    return await _store.usage_summary(principal, limit=limit)


@router.get("/admin-access")
async def admin_access(principal: Principal = Depends(require_tenant_principal)) -> dict[str, Any]:
    """Who has read this user's content, when, why, and when they authorised it.

    Expired and revoked grants are included: "who has looked at my data?" is not
    answered by a list of who can look right now.
    """
    grants = await _store.grants_for_user(principal)
    reads = await _store.access_log_for_user(principal)
    return {
        "grants": grants,
        "reads": reads,
        "note": (
            "An administrator can only read your content inside a window you "
            "authorised, and every read appears here. If you see a read you do "
            "not recognise, revoke the grant."
            if grants else
            "No administrator has ever been authorised to read your content."
        ),
    }


@router.post("/admin-access/{grant_id}/revoke")
async def revoke(grant_id: str, principal: Principal = Depends(require_tenant_principal)) -> dict[str, Any]:
    """Withdraw a grant. The only direction this API moves access.

    Revoking is immediate: the database refuses to log any further read against a
    revoked grant, so this does not depend on an administrator's client noticing.
    """
    try:
        wanted = uuid.UUID(grant_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="no such grant") from exc
    revoked = await _store.revoke_grant(principal, wanted)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no such grant, or it was already revoked",
        )
    return {
        "grant_id": grant_id,
        "revoked": True,
        "detail": (
            "Revoked. No further reads can be recorded against this "
            "authorisation, and the database will refuse them."
        ),
    }
