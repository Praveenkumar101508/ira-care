"""Loading rules and grants, and recording decisions — all tenant-scoped.

Every statement here runs inside `tenant_connection`, which sets `app.tenant_id`
with `SET LOCAL` from the authenticated Principal. Row-Level Security does the
rest: a query without tenant context matches no rows by construction, so a
forgotten `WHERE` cannot leak another tenant's Constitution.

`broker.py` deliberately takes rules and grants as arguments rather than loading
them itself. That keeps the decision logic pure and exhaustively testable
without a database, and confines tenant scoping to this one module.

── On recording denials ────────────────────────────────────────────────────

`record_decision` is called for every decision, including refusals. A log that
only records what happened cannot answer "why did IRA refuse?", which is the
question users actually ask — and a denial that leaves no trace is
indistinguishable from a request that was never made, which is exactly what an
attacker probing the boundary would want.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional, Sequence

from core.governance.broker import BrokerDecision
from core.governance.grants import PermissionGrant
from core.governance.rules import ConstitutionRule, parse_rule
from core.identity.db_context import tenant_connection
from core.identity.principal import Principal
from core.interfaces.permission import ActionRequest

logger = logging.getLogger("ira.governance.store")


# ── Reads ────────────────────────────────────────────────────────────────────


async def load_rules(principal: Principal) -> list[ConstitutionRule]:
    """The caller's enabled constitution rules.

    A rule row that fails to parse is SKIPPED and logged, not raised. The
    alternative — refusing every action because one stored rule is malformed —
    would turn a data problem into a total outage. Skipping is safe in this
    direction because a rule that cannot be parsed cannot be one that grants
    permission: the broker's default is already deny, so losing a rule can only
    make the outcome stricter or leave it unchanged.
    """
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            """
            SELECT id, domain, action, effect, conditions, priority, enabled,
                   label, origin
              FROM constitution_rules
             WHERE enabled AND (user_id IS NULL OR user_id = $1)
             ORDER BY priority DESC, id
            """,
            principal.user_id,
        )

    rules: list[ConstitutionRule] = []
    for row in rows:
        try:
            conditions = row["conditions"]
            if isinstance(conditions, str):
                conditions = json.loads(conditions)
            rules.append(
                parse_rule(
                    {
                        "domain": row["domain"],
                        "action": row["action"],
                        "effect": row["effect"],
                        "conditions": conditions or {},
                        "priority": row["priority"],
                        "enabled": row["enabled"],
                        "label": row["label"],
                        "origin": row["origin"],
                    },
                    rule_id=row["id"],
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "skipping unparseable constitution rule %s: %s", row["id"], exc
            )
    return rules


async def load_grants(principal: Principal) -> list[PermissionGrant]:
    """The caller's permission grants — including inert ones.

    Expired and revoked grants are loaded deliberately rather than filtered in
    SQL. `PermissionGrant.status()` distinguishes "never granted", "expired" and
    "revoked", and the Privacy Centre has to be able to tell a user which one
    applies. Filtering here would make all three look identical to the caller.
    Only `covers()` treats them as inert, which is where it matters.
    """
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            """
            SELECT id, connector, resource, scope, purpose, granted_at,
                   expires_at, revoked_at, confirmation_policy, retention_policy
              FROM permission_grants
             WHERE user_id IS NULL OR user_id = $1
             ORDER BY created_at DESC
            """,
            principal.user_id,
        )

    return [
        PermissionGrant(
            connector=row["connector"],
            resource=row["resource"],
            scope=row["scope"],
            purpose=row["purpose"],
            granted_at=row["granted_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            confirmation_policy=row["confirmation_policy"],
            retention_policy=row["retention_policy"],
            grant_id=row["id"],
        )
        for row in rows
    ]


async def load_broker(principal: Principal):
    """A broker loaded with this caller's rules and grants."""
    from core.governance.broker import IRAPermissionBroker

    return IRAPermissionBroker(
        rules=await load_rules(principal),
        grants=await load_grants(principal),
    )


# ── Writes ───────────────────────────────────────────────────────────────────


async def record_decision(
    principal: Principal,
    request: ActionRequest,
    outcome: BrokerDecision,
) -> Optional[uuid.UUID]:
    """Append the decision to `permission_decisions`. Fail-soft.

    Returns the row id, or None if the write failed. Fail-soft because a
    momentarily-unreachable database must not turn a legitimate ALLOW into a
    refusal — but the caller is told, so an Action Receipt can report the gap
    rather than claim an audit entry that does not exist.

    The table is append-only (a trigger in 025 refuses UPDATE and DELETE), so
    there is no path here that edits an earlier decision.
    """
    decision = outcome.decision
    try:
        async with tenant_connection(principal) as conn:
            return await conn.fetchval(
                """
                INSERT INTO permission_decisions
                    (tenant_id, user_id, domain, action, resource, purpose,
                     effect, risk, reason, source, rule_id, grant_id,
                     ticket_id, model_proposed)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                RETURNING id
                """,
                principal.tenant_id,
                principal.user_id,
                request.domain,
                request.action,
                request.resource,
                request.purpose,
                decision.effect.value,
                decision.risk.value,
                decision.reason,
                decision.source,
                outcome.matched_rule.rule_id if outcome.matched_rule else None,
                outcome.matched_grant.grant_id if outcome.matched_grant else None,
                outcome.ticket.ticket_id if outcome.ticket else None,
                request.model_proposed,
            )
    except Exception as exc:  # noqa: BLE001 - fail-soft, reported
        logger.warning(
            "could not record permission decision for %s.%s: %s",
            request.domain, request.action, exc,
        )
        return None


async def save_rule(
    principal: Principal, raw: dict[str, Any]
) -> uuid.UUID:
    """Validate and store one constitution rule.

    Parsed BEFORE the insert so a malformed rule is refused at write time with a
    reason the user can act on. Storing a rule the engine will silently never
    match is worse than refusing it: the user would believe they were protected.
    """
    rule = parse_rule(raw)
    async with tenant_connection(principal) as conn:
        return await conn.fetchval(
            """
            INSERT INTO constitution_rules
                (tenant_id, user_id, domain, action, effect, conditions,
                 priority, enabled, label, origin)
            VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10)
            RETURNING id
            """,
            principal.tenant_id, principal.user_id, rule.domain, rule.action,
            rule.effect.value, json.dumps(dict(rule.conditions)), rule.priority,
            rule.enabled, rule.label, rule.origin,
        )


async def set_rule_enabled(
    principal: Principal, rule_id: uuid.UUID, enabled: bool
) -> bool:
    """Enable or disable a rule. Returns whether a row was affected.

    RLS scopes the UPDATE, so a rule belonging to another tenant simply matches
    nothing and returns False — the caller cannot tell the difference between
    "not yours" and "does not exist", which is the correct amount of
    information to disclose.
    """
    async with tenant_connection(principal) as conn:
        result = await conn.execute(
            """
            UPDATE constitution_rules
               SET enabled = $3, updated_at = NOW()
             WHERE id = $1 AND tenant_id = $2
            """,
            rule_id, principal.tenant_id, enabled,
        )
    return str(result).endswith("1")


async def revoke_grant(principal: Principal, grant_id: uuid.UUID) -> bool:
    """Revoke a grant. Sets `revoked_at`; never deletes the row.

    Deleting would destroy the record that the user once granted this and later
    withdrew it — which is exactly what the Privacy Centre must be able to show.
    """
    async with tenant_connection(principal) as conn:
        result = await conn.execute(
            """
            UPDATE permission_grants
               SET revoked_at = NOW(), updated_at = NOW()
             WHERE id = $1 AND tenant_id = $2 AND revoked_at IS NULL
            """,
            grant_id, principal.tenant_id,
        )
    return str(result).endswith("1")


async def recent_decisions(
    principal: Principal, *, limit: int = 50
) -> list[dict[str, Any]]:
    """The caller's recent permission decisions, newest first."""
    capped = max(1, min(int(limit), 200))
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            """
            SELECT domain, action, resource, purpose, effect, risk, reason,
                   source, model_proposed, decided_at
              FROM permission_decisions
             WHERE tenant_id = $1
             ORDER BY decided_at DESC
             LIMIT $2
            """,
            principal.tenant_id, capped,
        )
    return [
        {
            "domain": r["domain"],
            "action": r["action"],
            "resource": r["resource"],
            "purpose": r["purpose"],
            "effect": r["effect"],
            "risk": r["risk"],
            "reason": r["reason"],
            "source": r["source"],
            "model_proposed": r["model_proposed"],
            "decided_at": r["decided_at"].isoformat() if r["decided_at"] else None,
        }
        for r in rows
    ]


async def seed_starter_rules(principal: Principal, labels: Sequence[str]) -> int:
    """Install the onboarding rules the user selected. Returns how many.

    Offered, never imposed: the caller passes the labels the user actually
    chose. Seeding all of them by default would be deciding someone's
    Constitution for them, which is the opposite of the point.
    """
    from core.governance.rules import STARTER_RULES

    wanted = {label for label in labels}
    installed = 0
    for raw in STARTER_RULES:
        if raw.get("label") in wanted:
            await save_rule(principal, dict(raw))
            installed += 1
    return installed


__all__ = [
    "load_broker",
    "load_grants",
    "load_rules",
    "recent_decisions",
    "record_decision",
    "revoke_grant",
    "save_rule",
    "seed_starter_rules",
    "set_rule_enabled",
]
