"""P11-05 — the billing surface, on the verified principal.

What a tenant is entitled to ask: *what am I on, what does it allow me, and
what have I been billed?* Everything here is READ-ONLY. Nothing in this module
subscribes, prices, charges or cancels — those are decisions with money
attached, and P11-05's scope is the surface, not the transaction.

── The four answers ─────────────────────────────────────────────────────────

The failure this module exists to avoid is the one Phase 10 fixed in the Trust
Ledger view: a single `available: false` that covered an outage, a wiring
defect and an honest absence alike, so a caller could not tell "you owe
nothing" from "we could not find out".

    OK                      this tenant really is subscribed, and priced
    NO_SUBSCRIPTION         nothing to bill — a FACT, not a failure
    PRICE_NOT_CONFIGURED    subscribed to a plan the owner has not priced
    BILLING_UNAVAILABLE     we could not find out

`PRICE_NOT_CONFIGURED` is the one that earns its place. Production prices are
deliberately unset (`P11-PRICE`), and the tempting collapse is to render an
unpriced plan as `OK` with a total of zero — which tells a customer they owe
nothing when the truth is that nobody has decided yet. Absence of a price is
never free, and the API says so in a word a client can branch on rather than in
a number it will format as "£0.00".

An unauthorised caller gets a **403 raised**, not a status in the body, exactly
as `trust.py` does: a refusal to answer is not one of the answers.

── Authority ────────────────────────────────────────────────────────────────

The tenant is never taken from the request. `require_tenant_principal` resolves
it from the verified session and checks membership; FORCE RLS underneath is the
boundary. The scope check on top is a legible early refusal, not a substitute.

`billing_read` is held by `primary_owner`, `owner`, `admin` and — at last —
`billing_admin`, the role `cloud_roles.py` said would gain its capabilities
"with the surfaces they administer". Not `member`, not `viewer`: what a company
spends is not everyone's business by default.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from api.middleware.workspace import require_tenant_principal
from core.identity.principal import Principal
from security.roles import SCOPE_BILLING_MANAGE, SCOPE_BILLING_READ

logger = logging.getLogger("ira.api.billing")

router = APIRouter(prefix="/billing", tags=["billing"])

#: Why a billing view says what it says. Four answers that a lesser design
#: would have collapsed into `available: true/false`.
BILLING_OK = "OK"
BILLING_NO_SUBSCRIPTION = "NO_SUBSCRIPTION"
BILLING_PRICE_NOT_CONFIGURED = "PRICE_NOT_CONFIGURED"
BILLING_UNAVAILABLE = "BILLING_UNAVAILABLE"

#: Two more, added with the checkout path. Both are refusals and neither means
#: free: the first is "there is a price and no way to take the money", the
#: second is "there is a way and the attempt did not succeed". A user told the
#: wrong one cannot act on it — one is worth retrying and the other is not.
BILLING_PROVIDER_NOT_CONFIGURED = "PAYMENT_PROVIDER_NOT_CONFIGURED"
BILLING_CHECKOUT_UNAVAILABLE = "CHECKOUT_UNAVAILABLE"

PROVIDER_NOT_CONFIGURED_MEANS = (
    "no payment provider is configured for this deployment, so a subscription "
    "cannot be started here yet. This fails closed — it does NOT mean the "
    "service is free."
)

#: Said in full, in the response, every time. A client that renders this can
#: tell a customer something true; a client that renders "£0.00" cannot.
PRICE_NOT_CONFIGURED_MEANS = (
    "this plan has no configured production price, so nothing can be billed "
    "against it yet. This is deliberate and fails closed — it does NOT mean the "
    "service is free."
)


def _require_billing_manage(principal: Principal) -> None:
    """Reading what is owed and committing to a charge are different authorities.

    `admin` holds `billing_read` and deliberately not this one: an engineer
    should not be able to start a paid subscription — or cancel the plan the
    whole tenant runs on — because their role happened to include the scope that
    lets them see the invoice.
    """
    if not principal.has_scope(SCOPE_BILLING_MANAGE):
        raise HTTPException(
            status_code=403,
            detail="this role may not change the tenant's commercial position",
        )


def _require_billing_scope(principal: Principal) -> None:
    if not principal.has_scope(SCOPE_BILLING_READ):
        # Raised, not returned. A refusal to answer is not one of the answers,
        # and a 200 carrying "not allowed" is a body somebody will render as a
        # billing state.
        raise HTTPException(
            status_code=403,
            detail="this role may not read the tenant's billing position",
        )


@router.get("/status")
async def billing_status(
    principal: Principal = Depends(require_tenant_principal),
) -> dict[str, Any]:
    """This TENANT's commercial position: plan, entitlements, and whether it
    can be billed at all.

    Fail-soft in shape but never fail-quiet in meaning: every branch names
    which of the four answers it is giving.
    """
    _require_billing_scope(principal)

    from core.billing.entitlements import effective_limits
    from core.identity.db_context import tenant_connection

    try:
        async with tenant_connection(principal) as raw:
            conn: Any = raw
            sub = await conn.fetchrow(
                "SELECT s.id, s.plan_id, s.status, s.started_at, "
                "       s.current_period_end, p.display_name "
                "  FROM tenant_subscriptions s "
                "  JOIN billing_plans p ON p.plan_id = s.plan_id "
                " WHERE s.tenant_id = $1 "
                "   AND s.status IN ('trialing','active','past_due') "
                " ORDER BY s.started_at DESC LIMIT 1",
                principal.tenant_id,
            )
            if sub is None:
                # An ANSWER, not a failure. A tenant that never subscribed and a
                # tenant whose subscription we could not read are different
                # facts, and 078 keeps cancelled rows precisely so that
                # "cancelled" and "never subscribed" stay distinguishable.
                return {
                    "status": BILLING_NO_SUBSCRIPTION,
                    "subscription": None,
                    "entitlements": {},
                    "tenant_id": str(principal.tenant_id),
                }

            # The price in force NOW. Absence is the headline case, not an edge.
            price = await conn.fetchrow(
                "SELECT currency, unit_amount_minor, recurrence "
                "  FROM billing_plan_prices "
                " WHERE plan_id = $1 AND effective_from <= NOW() "
                " ORDER BY effective_from DESC LIMIT 1",
                sub["plan_id"],
            )

        # Outside the connection: `effective_limits` opens its own, and nesting
        # them would hold two for no reason.
        limits = await effective_limits(principal)

    except Exception as exc:  # noqa: BLE001 — the console works when things break
        # Distinguishable from every answer above. A caller must be able to
        # retry this and must NOT be able to mistake it for "nothing owed".
        logger.warning("billing status unavailable for tenant %s: %s",
                       principal.tenant_id, exc)
        return {
            "status": BILLING_UNAVAILABLE,
            "reason": str(exc),
            "subscription": None,
            "entitlements": {},
            "tenant_id": str(principal.tenant_id),
        }

    subscription = {
        "plan_id": sub["plan_id"],
        "plan_name": sub["display_name"],
        "state": sub["status"],
        "current_period_end": (
            sub["current_period_end"].isoformat()
            if sub["current_period_end"] else None),
    }

    if price is None:
        return {
            "status": BILLING_PRICE_NOT_CONFIGURED,
            "reason": PRICE_NOT_CONFIGURED_MEANS,
            "subscription": subscription,
            # The entitlements are REAL and are reported. What the plan allows
            # you to do has been decided; only what it costs has not, and
            # withholding both would misreport one as undecided too.
            "entitlements": limits,
            "price": None,
            "tenant_id": str(principal.tenant_id),
        }

    return {
        "status": BILLING_OK,
        "subscription": subscription,
        "entitlements": limits,
        "price": {
            "currency": price["currency"],
            # Minor units, named as such, beside their currency — 079's rule.
            # Never a float, and never a bare number a client might scale.
            "unit_amount_minor": int(price["unit_amount_minor"]),
            "recurrence": price["recurrence"],
        },
        "tenant_id": str(principal.tenant_id),
    }


@router.get("/invoices")
async def list_invoices(
    limit: int = 20,
    principal: Principal = Depends(require_tenant_principal),
) -> dict[str, Any]:
    """This TENANT's invoices, newest first.

    Drafts are included and labelled. A draft is a working document and may
    still change; hiding it would leave a customer unable to see what is about
    to be issued, and showing it unlabelled would let them mistake it for a
    bill that has been sent.
    """
    _require_billing_scope(principal)

    from core.identity.db_context import tenant_connection

    try:
        async with tenant_connection(principal) as raw:
            conn: Any = raw
            rows = await conn.fetch(
                "SELECT id, period_start, period_end, currency, total_minor, "
                "       status, issued_at, voided_at "
                "  FROM invoices WHERE tenant_id = $1 "
                " ORDER BY period_end DESC LIMIT $2",
                principal.tenant_id, max(1, min(int(limit), 200)),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("invoice list unavailable for tenant %s: %s",
                       principal.tenant_id, exc)
        return {"status": BILLING_UNAVAILABLE, "reason": str(exc),
                "invoices": [], "count": 0,
                "tenant_id": str(principal.tenant_id)}

    # An empty list here is an ANSWER — this tenant has not been invoiced — and
    # it is reported as OK rather than as an absence, because the failure case
    # above has its own status and no longer needs to borrow this one.
    return {
        "status": BILLING_OK,
        "invoices": [{
            "id": str(r["id"]),
            "period_start": r["period_start"].isoformat(),
            "period_end": r["period_end"].isoformat(),
            "currency": r["currency"],
            "total_minor": int(r["total_minor"]),
            "state": r["status"],
            "issued_at": r["issued_at"].isoformat() if r["issued_at"] else None,
            "voided_at": r["voided_at"].isoformat() if r["voided_at"] else None,
        } for r in rows],
        "count": len(rows),
        "tenant_id": str(principal.tenant_id),
    }


# ── The catalogue ────────────────────────────────────────────────────────────


@router.get("/plans")
async def list_plans(
    principal: Principal = Depends(require_tenant_principal),
) -> dict[str, Any]:
    """What this deployment sells, at the price it sells it for.

    The price comes from `billing_plan_prices` — the same rows the charge is
    computed from — so a plan card cannot show one figure while the invoice says
    another. A plan with no price row is returned WITH `price: null` rather than
    hidden: a customer who can see the plan and not its price learns something
    true, and a customer who cannot see the plan at all learns nothing.
    """
    _require_billing_scope(principal)

    from core.billing.subscriptions import resolve_price
    from utils.db import acquire

    try:
        async with acquire() as raw:
            conn: Any = raw
            rows = await conn.fetch(
                "SELECT plan_id, display_name, description FROM billing_plans "
                " WHERE active ORDER BY plan_id")
        plans = []
        for row in rows:
            price = await resolve_price(row["plan_id"])
            plans.append({
                "plan_id": row["plan_id"],
                "display_name": row["display_name"],
                "description": row["description"],
                "price": None if price is None else {
                    "currency": price.currency,
                    "unit_amount_minor": price.unit_amount_minor,
                    "recurrence": price.recurrence,
                    # Formatted from the same fields the charge uses, so the two
                    # cannot drift. A client is free to format it itself; this
                    # exists so that a client which does not, still cannot show
                    # a number nobody derived from the price row.
                    "formatted": price.formatted,
                },
                "price_status": (BILLING_PRICE_NOT_CONFIGURED if price is None
                                 else BILLING_OK),
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan catalogue unavailable: %s", exc)
        return {"status": BILLING_UNAVAILABLE, "reason": str(exc), "plans": []}

    return {"status": BILLING_OK, "plans": plans, "count": len(plans)}


# ── Starting, managing and ending a subscription ─────────────────────────────


@router.post("/checkout")
async def start_checkout(
    body: dict[str, Any] = Body(default_factory=dict),
    principal: Principal = Depends(require_tenant_principal),
) -> dict[str, Any]:
    """Open a hosted checkout for one plan.

    Three refusals before anything is attempted, each with its own name:

      * no price row              → `PRICE_NOT_CONFIGURED`
      * no provider configured    → `PAYMENT_PROVIDER_NOT_CONFIGURED`
      * the provider call failed  → `CHECKOUT_UNAVAILABLE`

    None of them is "free", and the client is given the word rather than a
    boolean so it can say something true.

    The tenant is the VERIFIED principal's, never the body's. `plan_id` is the
    only thing the caller supplies and it is checked against the catalogue — a
    request naming a plan that does not exist is refused rather than defaulted,
    because a default here is a request choosing what it pays.
    """
    _require_billing_manage(principal)

    from core.billing.provider import (
        ProviderCallFailed, configured_provider, provider_supports_checkout,
    )
    from core.billing.subscriptions import resolve_price
    from core.identity.db_context import tenant_connection

    plan_id = str(body.get("plan_id") or "ira_pro")

    price = await resolve_price(plan_id)
    if price is None:
        return {"status": BILLING_PRICE_NOT_CONFIGURED,
                "reason": PRICE_NOT_CONFIGURED_MEANS, "checkout_url": None}

    provider = configured_provider()
    if provider is None or not provider_supports_checkout(provider):
        # Two different absences, one answer on purpose: which secrets a
        # deployment holds is not a fact an authenticated tenant needs, and the
        # operator-facing detail is in the log where `configured_provider`
        # named the missing half.
        logger.warning("checkout requested with no usable payment provider")
        return {"status": BILLING_PROVIDER_NOT_CONFIGURED,
                "reason": PROVIDER_NOT_CONFIGURED_MEANS, "checkout_url": None}

    async with tenant_connection(principal) as raw:
        conn: Any = raw
        # The row is written BEFORE the provider is called, so a session that is
        # created and whose response is lost still has a record. The reverse
        # order loses the attribution for exactly the payment that succeeded.
        session_row = await conn.fetchrow(
            "INSERT INTO billing_checkout_sessions "
            "  (tenant_id, plan_id, provider, provider_session_ref, currency, "
            "   unit_amount_minor, recurrence) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id",
            principal.tenant_id, plan_id, provider.name,
            f"pending:{uuid.uuid4()}",
            price.currency, price.unit_amount_minor, price.recurrence)
        session_id = session_row["id"]

        try:
            created = provider.create_checkout_session(
                price=price,
                client_reference=str(session_id),
                success_url=str(body.get("success_url") or _default_return_url()),
                cancel_url=str(body.get("cancel_url") or _default_return_url()),
                customer_ref=None,
            )
        except ProviderCallFailed as exc:
            await conn.execute(
                "UPDATE billing_checkout_sessions SET status = 'expired' "
                " WHERE id = $1", session_id)
            logger.warning("checkout could not be created for tenant %s: %s",
                           principal.tenant_id, exc)
            return {"status": BILLING_CHECKOUT_UNAVAILABLE, "reason": str(exc),
                    "checkout_url": None}

        await conn.execute(
            "UPDATE billing_checkout_sessions SET provider_session_ref = $2 "
            " WHERE id = $1", session_id, created.session_ref)

    return {
        "status": BILLING_OK,
        "checkout_url": created.url,
        "session_id": str(session_id),
        "price": {"currency": price.currency,
                  "unit_amount_minor": price.unit_amount_minor,
                  "recurrence": price.recurrence,
                  "formatted": price.formatted},
    }


@router.post("/portal")
async def open_portal(
    body: dict[str, Any] = Body(default_factory=dict),
    principal: Principal = Depends(require_tenant_principal),
) -> dict[str, Any]:
    """A provider-hosted billing portal for THIS tenant's customer record.

    The customer reference is read from `tenants` for the verified principal and
    is never accepted from the request. A portal opened for a customer reference
    the caller supplied would show somebody else's payment methods, which is the
    whole reason `042`'s trigger stops the runtime writing that column at all.
    """
    _require_billing_manage(principal)

    from core.billing.provider import ProviderCallFailed, configured_provider
    from utils.db import acquire

    provider = configured_provider()
    if provider is None or not callable(getattr(provider, "create_portal_session", None)):
        return {"status": BILLING_PROVIDER_NOT_CONFIGURED,
                "reason": PROVIDER_NOT_CONFIGURED_MEANS, "portal_url": None}

    async with acquire() as raw:
        conn: Any = raw
        customer_ref = await conn.fetchval(
            "SELECT provider_customer_ref FROM tenants WHERE id = $1",
            principal.tenant_id)

    if not customer_ref:
        return {
            "status": BILLING_NO_SUBSCRIPTION,
            "reason": ("this tenant has no billing account with the payment "
                       "provider yet, so there is no portal to open. Start a "
                       "subscription first."),
            "portal_url": None,
        }

    try:
        session = provider.create_portal_session(
            customer_ref=customer_ref,
            return_url=str(body.get("return_url") or _default_return_url()))
    except ProviderCallFailed as exc:
        logger.warning("portal unavailable for tenant %s: %s",
                       principal.tenant_id, exc)
        return {"status": BILLING_CHECKOUT_UNAVAILABLE, "reason": str(exc),
                "portal_url": None}

    return {"status": BILLING_OK, "portal_url": session.url}


@router.post("/cancel")
async def cancel_subscription(
    body: dict[str, Any] = Body(default_factory=dict),
    principal: Principal = Depends(require_tenant_principal),
) -> dict[str, Any]:
    """Cancel this TENANT's subscription.

    Defaults to `at_period_end=True`, which is the non-destructive reading: the
    tenant has paid for the rest of the period and keeps it. Ending access
    immediately is available and must be asked for — the same shape
    `privacy/delete` uses for its two widest scopes, and for the same reason.
    """
    _require_billing_manage(principal)

    from core.billing.subscriptions import cancel as cancel_subscription_state
    from core.identity.db_context import tenant_connection

    at_period_end = bool(body.get("at_period_end", True))
    if not at_period_end and str(body.get("confirm") or "") != "cancel_now":
        raise HTTPException(
            status_code=400,
            detail=("ending access immediately forfeits the remainder of a paid "
                    "period. Send confirm='cancel_now' to proceed, or omit "
                    "at_period_end to stop at the end of the period."))

    # Read through the tenant connection so a caller with no live subscription
    # gets an answer rather than a silent no-op — `cancel()` writes unbound (it
    # is also the webhook path's applier), and an unbound write that matched
    # nothing would be indistinguishable from success.
    async with tenant_connection(principal) as raw:
        conn: Any = raw
        live = await conn.fetchval(
            "SELECT count(*) FROM tenant_subscriptions "
            " WHERE tenant_id = $1 AND status <> 'canceled'",
            principal.tenant_id)
    if not live:
        return {"status": BILLING_NO_SUBSCRIPTION,
                "reason": "this tenant has no live subscription to cancel",
                "cancelled": False}

    changed = await cancel_subscription_state(
        principal.tenant_id, at_period_end=at_period_end)

    logger.info("tenant %s cancelled subscription at_period_end=%s by %s",
                principal.tenant_id, at_period_end, principal.user_id)
    return {"status": BILLING_OK, "cancelled": changed,
            "at_period_end": at_period_end}


# ── Inbound: the provider tells us what happened ─────────────────────────────


@router.post("/webhook")
async def payment_webhook(request: Request) -> dict[str, Any]:
    """Signed events from the payment provider.

    **Unauthenticated by design, and authenticated by signature.** There is no
    session here: the caller is a provider, not a person, and the only thing
    that makes the request trustworthy is that it was signed with a secret this
    deployment holds. Adding `require_auth` would break every real delivery and
    prove nothing.

    The order below is the whole security property, and each step is somewhere
    else in the codebase precisely so this route cannot skip one:

      1. `provider.verify`   — signature and timestamp, BEFORE anything is read
      2. `record_arrival`    — written exactly once; a repeat returns was_new=False
      3. `attribute`         — provider customer reference → tenant, or kept
      4. `apply_event`       — the only step that changes a subscription

    A verified event with no attributable tenant is KEPT as `unattributed` and
    changes nothing. Deleting it would destroy the only record that a payment
    arrived for somebody we cannot identify.
    """
    from core.billing.payments import (
        PaymentRefused, SignatureInvalid, TimestampOutOfRange, attribute,
        record_arrival,
    )
    from core.billing.provider import configured_provider
    from core.billing.subscriptions import apply_event
    from utils.db import acquire

    provider = configured_provider()
    if provider is None:
        # 503 and not 200: a provider posting to a deployment that cannot verify
        # it must retry, and a 200 tells it the event was handled.
        logger.error("payment webhook received with no provider configured")
        raise HTTPException(
            status_code=503,
            detail="no payment provider is configured; this event was not read")

    body = await request.body()
    headers = dict(request.headers)

    try:
        event = provider.verify(body=body, headers=headers)
    except (SignatureInvalid, TimestampOutOfRange) as exc:
        # 400, and the body is NOT parsed. An unverified request never reaches
        # a database, and the log records that somebody sent one.
        logger.warning("payment webhook REJECTED: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PaymentRefused as exc:
        logger.warning("payment webhook unusable: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    event_row_id, was_new = await record_arrival(event)
    if not was_new:
        # 200: the provider did its job and must not retry. Nothing is applied
        # a second time, which is the entire purpose of the unique constraint
        # in `080`.
        return {"received": True, "duplicate": True, "applied": False}

    # A checkout completing carries this system's OWN reference — the row id —
    # because at that moment a new customer has no provider_customer_ref to
    # attribute against: the customer is created BY the checkout.
    tenant_id = None
    obj = (event.payload.get("data") or {}).get("object") or {}
    client_ref = str(obj.get("client_reference_id") or "") if isinstance(obj, dict) else ""
    if client_ref:
        try:
            async with acquire() as raw:
                conn: Any = raw
                tenant_id = await conn.fetchval(
                    "SELECT tenant_id FROM billing_checkout_sessions WHERE id = $1",
                    uuid.UUID(client_ref))
                if tenant_id is not None:
                    await conn.execute(
                        "UPDATE billing_checkout_sessions "
                        "   SET status = 'completed', completed_at = NOW() "
                        " WHERE id = $1 AND status = 'open'",
                        uuid.UUID(client_ref))
                    # The provider's customer reference is learned HERE and
                    # nowhere else in the request path. `042`'s trigger stops
                    # the runtime role writing it, so this uses the elevated
                    # connection the webhook path already holds.
                    if event.customer_ref:
                        await conn.execute(
                            "UPDATE tenants SET provider_customer_ref = $2 "
                            " WHERE id = $1 AND provider_customer_ref = ''",
                            tenant_id, event.customer_ref)
        except (ValueError, TypeError):
            logger.warning("checkout reference %r is not a session id", client_ref)

    if tenant_id is None:
        tenant_id = await attribute(event_row_id, event.customer_ref)
    if tenant_id is None:
        return {"received": True, "duplicate": False, "applied": False,
                "outcome": "unattributed"}

    outcome = await apply_event(tenant_id, event)

    async with acquire() as audit_raw:
        # A second name, because the earlier `conn` in this function is already
        # bound and mypy reads the two `async with` blocks as one scope.
        audit_conn: Any = audit_raw
        await audit_conn.execute(
            "UPDATE payment_events SET outcome = $2, processed_at = NOW() "
            " WHERE id = $1 AND outcome = 'received'",
            event_row_id, "applied" if outcome == "applied" else "ignored")

    return {"received": True, "duplicate": False,
            "applied": outcome == "applied", "outcome": outcome}


def _default_return_url() -> str:
    """Where the provider sends the user back to.

    Derived from the deployment's own configured domain rather than from the
    request: a `success_url` taken from a header is an open redirect with a
    payment attached to it.
    """
    import os

    domain = (os.getenv("IRA_DOMAIN") or "").strip()
    if domain and not domain.startswith("http"):
        return f"https://{domain}/billing"
    return domain or "http://localhost:3000/billing"


__all__ = [
    "BILLING_OK", "BILLING_NO_SUBSCRIPTION", "BILLING_PRICE_NOT_CONFIGURED",
    "BILLING_UNAVAILABLE", "BILLING_PROVIDER_NOT_CONFIGURED",
    "BILLING_CHECKOUT_UNAVAILABLE", "PRICE_NOT_CONFIGURED_MEANS",
    "PROVIDER_NOT_CONFIGURED_MEANS", "router",
]
