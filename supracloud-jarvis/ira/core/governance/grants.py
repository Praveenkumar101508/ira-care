"""Permission grants — what the user actually allowed, to whom, and until when.

A grant answers a narrower question than people expect. It is not "may IRA use
Gmail"; it is "may IRA use this scope, on this resource, for THIS purpose, right
now". All four parts are checked.

── Purpose binding ──────────────────────────────────────────────────────────

The part most systems omit. A grant carries the purpose the user was told about
when they gave it, and a request carries the purpose it is being made for. If
they do not match, the grant does not apply.

Without that, "read my calendar so I can suggest meeting times" becomes
authority to read the calendar for anything at all, forever — which is the
mechanism by which consent-based permission systems quietly become
unconditional ones. Matching is deliberately conservative: exact purpose, or a
grant explicitly marked as covering any purpose. No fuzzy matching, because
"close enough" is how the binding erodes.

── Three ways to be inert ───────────────────────────────────────────────────

`granted_at` unset, `expires_at` past, `revoked_at` set. Kept as three columns
rather than one status because they are different facts and the Privacy Centre
has to tell a user which one applies. "You never granted this" and "you revoked
this" are not the same sentence.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Iterable, Optional

from core.interfaces.permission import ActionRequest, Effect

#: A grant whose purpose is this covers any request purpose. It exists for
#: genuinely purpose-free scopes (reading one's own settings) and is deliberately
#: ugly to type, so it is never reached for out of convenience.
ANY_PURPOSE: Final[str] = "*any-purpose*"

CONFIRMATION_POLICIES: Final[tuple[str, ...]] = (
    "always", "once_per_session", "once_per_routine", "never",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class PermissionGrant:
    """One thing the user allowed."""

    connector: str
    scope: str
    purpose: str
    resource: str = "*"
    granted_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    confirmation_policy: str = "always"
    retention_policy: str = "none"
    grant_id: Optional[uuid.UUID] = None

    # ── State ───────────────────────────────────────────────────────────────

    def status(self, *, now: Optional[datetime] = None) -> str:
        """'active', 'never_granted', 'expired' or 'revoked'.

        Checked in that order deliberately: a revoked grant reads as revoked
        even if it also happens to have expired, because revocation is the fact
        the user acted on and the one they will look for.
        """
        moment = now or _now()
        if self.revoked_at is not None and self.revoked_at <= moment:
            return "revoked"
        if self.granted_at is None:
            return "never_granted"
        if self.expires_at is not None and self.expires_at <= moment:
            return "expired"
        return "active"

    def is_active(self, *, now: Optional[datetime] = None) -> bool:
        return self.status(now=now) == "active"

    # ── Matching ────────────────────────────────────────────────────────────

    def covers(self, request: ActionRequest, *, now: Optional[datetime] = None) -> bool:
        """Whether this grant authorises `request`.

        Every clause must hold. There is no partial credit and no "closest
        match" — a grant either covers the thing being attempted or it does not.
        """
        if not self.is_active(now=now):
            return False
        if self.connector != request.domain:
            return False
        if self.scope != request.action:
            return False
        if not self._resource_matches(request.resource):
            return False
        return self._purpose_matches(request.purpose)

    def _resource_matches(self, resource: str) -> bool:
        if self.resource == "*":
            return True
        if self.resource.endswith("/*"):
            return str(resource).startswith(self.resource[:-1])
        return str(resource) == self.resource

    def _purpose_matches(self, purpose: str) -> bool:
        if self.purpose == ANY_PURPOSE:
            return True
        # Exact, case-insensitive, whitespace-normalised. Nothing fuzzier:
        # "close enough" is how purpose binding erodes into no binding at all.
        return " ".join(self.purpose.lower().split()) == " ".join(
            str(purpose).lower().split()
        )

    # ── What a covering grant permits ───────────────────────────────────────

    def effect(self) -> Effect:
        """The effect this grant's confirmation policy implies.

        Note `never` maps to ALLOW but is still subject to the deployment
        ceiling downstream. A user setting "never ask me" for email sending does
        not get silent sending, because `email.send` has a `max_effect` of
        `require_confirmation`. The grant expresses a preference; the policy
        decides whether that preference is available.
        """
        return {
            "always": Effect.REQUIRE_CONFIRMATION,
            "once_per_session": Effect.ALLOW_FOR_SESSION,
            "once_per_routine": Effect.ALLOW_FOR_ROUTINE,
            "never": Effect.ALLOW,
        }.get(self.confirmation_policy, Effect.REQUIRE_CONFIRMATION)


def find_covering(
    request: ActionRequest,
    grants: Iterable[PermissionGrant],
    *,
    now: Optional[datetime] = None,
) -> Optional[PermissionGrant]:
    """The STRICTEST grant that covers the request, or None.

    Strictest rather than first-found: if a user holds both a broad grant and a
    narrow one that both cover an action, the safer reading is that they meant
    the narrow one to apply. Picking whichever happened to be returned first
    would make the outcome depend on row ordering.
    """
    from core.governance.effects import strictness

    covering = [g for g in grants if g.covers(request, now=now)]
    if not covering:
        return None
    return min(covering, key=lambda g: strictness(g.effect()))


__all__ = [
    "ANY_PURPOSE",
    "CONFIRMATION_POLICIES",
    "PermissionGrant",
    "find_covering",
]
