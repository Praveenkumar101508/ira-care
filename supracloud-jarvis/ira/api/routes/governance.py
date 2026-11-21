"""Constitution and permission endpoints — the user's rules, made visible.

GET    /api/v1/governance/constitution      -> the caller's rules, in plain language
POST   /api/v1/governance/constitution      -> add a rule
PATCH  /api/v1/governance/constitution/{id} -> enable/disable a rule
GET    /api/v1/governance/starter-rules     -> the rules offered at onboarding
POST   /api/v1/governance/starter-rules     -> install the ones the user chose
GET    /api/v1/governance/policies          -> what this deployment permits at all
GET    /api/v1/governance/grants            -> what the user has granted
DELETE /api/v1/governance/grants/{id}       -> revoke a grant
GET    /api/v1/governance/decisions         -> why IRA allowed or refused things
POST   /api/v1/governance/preview           -> what WOULD happen, executing nothing

Two properties worth stating explicitly.

**`/preview` never executes.** It runs the broker and returns the decision. Any
ticket the broker mints is discarded rather than returned — publishing a ticket
over HTTP would hand a client the one object that authorises execution, which
would undo the entire point of `ticket.py`.

**`/policies` is read-only, for everyone.** The deployment baseline is not
user-editable, by design: a user's Constitution may only make things stricter.
There is deliberately no endpoint to loosen a policy — see ADR-0003.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from api.middleware.auth import require_auth
from api.middleware.workspace import require_tenant_principal
from core.identity.principal import Principal
from core.governance import constitution as _constitution
from core.governance import policies as _policies
from core.governance import store as _store
from core.governance.rules import STARTER_RULES, InvalidRule
from core.interfaces.permission import ActionRequest

router = APIRouter(prefix="/governance", tags=["governance"])


class RuleBody(BaseModel):
    effect: str = Field(..., max_length=40)
    domain: str = Field("*", max_length=64)
    action: str = Field("*", max_length=64)
    conditions: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100
    enabled: bool = True
    label: str = Field("", max_length=200)


class RuleToggleBody(BaseModel):
    enabled: bool


class StarterBody(BaseModel):
    labels: list[str] = Field(default_factory=list)


class PreviewBody(BaseModel):
    domain: str = Field(..., max_length=64)
    action: str = Field(..., max_length=64)
    purpose: str = Field(..., min_length=1, max_length=500)
    resource: str = Field("", max_length=1000)
    model_proposed: bool = True


# ── Constitution ─────────────────────────────────────────────────────────────


@router.get("/constitution")
async def get_constitution(principal: Principal = Depends(require_tenant_principal)) -> dict[str, Any]:
    """The caller's rules, plus a plain-language reading of each.

    `summarise` reads the STRUCTURE rather than the stored label, so what a user
    sees here is what the engine will actually do. A label that has drifted from
    its rule shows up as a discrepancy instead of being echoed back as truth.
    """
    rules = await _store.load_rules(principal)
    return {
        "rules": [
            {
                "id": str(r.rule_id) if r.rule_id else None,
                "domain": r.domain,
                "action": r.action,
                "effect": r.effect.value,
                "conditions": dict(r.conditions),
                "priority": r.priority,
                "enabled": r.enabled,
                "label": r.label,
                "origin": r.origin,
            }
            for r in rules
        ],
        "plain_language": _constitution.summarise(rules),
    }


@router.post("/constitution", status_code=status.HTTP_201_CREATED)
async def add_rule(
    body: RuleBody, principal: Principal = Depends(require_tenant_principal)
) -> dict[str, Any]:
    try:
        rule_id = await _store.save_rule(principal, body.model_dump())
    except InvalidRule as exc:
        # 422 with the actual reason. "Invalid rule" alone teaches nobody
        # anything, and this is a form a user fills in themselves.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return {"id": str(rule_id)}


@router.patch("/constitution/{rule_id}")
async def toggle_rule(
    rule_id: uuid.UUID,
    body: RuleToggleBody,
    principal: Principal = Depends(require_tenant_principal),
) -> dict[str, Any]:
    changed = await _store.set_rule_enabled(principal, rule_id, body.enabled)
    if not changed:
        # 404 for both "not yours" and "does not exist" — RLS makes them
        # indistinguishable here, which is the correct amount to disclose.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such rule")
    return {"id": str(rule_id), "enabled": body.enabled}


@router.get("/starter-rules")
async def get_starter_rules(_user: str = Depends(require_auth)) -> dict[str, Any]:
    """The rules offered during onboarding. Offered, never imposed."""
    return {"starter_rules": [dict(r) for r in STARTER_RULES]}


@router.post("/starter-rules")
async def install_starter_rules(
    body: StarterBody, principal: Principal = Depends(require_tenant_principal)
) -> dict[str, Any]:
    installed = await _store.seed_starter_rules(principal, body.labels)
    return {"installed": installed}


# ── Policies ─────────────────────────────────────────────────────────────────


@router.get("/policies")
async def get_policies(_user: str = Depends(require_auth)) -> dict[str, Any]:
    """What this deployment permits at all — read-only.

    `never_automatic` is derived from the policies rather than listed
    separately, so the two cannot disagree.
    """
    return {
        "policies": [
            {
                "domain": p.domain,
                "action": p.action,
                "max_effect": p.max_effect.value,
                "default_effect": p.default_effect.value,
                "risk": p.risk.value,
                "side_effecting": p.side_effecting,
                "description": p.description,
            }
            for p in _policies.all_policies()
        ],
        "never_automatic": [
            f"{d}.{a}" for d, a in _policies.never_automatic()
        ],
    }


# ── Grants ───────────────────────────────────────────────────────────────────


@router.get("/grants")
async def get_grants(principal: Principal = Depends(require_tenant_principal)) -> dict[str, Any]:
    """Every grant, including inert ones, with WHY each is inert.

    Expired and revoked grants are shown rather than hidden: "you never granted
    this" and "you revoked this" are different sentences, and a user auditing
    their own permissions needs to see both.
    """
    grants = await _store.load_grants(principal)
    return {
        "grants": [
            {
                "id": str(g.grant_id) if g.grant_id else None,
                "connector": g.connector,
                "resource": g.resource,
                "scope": g.scope,
                "purpose": g.purpose,
                "status": g.status(),
                "confirmation_policy": g.confirmation_policy,
                "retention_policy": g.retention_policy,
                "granted_at": g.granted_at.isoformat() if g.granted_at else None,
                "expires_at": g.expires_at.isoformat() if g.expires_at else None,
                "revoked_at": g.revoked_at.isoformat() if g.revoked_at else None,
            }
            for g in grants
        ]
    }


@router.delete("/grants/{grant_id}")
async def revoke(
    grant_id: uuid.UUID, principal: Principal = Depends(require_tenant_principal)
) -> dict[str, Any]:
    revoked = await _store.revoke_grant(principal, grant_id)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such active grant",
        )
    return {"id": str(grant_id), "status": "revoked"}


# ── Decisions ────────────────────────────────────────────────────────────────


@router.get("/decisions")
async def get_decisions(
    limit: int = 50, principal: Principal = Depends(require_tenant_principal)
) -> dict[str, Any]:
    """Why IRA allowed or refused things. Includes refusals, deliberately."""
    return {"decisions": await _store.recent_decisions(principal, limit=limit)}


@router.post("/preview")
async def preview(
    body: PreviewBody, principal: Principal = Depends(require_tenant_principal)
) -> dict[str, Any]:
    """What WOULD be decided for this action. Executes nothing.

    The ticket the broker may mint is deliberately NOT returned. Publishing one
    over HTTP would hand a client the object that authorises execution — the
    ticket exists precisely so that permission and execution stay welded
    together inside the process.
    """
    broker = await _store.load_broker(principal)

    request = ActionRequest(
        principal=principal,
        domain=body.domain.strip().lower(),
        action=body.action.strip().lower(),
        purpose=body.purpose,
        resource=body.resource,
        model_proposed=body.model_proposed,
    )
    outcome = broker.evaluate(request)
    await _store.record_decision(principal, request, outcome)

    return {
        "effect": outcome.decision.effect.value,
        "risk": outcome.decision.risk.value,
        "reason": outcome.decision.reason,
        "source": outcome.decision.source,
        "would_execute": outcome.may_execute,
        "needs_confirmation": outcome.decision.needs_confirmation,
        "matched_rule": (
            outcome.matched_rule.label or None if outcome.matched_rule else None
        ),
    }
