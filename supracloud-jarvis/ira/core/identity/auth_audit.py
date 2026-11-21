"""The authentication audit trail (Phase 2 batch 11).

Phase 2 built ten batches of authentication and recorded almost none of it.
`access_audit_log` covers authorisation — what a principal was allowed to reach
— and says nothing about how they came to be that principal. This answers the
questions asked after an incident: when did this account last sign in, from
where, did the second factor ever fail, was a recovery code spent, was the
password reset, did somebody link a provider.

── Recording must never change an authentication outcome ───────────────────

Every function here is best-effort and swallows its own failures. That is the
opposite of the rule the rest of Phase 2 follows, and it is deliberate: a
logging outage that refused logins would convert a bookkeeping problem into a
total outage, and an attacker who could break the log could lock everybody out.
The log is evidence, not a control.

The one thing that is NOT best-effort is what gets written. See below.

── What is never written ───────────────────────────────────────────────────

No password, no token, no recovery code, no TOTP code, and no hash of any of
them. An audit trail carrying credential material turns a log reader into an
authenticator, and logs are copied, shipped and retained far more widely than
credential tables.

The attempted address is not written in the clear either. A failed-sign-in log
full of addresses is a personal-data store nobody remembers to erase AND a list
of every address somebody probed. `subject_hash` is an HMAC under the
application secret: enough to answer "was this the same account twice", useless
to whoever steals the log.

`detail` is bounded at 200 characters by a CHECK, and is for a fixed phrase
rather than a formatted message. An unbounded column on an audit table is where
credential material eventually lands, because somebody logs "the request" to
debug something and forgets.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Final

logger = logging.getLogger("ira.identity.auth_audit")

#: The vocabulary `061` enforces with a CHECK. Repeated here so a typo is an
#: ImportError at the call site rather than a constraint violation at runtime —
#: and, because the log is best-effort, a constraint violation would be
#: swallowed and the event simply lost.
SIGN_IN_SUCCEEDED: Final = "sign_in_succeeded"
SIGN_IN_REFUSED: Final = "sign_in_refused"
SIGN_IN_UNAVAILABLE: Final = "sign_in_unavailable"
MFA_REQUIRED: Final = "mfa_required"
MFA_SUCCEEDED: Final = "mfa_succeeded"
MFA_FAILED: Final = "mfa_failed"
RECOVERY_CODE_USED: Final = "recovery_code_used"
RECOVERY_CODES_ISSUED: Final = "recovery_codes_issued"
PASSWORD_RESET_REQUESTED: Final = "password_reset_requested"
PASSWORD_RESET_COMPLETED: Final = "password_reset_completed"
SESSION_REVOKED: Final = "session_revoked"
SESSIONS_REVOKED_ALL: Final = "sessions_revoked_all"
PROVIDER_LINKED: Final = "provider_linked"
PROVIDER_UNLINKED: Final = "provider_unlinked"
PROVIDER_SIGN_IN: Final = "provider_sign_in"
ACCOUNT_LINK_REQUIRED: Final = "account_link_required"

EVENTS: Final = frozenset({
    SIGN_IN_SUCCEEDED, SIGN_IN_REFUSED, SIGN_IN_UNAVAILABLE,
    MFA_REQUIRED, MFA_SUCCEEDED, MFA_FAILED,
    RECOVERY_CODE_USED, RECOVERY_CODES_ISSUED,
    PASSWORD_RESET_REQUESTED, PASSWORD_RESET_COMPLETED,
    SESSION_REVOKED, SESSIONS_REVOKED_ALL,
    PROVIDER_LINKED, PROVIDER_UNLINKED, PROVIDER_SIGN_IN,
    ACCOUNT_LINK_REQUIRED,
})


def _keyed(value: str | None) -> str | None:
    """HMAC-SHA256 under the application secret, or None.

    Keyed rather than a bare digest for the reason `sessions.hash_ip` records:
    the whole IPv4 space, and any list of plausible addresses, is a table
    somebody precomputes once. An unkeyed hash of an identifier IS the
    identifier to anybody willing to build that table.
    """
    if not value:
        return None
    from config import get_settings

    secret = getattr(get_settings(), "ira_secret_key", None) or ""
    if not secret:
        # No key, no keyed hash. Writing an unkeyed one "for now" is how a
        # reversible identifier ends up in an audit table nobody revisits.
        return None
    return hmac.new(str(secret).encode("utf-8"),
                    value.strip().lower().encode("utf-8"),
                    hashlib.sha256).hexdigest()


async def record(
    event: str,
    *,
    user_id=None,
    tenant_id=None,
    subject: str | None = None,
    ip_address: str | None = None,
    user_agent: str = "",
    detail: str = "",
) -> None:
    """Write one authentication event. Never raises.

    `subject` is an email address and is HMAC'd before it reaches the database;
    `ip_address` likewise. Neither is stored in the clear, and `061` has a CHECK
    making the clear form unrepresentable.

    Runs on an UNBOUND connection deliberately. Most authentication happens
    before a tenant is selected, and the events that matter most — a refused
    sign-in for an address that matched no account — have no user and no tenant
    at all. `061`'s INSERT policy therefore permits an unattributed write, and
    its SELECT policy is what keeps those rows from being readable.
    """
    if event not in EVENTS:
        # Not raised: this is best-effort, and a bad event name is a programming
        # error that must not take an authentication path down with it. Logged
        # loudly instead, because a silently dropped audit event is exactly what
        # this module exists to prevent.
        logger.error("refusing to record an unknown auth event %r", event)
        return

    try:
        from utils.db import acquire

        async with acquire() as conn:
            await conn.execute(
                """
                INSERT INTO auth_events
                       (user_id, tenant_id, event, subject_hash, ip_hash,
                        user_agent, detail)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                user_id, tenant_id, event, _keyed(subject), _keyed(ip_address),
                (user_agent or "")[:400], (detail or "")[:200],
            )
    except Exception:  # noqa: BLE001 — see the module docstring
        # `exc_info` is safe: nothing in this function's locals is a credential,
        # and the two identifiers that could be personal are already HMAC'd.
        logger.warning("could not record an auth event (%s)", event, exc_info=True)


async def history(principal, *, limit: int = 100) -> list[dict]:
    """The caller's OWN authentication history, newest first.

    Not the tenant's. "When did my colleague last sign in, and from where" is
    the movement log `057` refused to hand out, and it is no more theirs to read
    because it lives in an audit table. `061`'s SELECT policy enforces this; the
    query does not need a predicate and deliberately does not carry one, so the
    boundary has exactly one definition.

    `ip_hash` and `subject_hash` are NOT returned. They are keyed, so they are
    not addresses — but they are stable identifiers, and a list of them is a
    list of "the same place, again", which is not something an API needs to
    hand out.
    """
    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            """
            SELECT event, user_agent, detail, created_at
              FROM auth_events
             ORDER BY created_at DESC
             LIMIT $1
            """,
            max(1, min(int(limit or 100), 500)),
        )

    return [
        {
            "event": r["event"],
            "user_agent": r["user_agent"] or "",
            "detail": r["detail"] or "",
            "at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


async def redact_for(principal) -> int:
    """Clear attribution on this account's events. Returns the number cleared.

    The row STAYS. `061`'s trigger permits exactly this mutation and refuses
    every other, including DELETE — the shape `038` gave `biometric_audit` and
    ADR-0004 gave the trust ledger. What happened and when is the evidence; who
    it was about is the personal data.

    Called by `erase_audit`, and NOT best-effort there: an authentication log
    still naming an erased account is the residue the erasure report exists to
    surface.

    ── Why this binds a GUC nothing else binds ─────────────────────────────

    PostgreSQL applies SELECT policies to the NEW row of an UPDATE, not only to
    the row it reads. This statement sets `user_id` to NULL and `061`'s SELECT
    policy keys on `user_id` — so without `app.audit_redaction` bound, the
    redaction updates every row out of its own visibility and PostgreSQL
    refuses the whole statement with SQLSTATE 42501.

    That is the same rule Batch 9 met from the other side, where a DELETE-only
    widening silently matched nothing because SELECT policies gate the rows a
    DELETE reads. The difference in symptom is worth noting: that one was
    silent, and this one is loud. `061` carries the policy and the full
    reasoning for what the widening costs.
    """
    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal) as conn:
        # Set to the user id rather than a flag, because the policy requires it
        # to MATCH `app.user_id`: a caller cannot declare "this is a redaction"
        # without also naming the account it claims to be erasing.
        await conn.execute(
            "SELECT set_config('app.audit_redaction', $1, true)",
            str(principal.user_id))
        result = await conn.execute(
            """
            UPDATE auth_events
               SET user_id = NULL, subject_hash = NULL, ip_hash = NULL,
                   user_agent = ''
             WHERE user_id = $1
            """,
            principal.user_id,
        )
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except ValueError:  # pragma: no cover - driver-dependent
        return 0


__all__ = [
    "ACCOUNT_LINK_REQUIRED",
    "EVENTS",
    "MFA_FAILED",
    "MFA_REQUIRED",
    "MFA_SUCCEEDED",
    "PASSWORD_RESET_COMPLETED",
    "PASSWORD_RESET_REQUESTED",
    "PROVIDER_LINKED",
    "PROVIDER_SIGN_IN",
    "PROVIDER_UNLINKED",
    "RECOVERY_CODES_ISSUED",
    "RECOVERY_CODE_USED",
    "SESSIONS_REVOKED_ALL",
    "SESSION_REVOKED",
    "SIGN_IN_REFUSED",
    "SIGN_IN_SUCCEEDED",
    "SIGN_IN_UNAVAILABLE",
    "history",
    "record",
    "redact_for",
]
