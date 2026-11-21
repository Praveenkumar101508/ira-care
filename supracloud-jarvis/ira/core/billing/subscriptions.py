"""What a payment event DOES to a subscription.

`080_payment_events.sql` and `payments.py` built an idempotent front door:
verified, recorded exactly once, attributed to a tenant or kept as
`unattributed`. **Nothing consumed the result.** An event arrived, was written
down, and no tenant's subscription moved. This is the missing half.

── The state machine, and why each transition is separate ────────────────────

    (none)          →  a checkout completes            → active
    active          →  a renewal invoice is paid       → active, period rolled
    active          →  a payment fails                 → past_due
    past_due        →  a later payment succeeds        → active
    active/past_due →  cancel at period end            → still active, flagged
    active/past_due →  cancel now                      → canceled
    active          →  the paid period elapses         → expired by TIME

The last one has no event and never will: no provider sends "this subscription
has finished being paid for". It is a fact about the clock, so it is computed at
read time by `is_entitled` rather than written by a job that may not have run.
A status column that depends on a job having run is a status column that is
wrong for as long as the job is down.

── Everything here is idempotent, because delivery is at-least-once ──────────

That is not a corner case, it is the contract every provider publishes. The
front door already refuses to record the same event twice; these functions are
additionally safe to re-run, because "recorded once" and "applied once" are
different guarantees and only the first is enforced by a unique index.

── Authority ────────────────────────────────────────────────────────────────

Nothing here takes a tenant from a request. The webhook path resolves the
tenant from the provider's customer reference or from a checkout session this
system created for a verified principal; the user-facing path resolves it from
the verified principal itself. A `tenant_id` argument on these functions is
always one of those two, never a request field.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.billing.provider import ResolvedPrice
from core.identity.db_context import scheduler_connection
from utils.db import acquire

logger = logging.getLogger("ira.billing.subscriptions")

#: Event types this system acts on, and what each one means here.
#:
#: An event outside this set is recorded and IGNORED, not treated as a failure:
#: providers add event types, and a deployment that broke on an unknown one
#: would break on the provider's release schedule. What it must never do is
#: guess — an unrecognised event changes no subscription state.
CHECKOUT_COMPLETED = "checkout.session.completed"
PAYMENT_SUCCEEDED = "invoice.payment_succeeded"
PAYMENT_FAILED = "invoice.payment_failed"
SUBSCRIPTION_UPDATED = "customer.subscription.updated"
SUBSCRIPTION_DELETED = "customer.subscription.deleted"

ACTED_ON = frozenset({
    CHECKOUT_COMPLETED, PAYMENT_SUCCEEDED, PAYMENT_FAILED,
    SUBSCRIPTION_UPDATED, SUBSCRIPTION_DELETED,
})

#: How long a period runs when the provider does not say. Only ever used for a
#: monthly plan whose event carried no period, which a real provider does not
#: send — it is the fail-safe, and it is deliberately SHORT rather than
#: generous: guessing long grants unpaid access, guessing short asks for a
#: renewal that will arrive.
DEFAULT_PERIOD = timedelta(days=31)


class SubscriptionRefused(RuntimeError):
    """The change was not made. Nothing was written."""


def _utc(value: Any) -> Optional[datetime]:
    """A provider timestamp (unix seconds) as an aware datetime, or None."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


async def resolve_price(plan_id: str, *, at: Optional[datetime] = None,
                        currency: Optional[str] = None) -> Optional[ResolvedPrice]:
    """The price in force for `plan_id`, or None when there is none.

    **None is the answer that means `PRICE_NOT_CONFIGURED`.** It is never zero
    and never a default; `078` chose absence as the representation and this
    respects it.

    `billing_plans` and `billing_plan_prices` are deployment-wide reference
    data with no RLS, so this reads on an unbound connection deliberately — the
    price of a plan is the same fact for every tenant, and reading it through a
    tenant connection would make an unbound caller (the webhook path) see no
    price at all.
    """
    moment = at or datetime.now(timezone.utc)
    async with acquire() as raw:
        conn: Any = raw
        row = await conn.fetchrow(
            "SELECT p.plan_id, pl.display_name, p.currency, p.unit_amount_minor, "
            "       p.recurrence "
            "  FROM billing_plan_prices p "
            "  JOIN billing_plans pl ON pl.plan_id = p.plan_id "
            " WHERE p.plan_id = $1 AND p.effective_from <= $2 "
            "   AND ($3::text IS NULL OR p.currency = $3) "
            " ORDER BY p.effective_from DESC LIMIT 1",
            plan_id, moment, currency,
        )
    if row is None:
        return None
    return ResolvedPrice(
        plan_id=row["plan_id"],
        display_name=row["display_name"],
        currency=row["currency"],
        unit_amount_minor=int(row["unit_amount_minor"]),
        recurrence=row["recurrence"],
    )


def is_entitled(status: str, current_period_end: Optional[datetime]) -> bool:
    """Whether a subscription in this state entitles its tenant to service.

    A pure function of two fields, so the rule is testable without a database
    and stated once rather than re-derived at each call site.

    `past_due` **is** entitled while the period lasts, deliberately: a failed
    card is a payment problem, not a reason to lock somebody out of their own
    data mid-month. What ends access is the paid period elapsing.

    `canceled` is never entitled. A cancellation that should have run to the end
    of the period is represented as `active` with `cancel_at_period_end`, which
    is exactly why that flag is not a status.
    """
    if status not in {"trialing", "active", "past_due"}:
        return False
    if current_period_end is None:
        return False
    return current_period_end > datetime.now(timezone.utc)


async def activate(
    tenant_id: uuid.UUID, *, plan_id: str,
    provider_subscription_ref: Optional[str] = None,
    period_start: Optional[datetime] = None,
    period_end: Optional[datetime] = None,
) -> uuid.UUID:
    """Make this tenant's subscription to `plan_id` active. Idempotent.

    Re-running with the same provider reference rolls the period forward and
    changes nothing else, which is what a redelivered renewal must do.

    ── Which connection, and the defect that decided it (FINDING CG) ──────

    `scheduler_connection(tenant_id)`, not `acquire()`. The first version of
    this function wrote on an UNBOUND connection and reasoned — in a comment,
    confidently — that a bound one "would match nothing". That is backwards:
    `tenant_subscriptions` is FORCE RLS, so an unbound INSERT is **refused**
    outright (`new row violates row-level security policy`) and an unbound
    SELECT matches nothing. Every lifecycle test failed on the first run.

    `scheduler_connection` is the right primitive because the webhook path has
    no principal: the tenant was resolved from the provider's customer reference
    or from a checkout session this system created for a verified principal, and
    inventing a principal would mean choosing a user whose authority to borrow.
    It binds a tenant and NOTHING else — `app.user_id` is cleared — so no
    user-scoped policy can be satisfied through it.
    """
    start = period_start or datetime.now(timezone.utc)
    end = period_end or (start + DEFAULT_PERIOD)
    if end <= start:
        # `078`'s CHECK would reject this anyway; refusing here says which
        # subscription and why, instead of surfacing a constraint name.
        raise SubscriptionRefused(
            f"a subscription period must end after it starts (got {start} → {end})")

    # `scheduler_connection` opens its own transaction, so the read and the
    # write below are already atomic with respect to each other.
    async with scheduler_connection(tenant_id) as raw:
        # One typed local, the shape P14-R3 established. The db_context helpers
        # are annotated as yielding `object`, so every `conn.fetchrow(...)`
        # below is an `attr-defined` error to mypy — thirteen of them across
        # this batch. Naming the type once here is the fix that batch used, and
        # it beats raising the ratchet's ceiling for errors this file
        # introduced.
        conn: Any = raw
        existing = await conn.fetchrow(
            "SELECT id, status FROM tenant_subscriptions "
            " WHERE tenant_id = $1 AND status <> 'canceled' "
            " ORDER BY started_at DESC LIMIT 1",
            tenant_id)

        if existing is None:
            row = await conn.fetchrow(
                "INSERT INTO tenant_subscriptions "
                "  (tenant_id, plan_id, status, current_period_start, "
                "   current_period_end, provider_subscription_ref) "
                "VALUES ($1,$2,'active',$3,$4,$5) RETURNING id",
                tenant_id, plan_id, start, end, provider_subscription_ref)
            logger.info("subscription created tenant=%s plan=%s", tenant_id, plan_id)
            return row["id"]

        await conn.execute(
            "UPDATE tenant_subscriptions "
            "   SET status = 'active', plan_id = $2, "
            "       current_period_start = $3, current_period_end = $4, "
            "       provider_subscription_ref = "
            "           COALESCE($5, provider_subscription_ref), "
            "       cancel_at_period_end = FALSE, "
            "       updated_at = NOW() "
            " WHERE id = $1",
            existing["id"], plan_id, start, end, provider_subscription_ref)
        logger.info("subscription renewed tenant=%s plan=%s", tenant_id, plan_id)
        return existing["id"]


async def mark_past_due(tenant_id: uuid.UUID) -> bool:
    """A payment failed. Returns whether a subscription was moved.

    Does NOT touch `current_period_end`. The tenant has paid up to that date and
    a failed renewal does not un-pay it — moving the date would take away access
    somebody is owed, and `is_entitled` deliberately keeps `past_due` entitled
    until the period runs out.
    """
    async with scheduler_connection(tenant_id) as raw:
        conn: Any = raw
        result = await conn.execute(
            "UPDATE tenant_subscriptions SET status = 'past_due', updated_at = NOW() "
            " WHERE tenant_id = $1 AND status IN ('trialing','active')",
            tenant_id)
    moved = result.endswith(" 1")
    if moved:
        logger.warning("subscription past_due tenant=%s", tenant_id)
    return moved


async def cancel(tenant_id: uuid.UUID, *, at_period_end: bool) -> bool:
    """Cancel. Returns whether a subscription was changed.

    `at_period_end=True` leaves the subscription **active** and sets the flag:
    the tenant has paid for the rest of the period and keeps it. Only
    `at_period_end=False` ends access now.
    """
    async with scheduler_connection(tenant_id) as raw:
        conn: Any = raw
        if at_period_end:
            result = await conn.execute(
                "UPDATE tenant_subscriptions "
                "   SET cancel_at_period_end = TRUE, updated_at = NOW() "
                " WHERE tenant_id = $1 AND status <> 'canceled'",
                tenant_id)
        else:
            result = await conn.execute(
                "UPDATE tenant_subscriptions "
                "   SET status = 'canceled', canceled_at = NOW(), "
                "       cancel_at_period_end = FALSE, updated_at = NOW() "
                " WHERE tenant_id = $1 AND status <> 'canceled'",
                tenant_id)
    changed = result.endswith(" 1")
    if changed:
        logger.info("subscription cancelled tenant=%s at_period_end=%s",
                    tenant_id, at_period_end)
    return changed


async def apply_event(tenant_id: uuid.UUID, event: Any, *,
                      default_plan_id: str = "ira_pro") -> str:
    """Turn one verified, attributed event into a subscription state change.

    Returns a short outcome word for the audit row: `applied` or `ignored`.

    **An unrecognised event type is `ignored`, never an error and never a
    guess.** Providers add event types on their own schedule; a deployment that
    raised on an unknown one would fail on their release notes. What it must
    never do is move a subscription it did not understand.
    """
    event_type = getattr(event, "event_type", "")
    payload = getattr(event, "payload", {}) or {}
    obj = payload.get("data", {})
    obj = obj.get("object", {}) if isinstance(obj, dict) else {}
    if not isinstance(obj, dict):
        obj = {}

    if event_type not in ACTED_ON:
        logger.info("payment event type %r is not one this build acts on", event_type)
        return "ignored"

    if event_type in (CHECKOUT_COMPLETED, PAYMENT_SUCCEEDED):
        await activate(
            tenant_id,
            plan_id=str(obj.get("plan_id") or default_plan_id),
            provider_subscription_ref=(
                str(obj.get("subscription")) if obj.get("subscription") else None),
            period_start=_utc(obj.get("current_period_start")),
            period_end=_utc(obj.get("current_period_end")),
        )
        return "applied"

    if event_type == PAYMENT_FAILED:
        await mark_past_due(tenant_id)
        return "applied"

    if event_type == SUBSCRIPTION_DELETED:
        await cancel(tenant_id, at_period_end=False)
        return "applied"

    # SUBSCRIPTION_UPDATED — the one event that can mean several things. Only
    # the two this system represents are acted on; anything else about the
    # provider's subscription object is deliberately not mirrored, because a
    # field this system does not enforce is a field it should not store.
    if bool(obj.get("cancel_at_period_end")):
        await cancel(tenant_id, at_period_end=True)
        return "applied"
    status = str(obj.get("status") or "")
    if status in {"active", "trialing"}:
        await activate(
            tenant_id,
            plan_id=str(obj.get("plan_id") or default_plan_id),
            provider_subscription_ref=(
                str(obj.get("id")) if obj.get("id") else None),
            period_start=_utc(obj.get("current_period_start")),
            period_end=_utc(obj.get("current_period_end")),
        )
        return "applied"
    if status in {"past_due", "unpaid"}:
        await mark_past_due(tenant_id)
        return "applied"
    if status in {"canceled", "incomplete_expired"}:
        await cancel(tenant_id, at_period_end=False)
        return "applied"

    logger.info("subscription.updated carried status %r; nothing changed", status)
    return "ignored"


__all__ = [
    "ACTED_ON", "CHECKOUT_COMPLETED", "PAYMENT_FAILED", "PAYMENT_SUCCEEDED",
    "SUBSCRIPTION_DELETED", "SUBSCRIPTION_UPDATED", "SubscriptionRefused",
    "activate", "apply_event", "cancel", "is_entitled", "mark_past_due",
    "resolve_price",
]
