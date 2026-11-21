"""Tenant-scoped persistence for the model gateway, quotas and admin access.

Every statement runs inside `tenant_connection`, so Row-Level Security binds
`app.tenant_id` to the authenticated Principal — the same arrangement as
`core/privacy/store.py`, `core/realtime/store.py` and `core/connectors/store.py`.

Two functions here are deliberately NOT fail-soft, against the pattern of the
rest of the module:

**`log_admin_access`.** An administrator read that goes unlogged costs the only
evidence that it was authorised. `read_for_admin` turns a logging failure into a
refused read, and this function raises so it can.

**`load_quota`.** If the ceiling cannot be read, the honest answer is not
"unmetered" — that is the most permissive possible guess, and it would let a
tenant spend without limit whenever the database hiccuped. It raises, and the
gateway's caller decides.

`record_usage` IS fail-soft, because a metering row that goes missing costs a
data point, not a guarantee.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from core.identity.db_context import tenant_connection
from core.identity.principal import Principal
from core.models.admin_access import AccessGrant
from core.models.gateway import Quota, UsageSoFar

logger = logging.getLogger("ira.models.store")


class QuotaUnavailable(RuntimeError):
    """The ceiling could not be read. NOT treated as unmetered — see the header."""


# ── Providers ────────────────────────────────────────────────────────────────


async def provider_catalogue(
    principal: Principal, *, edition: str = "cloud"
) -> list[dict[str, Any]]:
    """Every provider this edition enables, with its disclosure.

    `retains_prompts` and `trains_on_data` come back as `None` when the provider
    has not stated them. That is passed through untouched: converting NULL to
    False here would be the exact misrepresentation `029_commercial.sql`'s
    comment warns about.
    """
    column = "enabled_in_cloud" if edition == "cloud" else "enabled_in_private"
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            f"SELECT provider_id, display_name, locality, retains_prompts, "
            f"trains_on_data, disclosure_source, disclosure_checked_at, region, "
            f"tiers, cost_per_1k_prompt_tokens, cost_per_1k_completion_tokens "
            f"FROM model_providers WHERE {column} IS TRUE ORDER BY locality, provider_id"
        )
    return [
        {
            "provider_id": r["provider_id"],
            "display_name": r["display_name"],
            "locality": r["locality"],
            # None stays None. The surface must render "not known".
            "retains_prompts": r["retains_prompts"],
            "trains_on_data": r["trains_on_data"],
            "disclosure_source": r["disclosure_source"],
            "disclosure_checked_at": (
                r["disclosure_checked_at"].isoformat()
                if r["disclosure_checked_at"] else None
            ),
            "region": r["region"],
            "tiers": list(r["tiers"] or ()),
            "cost_per_1k_prompt_tokens": str(r["cost_per_1k_prompt_tokens"]),
            "cost_per_1k_completion_tokens": str(r["cost_per_1k_completion_tokens"]),
        }
        for r in rows
    ]


# ── Quotas and usage ─────────────────────────────────────────────────────────


async def load_quota(principal: Principal) -> tuple[Optional[Quota], UsageSoFar]:
    """The tenant's ceilings and what has been spent. Raises on failure.

    Raising rather than defaulting to unmetered: unmetered is the most permissive
    possible guess, and a database hiccup should not become an unlimited spend.
    """
    try:
        async with tenant_connection(principal) as conn:
            quota_row = await conn.fetchrow(
                "SELECT monthly_token_limit, monthly_cost_limit, "
                "daily_request_limit, on_exceeded, period_started_at "
                "FROM usage_quotas WHERE tenant_id = $1",
                principal.tenant_id,
            )
            usage_row = await conn.fetchrow(
                """
                SELECT
                  COALESCE(SUM(prompt_tokens + completion_tokens)
                           FILTER (WHERE at >= date_trunc('month', NOW())), 0) AS tokens,
                  COALESCE(SUM(cost)
                           FILTER (WHERE at >= date_trunc('month', NOW())), 0) AS cost,
                  COUNT(*) FILTER (WHERE at >= date_trunc('day', NOW())) AS requests
                FROM model_usage
                WHERE tenant_id = $1 AND outcome = 'ok'
                """,
                principal.tenant_id,
            )
    except Exception as exc:  # noqa: BLE001 - deliberately NOT swallowed
        raise QuotaUnavailable(
            f"could not read the usage quota: {exc}. Refusing to assume "
            f"unmetered — that is the most permissive possible guess."
        ) from exc

    quota = None
    if quota_row is not None:
        quota = Quota(
            monthly_token_limit=quota_row["monthly_token_limit"],
            monthly_cost_limit=(
                Decimal(str(quota_row["monthly_cost_limit"]))
                if quota_row["monthly_cost_limit"] is not None else None
            ),
            daily_request_limit=quota_row["daily_request_limit"],
            on_exceeded=quota_row["on_exceeded"],
        )
    usage = UsageSoFar(
        monthly_tokens=int(usage_row["tokens"] or 0),
        monthly_cost=Decimal(str(usage_row["cost"] or 0)),
        daily_requests=int(usage_row["requests"] or 0),
    )

    # ── The tenant's plan narrows the ceiling (P11-02) ───────────────────────
    #
    # Here, and not in the gateway, because this function is what ANSWERS
    # "what are this tenant's ceilings" — the gateway's job is the ORDER in
    # which admission happens, and that order is already correct. Putting the
    # entitlement lookup here means it runs on the one path every inference
    # request takes (`_admit` step 2) without the gateway growing a second
    # opinion about limits.
    #
    # Entitlements can only LOWER a ceiling, never raise one, and a tenant with
    # no subscription has none — so a deployment that has sold nothing gets
    # exactly the quota it got before this line existed.
    #
    # Wrapped so it RAISES IN THE ESTABLISHED VOCABULARY, and never swallowed.
    #
    # An entitlement lookup that failed and was caught would silently return the
    # UN-NARROWED quota — the most permissive answer available, and exactly the
    # "swallowed failure returns a plausible benign result" class. So the except
    # re-raises rather than falling back.
    #
    # `QuotaUnavailable` specifically, and not the raw error: this call sits
    # AFTER the try/except above, so a bare exception would reach callers as
    # something they have no handling for, while every caller already knows what
    # an unreadable quota means. The first version of this comment claimed the
    # handler above caught it, which was simply false — the call is outside it.
    from core.billing.entitlements import narrow

    try:
        quota = await narrow(quota, principal)
    except Exception as exc:  # noqa: BLE001 - re-raised, never swallowed
        raise QuotaUnavailable(
            f"could not read this tenant's entitlements: {exc}. Refusing to "
            f"fall back to the un-narrowed quota — that would restore the "
            f"wider ceiling the plan exists to reduce."
        ) from exc

    return quota, usage


async def reserve_daily_request(principal: Principal) -> bool:
    """Take one slot from today's request ceiling, atomically. Fail closed.

    ── Why this exists ──────────────────────────────────────────────────────

    `load_quota` + `check_quota` is check-then-act. Under concurrency every
    request reads the same under-limit snapshot before any of them writes, so
    all of them admit themselves: `tests/readiness/test_behaviour_under_load.py`
    put **24 of 24** concurrent requests through a ceiling of 5.

    Counting rows cannot fix that, however carefully the count is written — a
    count describes the past. This is a single conditional UPDATE, so PostgreSQL
    holds a row lock for its duration and concurrent callers serialise behind
    it. The statement either reserves a slot or matches no row, and there is no
    window between deciding and spending because they are the same statement.

    The daily rollover happens inside the same UPDATE rather than in a scheduled
    job, so no request can slip through while a reset is in progress.

    ── Fail closed ──────────────────────────────────────────────────────────

    Returns False on any error, including the columns being absent because
    migration 032 has not been applied. Refusing traffic is the safe failure for
    a ceiling; admitting it unmetered is not. This matches `load_quota`, which
    raises rather than assuming unmetered for the same reason.

    Returns True when there is no quota row (that tenant is unmetered) or no
    `daily_request_limit` is set — absence of a ceiling is not a ceiling of zero.
    """
    try:
        async with tenant_connection(principal) as conn:
            row = await conn.fetchrow(
                """
                UPDATE usage_quotas
                SET daily_requests_used =
                        CASE WHEN daily_period_started_at < date_trunc('day', now())
                             THEN 1 ELSE daily_requests_used + 1 END,
                    daily_period_started_at =
                        CASE WHEN daily_period_started_at < date_trunc('day', now())
                             THEN date_trunc('day', now()) ELSE daily_period_started_at END,
                    updated_at = now()
                WHERE tenant_id = $1
                  AND (
                        daily_request_limit IS NULL
                     OR daily_period_started_at < date_trunc('day', now())
                     OR daily_requests_used < daily_request_limit
                  )
                RETURNING daily_requests_used
                """,
                principal.tenant_id,
            )
            if row is not None:
                return True

            # No row matched. Either the tenant is over its ceiling, or it has
            # no quota row at all — and those mean opposite things, so ask.
            exists = await conn.fetchval(
                "SELECT 1 FROM usage_quotas WHERE tenant_id = $1",
                principal.tenant_id,
            )
            if exists is None:
                return True  # unmetered tenant
            return False  # genuinely exhausted
    except Exception as exc:  # noqa: BLE001 - deliberately NOT swallowed into True
        logger.warning("could not reserve a request slot: %s", exc)
        return False


async def release_daily_request(principal: Principal) -> None:
    """Give a reserved slot back. Fail-soft.

    Used when a request is reserved and then refused for a reason that is not
    the tenant's doing — no provider reachable, for instance. Without this, an
    outage in a provider would silently eat a tenant's daily allowance.

    Fail-soft because the failure mode is a tenant who is one request short for
    the day, which is worth strictly less than turning a completed refusal into
    an error. Never drops below zero.
    """
    try:
        async with tenant_connection(principal) as conn:
            await conn.execute(
                """
                UPDATE usage_quotas
                SET daily_requests_used = GREATEST(daily_requests_used - 1, 0),
                    updated_at = now()
                WHERE tenant_id = $1
                  AND daily_period_started_at >= date_trunc('day', now())
                """,
                principal.tenant_id,
            )
    except Exception as exc:  # noqa: BLE001 - fail-soft, logged
        logger.warning("could not release a reserved request slot: %s", exc)


async def record_usage(principal: Principal, row: dict[str, Any]) -> Optional[uuid.UUID]:
    """Append a metering row. Fail-soft — a missing data point, not a guarantee."""
    try:
        async with tenant_connection(principal) as conn:
            return await conn.fetchval(
                """
                INSERT INTO model_usage
                    (tenant_id, user_id, provider_id, locality, model, tier,
                     sensitivity, privacy_mode, outcome, prompt_tokens,
                     completion_tokens, cost, latency_ms)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                RETURNING id
                """,
                principal.tenant_id, principal.user_id,
                row.get("provider_id", "ollama-local"),
                row.get("locality", "local"), (row.get("model") or "")[:128],
                row.get("tier", "fast"), row.get("sensitivity", "normal"),
                row.get("privacy_mode", "local_only"), row.get("outcome", "ok"),
                int(row.get("prompt_tokens", 0)),
                int(row.get("completion_tokens", 0)),
                Decimal(str(row.get("cost", 0))), row.get("latency_ms"),
            )
    except Exception as exc:  # noqa: BLE001 - fail-soft, logged
        logger.warning("could not record model usage: %s", exc)
        return None


async def usage_summary(principal: Principal, *, limit: int = 50) -> dict[str, Any]:
    """Where this tenant's requests went, and what they cost.

    Grouped by provider AND locality so the answer to "did anything leave my
    machine?" is a number rather than an inference from provider names.
    """
    capped = max(1, min(int(limit), 200))
    async with tenant_connection(principal) as conn:
        by_provider = await conn.fetch(
            "SELECT provider_id, locality, outcome, COUNT(*) AS calls, "
            "SUM(prompt_tokens + completion_tokens) AS tokens, SUM(cost) AS cost "
            "FROM model_usage WHERE tenant_id = $1 "
            "AND at >= date_trunc('month', NOW()) "
            "GROUP BY provider_id, locality, outcome ORDER BY calls DESC",
            principal.tenant_id,
        )
        recent = await conn.fetch(
            "SELECT provider_id, locality, tier, sensitivity, privacy_mode, "
            "outcome, prompt_tokens, completion_tokens, cost, latency_ms, at "
            "FROM model_usage WHERE tenant_id = $1 ORDER BY at DESC LIMIT $2",
            principal.tenant_id, capped,
        )
    left_machine = sum(
        int(r["calls"]) for r in by_provider
        if r["locality"] != "local" and r["outcome"] == "ok"
    )
    return {
        "by_provider": [
            {
                "provider_id": r["provider_id"], "locality": r["locality"],
                "outcome": r["outcome"], "calls": int(r["calls"]),
                "tokens": int(r["tokens"] or 0), "cost": str(r["cost"] or 0),
            }
            for r in by_provider
        ],
        "recent": [
            {
                "provider_id": r["provider_id"], "locality": r["locality"],
                "tier": r["tier"], "sensitivity": r["sensitivity"],
                "privacy_mode": r["privacy_mode"], "outcome": r["outcome"],
                "prompt_tokens": r["prompt_tokens"],
                "completion_tokens": r["completion_tokens"],
                "cost": str(r["cost"]), "latency_ms": r["latency_ms"],
                "at": r["at"].isoformat() if r["at"] else None,
            }
            for r in recent
        ],
        # Stated as a count so the answer is a fact, not an inference from
        # provider names a user may not recognise.
        "successful_calls_that_left_this_machine": left_machine,
    }


# ── Break-glass access ───────────────────────────────────────────────────────


def _row_to_grant(row: Any) -> AccessGrant:
    return AccessGrant(
        grant_id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        subject_user_id=str(row["subject_user_id"]),
        admin_identity=row["admin_identity"],
        reason=row["reason"],
        scope=row["scope"],
        user_authorised_at=row["user_authorised_at"],
        granted_at=row["granted_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
    )


_GRANT_COLUMNS = (
    "id, tenant_id, subject_user_id, admin_identity, reason, scope, "
    "user_authorised_at, granted_at, expires_at, revoked_at"
)


async def create_grant(principal: Principal, row: dict[str, Any]) -> uuid.UUID:
    """Insert a grant the user authorised. Raises on failure.

    Not fail-soft: a grant that was not stored is an administrator about to read
    with no authorisation, and reporting success would be the worst possible
    outcome.
    """
    async with tenant_connection(principal) as conn:
        return await conn.fetchval(
            """
            INSERT INTO admin_access_grants
                (tenant_id, subject_user_id, admin_identity, reason, scope,
                 user_authorised_at, granted_at, expires_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            RETURNING id
            """,
            row["tenant_id"], row["subject_user_id"], row["admin_identity"],
            row["reason"], row["scope"], row["user_authorised_at"],
            row["granted_at"], row["expires_at"],
        )


async def live_grant(
    principal: Principal, *, subject_user_id: str, admin_identity: str, scope: str
) -> Optional[AccessGrant]:
    """The grant covering this read right now, or None.

    The window is checked in SQL as well as in `AccessGrant.is_live`, so a clock
    difference between the application and the database cannot widen it.
    """
    async with tenant_connection(principal) as conn:
        row = await conn.fetchrow(
            f"SELECT {_GRANT_COLUMNS} FROM admin_access_grants "
            f"WHERE tenant_id = $1 AND subject_user_id = $2 "
            f"AND admin_identity = $3 AND scope = $4 "
            f"AND revoked_at IS NULL AND NOW() BETWEEN granted_at AND expires_at "
            f"ORDER BY granted_at DESC LIMIT 1",
            principal.tenant_id, uuid.UUID(subject_user_id), admin_identity, scope,
        )
    return _row_to_grant(row) if row else None


async def grants_for_user(principal: Principal) -> list[dict[str, Any]]:
    """Every grant over this user's data, live or not.

    Expired and revoked grants are included on purpose: "who has looked at my
    data?" is not answered by a list of who can look right now.
    """
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            f"SELECT {_GRANT_COLUMNS} FROM admin_access_grants "
            f"WHERE tenant_id = $1 AND subject_user_id = $2 "
            f"ORDER BY granted_at DESC",
            principal.tenant_id, principal.user_id,
        )
    return [_row_to_grant(r).public_dict() for r in rows]


async def revoke_grant(principal: Principal, grant_id: uuid.UUID) -> bool:
    """Revoke. The trigger refuses anything that is not purely a revocation."""
    async with tenant_connection(principal) as conn:
        result = await conn.execute(
            "UPDATE admin_access_grants SET revoked_at = NOW() "
            "WHERE id = $1 AND tenant_id = $2 AND revoked_at IS NULL",
            grant_id, principal.tenant_id,
        )
    return str(result).endswith("1")


async def log_admin_access(
    principal: Principal, grant: AccessGrant, what: str, count: int
) -> None:
    """Record an administrator read. RAISES on failure — see the module header.

    The trigger in `029_commercial.sql` refuses a row whose grant is missing,
    revoked, out of window, for another admin, or in another tenant. So this
    either records an authorised access or fails, and `read_for_admin` turns the
    failure into a refused read.
    """
    async with tenant_connection(principal) as conn:
        await conn.execute(
            "INSERT INTO admin_access_log "
            "(tenant_id, grant_id, admin_identity, what_was_read, record_count) "
            "VALUES ($1,$2,$3,$4,$5)",
            principal.tenant_id, uuid.UUID(grant.grant_id), grant.admin_identity,
            what[:200], int(count),
        )


async def access_log_for_user(
    principal: Principal, *, limit: int = 100
) -> list[dict[str, Any]]:
    """Every administrator read of this user's data. The user's own surface."""
    capped = max(1, min(int(limit), 500))
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT l.admin_identity, l.what_was_read, l.record_count, l.at, "
            "g.reason, g.scope, g.user_authorised_at "
            "FROM admin_access_log l JOIN admin_access_grants g ON g.id = l.grant_id "
            "WHERE l.tenant_id = $1 AND g.subject_user_id = $2 "
            "ORDER BY l.at DESC LIMIT $3",
            principal.tenant_id, principal.user_id, capped,
        )
    return [
        {
            "admin": r["admin_identity"],
            "what_was_read": r["what_was_read"],
            "record_count": r["record_count"],
            "at": r["at"].isoformat() if r["at"] else None,
            "reason": r["reason"],
            "scope": r["scope"],
            "you_authorised_at": (
                r["user_authorised_at"].isoformat()
                if r["user_authorised_at"] else None
            ),
        }
        for r in rows
    ]


__all__ = [
    "QuotaUnavailable",
    "access_log_for_user",
    "create_grant",
    "grants_for_user",
    "live_grant",
    "load_quota",
    "release_daily_request",
    "reserve_daily_request",
    "log_admin_access",
    "provider_catalogue",
    "record_usage",
    "revoke_grant",
    "usage_summary",
]
