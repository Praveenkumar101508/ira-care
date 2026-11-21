"""What a tenant's plan entitles it to — resolved, and applied to the quota.

── Where this runs, and why that is the whole point (P11-02) ────────────────

Phase 9 was reported closed while its governance machinery had no product
caller. The reconciliation (PR #121) found it and fixed it. This module is
written so that failure cannot recur here: it is called from
`core.models.store.load_quota`, which is wired at `core/models/composition.py`
as the gateway's `quota_reader`, which `IRAModelGateway._admit()` calls at step
2 of the admission sequence that `complete()` and `stream()` both share.

    real request → _admit() step 2 → quota_reader → load_quota
                 → THIS MODULE → narrowed ceiling → check_quota → refuse or act

So an entitlement is not a row somebody might consult. It is on the path every
inference request already takes, before the provider is chosen and before any
tokens are spent.

── Entitlements NARROW the quota; they never replace it ─────────────────────

`usage_quotas` (029) and the atomic reservation (032) are
IMPLEMENTED_AND_VERIFIED and keep doing exactly what they do. This layer can
only make a ceiling LOWER, never higher, and the consequences are deliberate:

    no subscription            → no plan → no entitlement rows → the quota is
                                 returned untouched, so a deployment that never
                                 sells anything behaves exactly as it does today
    entitlement stricter       → the entitlement binds
    quota stricter             → the quota binds
    entitlement NULL           → UNLIMITED. Does NOT loosen an existing quota.
    entitlement 0              → NONE. Binds, and refuses everything.

**NULL and 0 are different values and are kept different.** `plan_entitlements`
CHECKs that, `078`'s comment says why, and this module is where getting it wrong
would actually hurt: collapsing them turns an unlimited plan into a blocked one,
or a blocked one into an unlimited one, and from outside those look like the
same bug.

An entitlement can also create a ceiling where the quota had none — a tenant
with no `usage_quotas` row but a plan that allows 100 requests a day is limited
to 100. That is the case that matters commercially, and it is why this returns a
`Quota` rather than editing one in place.

── Fail closed ──────────────────────────────────────────────────────────────

A database failure raises. It does not return the quota unnarrowed.
`load_quota`'s own docstring gives the reason and it applies twice over here:
unmetered is the most permissive possible guess, and a hiccup must not become an
unlimited spend. `QuotaUnavailable` is the existing vocabulary for it.
"""
from __future__ import annotations

import logging
from typing import Optional

from core.identity.db_context import tenant_connection
from core.identity.principal import Principal
from core.models.gateway import Quota

logger = logging.getLogger("ira.billing.entitlements")

#: Entitlement key → the `Quota` field it narrows.
#:
#: Only the two INTEGER ceilings are mapped. A cost entitlement is deliberately
#: absent: `Quota.monthly_cost_limit` is a `Decimal` of currency, and
#: `plan_entitlements.limit_value` is a bare BIGINT with no currency beside it.
#: Mapping one to the other would require inventing a unit — minor units? whole
#: units? which currency? — and inventing a unit for money is how an invoice
#: comes out a hundred times too large. It needs a schema decision, and P11-03
#: is where invoices force that decision to be made explicitly.
ENTITLEMENT_TO_QUOTA_FIELD: dict[str, str] = {
    "requests_per_day": "daily_request_limit",
    "tokens_per_month": "monthly_token_limit",
}


async def effective_limits(principal: Principal) -> dict[str, Optional[int]]:
    """This tenant's effective entitlements: {key: limit or None-for-unlimited}.

    Only keys the tenant actually has an entitlement for appear. A key that is
    absent from the result is one nobody has an opinion about, which is
    different from one entitled to `None` (explicitly unlimited) — and the
    caller must be able to tell those apart, because the first leaves the quota
    alone and the second is a deliberate statement.

    Resolution order: a tenant override beats the plan. Both are read through
    `tenant_connection`, so RLS is what scopes them rather than a predicate this
    function remembers to write.
    """
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            """
            -- The live subscription's plan entitlements, then this tenant's
            -- overrides. DISTINCT ON keeps the override when both exist:
            -- ordering puts source=0 (override) before source=1 (plan).
            SELECT DISTINCT ON (key) key, limit_value
              FROM (
                    SELECT o.key, o.limit_value, 0 AS source
                      FROM tenant_entitlement_overrides o
                     WHERE o.tenant_id = $1
                    UNION ALL
                    SELECT pe.key, pe.limit_value, 1 AS source
                      FROM tenant_subscriptions s
                      JOIN plan_entitlements pe ON pe.plan_id = s.plan_id
                     WHERE s.tenant_id = $1
                       AND s.status IN ('trialing', 'active', 'past_due')
                       -- FINDING CF. The status alone was the whole test, and
                       -- **no event ever sets a subscription to expired** — no
                       -- provider sends "this has finished being paid for". It
                       -- is a fact about the clock, so a subscription whose
                       -- period ended last year sat at status='active' and kept
                       -- granting its plan's entitlements indefinitely.
                       --
                       -- Checked here rather than by a sweep job, because a
                       -- status column that depends on a job having run is
                       -- wrong for as long as the job is down. `past_due` is
                       -- deliberately still inside the window: a failed card is
                       -- a payment problem, not a reason to lock somebody out
                       -- of the period they have already paid for.
                       --
                       -- Same rule as `subscriptions.is_entitled`, which is the
                       -- pure-Python statement of it.
                       AND s.current_period_end > NOW()
                   ) AS merged
             ORDER BY key, source
            """,
            principal.tenant_id,
        )

    # `limit_value` is NULL for "unlimited". The dict keeps that as None, and
    # the KEY BEING PRESENT is what distinguishes it from "no opinion".
    return {r["key"]: r["limit_value"] for r in rows}


def _narrower(current: Optional[int], entitled: Optional[int]) -> Optional[int]:
    """The stricter of two ceilings, where None means unlimited.

    A separate function because the None-handling is the part that is easy to
    get subtly wrong, and a `min()` over values that may be None either raises
    or silently treats None as smallest depending on how it is written.
    """
    if entitled is None:
        return current          # unlimited entitlement never loosens a quota
    if current is None:
        return entitled         # the entitlement creates a ceiling where none existed
    return min(current, entitled)


async def narrow(quota: Optional[Quota], principal: Principal) -> Optional[Quota]:
    """`quota`, tightened by this tenant's entitlements. Never loosened.

    Returns the quota unchanged — including `None` — when the tenant has no
    entitlements at all, which is the state of every tenant in a deployment that
    has sold nothing.
    """
    limits = await effective_limits(principal)
    if not limits:
        return quota

    applicable = {
        ENTITLEMENT_TO_QUOTA_FIELD[k]: v
        for k, v in limits.items()
        if k in ENTITLEMENT_TO_QUOTA_FIELD
    }
    if not applicable:
        # The tenant has entitlements, but none this build knows how to apply.
        # Logged rather than ignored: a plan sold on a limit nothing enforces is
        # a promise the product is not keeping, in either direction.
        logger.info(
            "tenant %s has entitlements this build does not enforce: %s",
            principal.tenant_id, sorted(set(limits) - set(ENTITLEMENT_TO_QUOTA_FIELD)))
        return quota

    base = quota or Quota()
    return Quota(
        monthly_token_limit=_narrower(
            base.monthly_token_limit, applicable.get("monthly_token_limit",
                                                     base.monthly_token_limit)),
        monthly_cost_limit=base.monthly_cost_limit,
        daily_request_limit=_narrower(
            base.daily_request_limit, applicable.get("daily_request_limit",
                                                     base.daily_request_limit)),
        on_exceeded=base.on_exceeded,
    )


__all__ = ["ENTITLEMENT_TO_QUOTA_FIELD", "effective_limits", "narrow"]
