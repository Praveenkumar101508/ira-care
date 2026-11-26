"""The ExecutionTicket — the object that makes "no execution without a decision"
a structural property rather than a convention.

Phase 4's acceptance criterion is unusually strong:

    Tests must prove that direct tool execution without a permission decision is
    impossible.

"Impossible" cannot be met by a code-review rule or a docstring asking callers
to check first. Something has to make the unchecked path not exist. So execution
requires a value that only a permission decision can produce:

    broker.decide(request)  ──mints──▶  ExecutionTicket  ──consumed by──▶  gateway.execute()

No ticket, no execution. The gateway takes a ticket as a required argument, and
verifies it against the request it is about to perform.

── Why the ticket is authenticated, not just a dataclass ────────────────────

A plain dataclass could be constructed by any caller — `ExecutionTicket(...)`
would be a complete bypass, and one that looks entirely innocent in review. So
every ticket carries an HMAC over its own contents, keyed by a secret generated
per process. `verify()` recomputes it.

The key never leaves this module, is never persisted, and is regenerated on
restart (which correctly invalidates every outstanding ticket — a ticket that
survived a restart would authorise an action decided by a configuration that is
no longer loaded).

This is not defending against an attacker with code execution inside the
process: at that point they can call the connector directly and no in-process
scheme helps. It defends against the realistic failure — new code, written six
months from now by someone who has not read this file, taking a shortcut. That
shortcut has to be a deliberate forgery rather than an oversight, and the AST
test in tests/architecture/ catches the deliberate version.

── One-shot ────────────────────────────────────────────────────────────────

A ticket is consumed on use. Replaying an approval to perform an action twice —
"send the email" approved once, sent five times — is a real and unglamorous
failure mode, and a consumed-set closes it. Consumption is recorded centrally so
two concurrent executions cannot both win.

── Bound to the request ────────────────────────────────────────────────────

The ticket fingerprints the exact request it was minted for: principal, domain,
action, resource, purpose. A ticket issued for "read calendar" cannot execute
"delete calendar", so a decision obtained for a benign action cannot be
redirected to a harmful one.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Final, Optional

from core.interfaces.permission import ActionRequest, Effect, RiskLevel

logger = logging.getLogger("ira.governance.ticket")

#: Per-process signing key. Generated at import, never persisted, never logged.
#: A restart invalidates outstanding tickets, which is the correct behaviour:
#: they were minted under a configuration this process has not loaded.
_SIGNING_KEY: Final[bytes] = secrets.token_bytes(32)

#: How long a ticket stays usable. Short: a ticket is meant to be redeemed
#: immediately by the caller that requested it, not held.
DEFAULT_TTL_SECONDS: Final[float] = 300.0

#: Tickets already redeemed, so one cannot be replayed: ticket id -> its expiry.
#:
#: Bounded. An id only needs remembering until its ticket expires, because
#: `verify()` refuses an expired ticket anyway. Once the map reaches
#: `_next_prune_at` entries (at least `_PRUNE_AT`, and twice what was left
#: after the last pruning, so a burst of live tickets is not rescanned on every
#: call), the ids of tickets that expired by the wall clock are
#: dropped, and `_pruned_through` keeps the latest expiry dropped: a ticket
#: whose expiry is at or before it is refused as possibly used, whatever `now`
#: the caller passes, so pruning never reopens a replay. Without this the set
#: grew by one id per governed action for the life of the process.
_consumed: dict[str, float] = {}
_consumed_lock = threading.Lock()
_PRUNE_AT: Final[int] = 1024
_pruned_through: float = float("-inf")
_next_prune_at: int = _PRUNE_AT


def _prune_expired_locked(wall_now: float) -> None:
    """Drop the ids of tickets expired by the wall clock. Hold `_consumed_lock`."""
    global _pruned_through, _next_prune_at
    expired = [k for k, exp in _consumed.items() if exp <= wall_now]
    for k in expired:
        _pruned_through = max(_pruned_through, _consumed.pop(k))
    _next_prune_at = max(_PRUNE_AT, 2 * len(_consumed))


class TicketError(RuntimeError):
    """Base class for every refusal in this module. Always fail closed."""


class InvalidTicket(TicketError):
    """The ticket is forged, tampered with, expired, or already used."""


class TicketMismatch(TicketError):
    """The ticket is valid but was issued for a different action."""


def _fingerprint(request: ActionRequest) -> str:
    """A stable digest of the request a ticket is bound to.

    Includes the tenant, so a ticket minted in one tenant cannot be redeemed in
    another even if every other field matches — which is the shape a
    cross-tenant confusion bug would take.
    """
    parts = "\x1f".join(
        str(p) for p in (
            request.principal.tenant_id,
            request.principal.user_id,
            request.domain,
            request.action,
            request.resource,
            " ".join(str(request.purpose).lower().split()),
        )
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutionTicket:
    """Proof that a permission decision permitted a specific action.

    Do not construct this directly — an unsigned ticket fails `verify()`. It is
    minted by :func:`issue`, which is called by the Permission Broker and
    nowhere else.
    """

    ticket_id: uuid.UUID
    fingerprint: str
    effect: Effect
    risk: RiskLevel
    issued_at: float
    expires_at: float
    signature: str
    #: The decision's reason, carried so the Action Receipt can state it without
    #: a second lookup.
    reason: str = ""
    source: str = ""
    #: Set when the decision required confirmation and it was obtained.
    approval_request_id: Optional[str] = None
    metadata: dict = field(default_factory=dict, compare=False)

    # ── Verification ────────────────────────────────────────────────────────

    def _expected_signature(self) -> str:
        payload = "\x1f".join(
            str(p) for p in (
                self.ticket_id, self.fingerprint, self.effect.value,
                self.risk.value, f"{self.issued_at:.6f}", f"{self.expires_at:.6f}",
                self.approval_request_id or "",
            )
        )
        return hmac.new(_SIGNING_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def is_signature_valid(self) -> bool:
        # compare_digest: a plain == leaks timing. Cheap to do correctly.
        return hmac.compare_digest(self.signature, self._expected_signature())

    def is_expired(self, *, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def verify(self, request: ActionRequest, *, now: Optional[float] = None) -> None:
        """Raise unless this ticket authorises exactly `request`, right now.

        Order matters: signature first, so a forged ticket is reported as forged
        rather than as expired or mismatched — the diagnostics of a real attack
        should not be muddied by whichever check happened to fire first.
        """
        if not self.is_signature_valid():
            raise InvalidTicket(
                "Execution ticket signature is invalid. It was not issued by the "
                "Permission Broker in this process."
            )
        if self.is_expired(now=now):
            raise InvalidTicket(
                f"Execution ticket {self.ticket_id} has expired. A new permission "
                f"decision is required."
            )
        if not hmac.compare_digest(self.fingerprint, _fingerprint(request)):
            raise TicketMismatch(
                "Execution ticket was issued for a different action. A decision "
                "obtained for one action cannot be redirected to another."
            )
        if self.effect is Effect.REQUIRE_CONFIRMATION:
            raise InvalidTicket(
                "Execution ticket carries REQUIRE_CONFIRMATION — approval was "
                "never obtained. 'Ask the user' is not 'go ahead'."
            )

    # ── Consumption ─────────────────────────────────────────────────────────

    def consume(self, request: ActionRequest, *, now: Optional[float] = None) -> None:
        """Verify and mark used. Raises if already redeemed.

        Verification happens BEFORE consumption so a forged ticket cannot burn a
        legitimate ticket's id, and the whole thing is under one lock so two
        concurrent executions cannot both pass.
        """
        self.verify(request, now=now)
        key = str(self.ticket_id)
        with _consumed_lock:
            if key in _consumed or self.expires_at <= _pruned_through:
                raise InvalidTicket(
                    f"Execution ticket {self.ticket_id} has already been used. "
                    f"An approval authorises an action once, not repeatedly."
                )
            if len(_consumed) >= _next_prune_at:
                _prune_expired_locked(time.time())
            _consumed[key] = self.expires_at


def issue(
    request: ActionRequest,
    *,
    effect: Effect,
    risk: RiskLevel,
    reason: str = "",
    source: str = "",
    approval_request_id: Optional[str] = None,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    now: Optional[float] = None,
) -> ExecutionTicket:
    """Mint a ticket. Called by the Permission Broker; not for general use.

    Refuses to mint for a non-permissive effect. That refusal is what stops the
    most tempting shortcut of all — minting a ticket "so the caller can decide
    later" — from existing.
    """
    from core.governance.effects import is_permissive

    if not is_permissive(effect):
        raise TicketError(
            f"Refusing to issue an execution ticket for effect {effect.value!r}. "
            f"Only a decision that permits execution without further approval "
            f"may mint a ticket."
        )

    issued = now if now is not None else time.time()
    ticket_id = uuid.uuid4()
    fingerprint = _fingerprint(request)
    expires = issued + max(1.0, float(ttl_seconds))

    payload = "\x1f".join(
        str(p) for p in (
            ticket_id, fingerprint, effect.value, risk.value,
            f"{issued:.6f}", f"{expires:.6f}", approval_request_id or "",
        )
    )
    signature = hmac.new(_SIGNING_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    return ExecutionTicket(
        ticket_id=ticket_id,
        fingerprint=fingerprint,
        effect=effect,
        risk=risk,
        issued_at=issued,
        expires_at=expires,
        signature=signature,
        reason=reason,
        source=source,
        approval_request_id=approval_request_id,
    )


def reset_consumed_for_tests() -> None:
    """Clear the consumed-ticket set.

    Test support only. Named so that its presence in non-test code is obvious in
    review, rather than hiding behind something that sounds like maintenance.
    """
    global _pruned_through, _next_prune_at
    with _consumed_lock:
        _consumed.clear()
        _pruned_through = float("-inf")
        _next_prune_at = _PRUNE_AT


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "ExecutionTicket",
    "InvalidTicket",
    "TicketError",
    "TicketMismatch",
    "issue",
    "reset_consumed_for_tests",
]
