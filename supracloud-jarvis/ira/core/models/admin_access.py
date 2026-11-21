"""Break-glass administrator access — time-bound, user-authorised, logged.

The threat model names this as a Phase 8 risk in exactly these words:

    administrators reading user content without time-bound, logged,
    user-authorised access.

All three adjectives are load-bearing, and each is enforced somewhere different:

  time-bound       `029_commercial.sql` caps the window at 8 hours by CHECK, and
                   `open_window` refuses to issue a longer one.
  user-authorised  `user_authorised_at` is NOT NULL and there is deliberately no
                   column for "authorised by an administrator". A grant nobody
                   authorised cannot be inserted.
  logged           `admin_access_log` references a grant, and a trigger refuses a
                   log row whose grant is missing, revoked, outside its window,
                   for a different admin, or in a different tenant.

── The honest limit, stated up front ────────────────────────────────────────

A schema cannot force code to log. Someone with database credentials can read
`messages` directly and no trigger will notice.

What this design achieves is narrower and still worth having: **the audit log
cannot contain a plausible-looking entry that was not authorised.** An
unauthorised read leaves the log EMPTY rather than leaving a row that looks
fine. So "was this access authorised?" is answerable from the data, and the
failure mode is a visible absence rather than a convincing forgery.

The application-side chokepoint is `read_for_admin`, which takes a grant and
logs before returning anything. `tests/architecture/test_admin_access_boundary.py`
asserts nothing else in the codebase reads another user's rows.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Final, Optional, Sequence, TypeVar

logger = logging.getLogger("ira.models.admin_access")

#: The schema caps this too, by CHECK. Duplicated here so a caller asking for
#: longer is refused with an explanation rather than a constraint violation.
MAX_GRANT_HOURS: Final[int] = 8

#: A reason short enough to be meaningless is not a reason. "support" tells a
#: user nothing about why their conversations were read.
MIN_REASON_CHARS: Final[int] = 10

SCOPES: Final[tuple[str, ...]] = ("conversations", "memories", "receipts", "usage")

T = TypeVar("T")


class AdminAccessError(RuntimeError):
    """Base for every reason administrator access is refused."""


class NotAuthorised(AdminAccessError):
    """No live grant covers this read. Always a refusal, never a warning."""


@dataclass(frozen=True, slots=True)
class AccessGrant:
    """A user's authorisation for one administrator to look, briefly."""

    grant_id: str
    tenant_id: str
    subject_user_id: str
    admin_identity: str
    reason: str
    scope: str
    user_authorised_at: datetime
    granted_at: datetime
    expires_at: datetime
    revoked_at: Optional[datetime] = None

    def is_live(self, now: Optional[datetime] = None) -> bool:
        moment = now or datetime.now(timezone.utc)
        return (
            self.revoked_at is None
            and self.granted_at <= moment <= self.expires_at
        )

    def why_not_live(self, now: Optional[datetime] = None) -> str:
        """Which condition failed. Separate messages, because "not authorised"
        makes an incident un-triageable: revoked, expired and not-yet-valid are
        three different situations."""
        moment = now or datetime.now(timezone.utc)
        if self.revoked_at is not None:
            return f"the grant was revoked at {self.revoked_at.isoformat()}"
        if moment > self.expires_at:
            return f"the grant expired at {self.expires_at.isoformat()}"
        if moment < self.granted_at:
            return f"the grant does not begin until {self.granted_at.isoformat()}"
        return ""

    def public_dict(self) -> dict[str, object]:
        """What a user is shown. The reason is included deliberately — it is the
        minimum they are owed when someone reads their conversations."""
        return {
            "grant_id": self.grant_id,
            "admin": self.admin_identity,
            "reason": self.reason,
            "scope": self.scope,
            "authorised_at": self.user_authorised_at.isoformat(),
            "granted_at": self.granted_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "live": self.is_live(),
        }


def validate_request(
    *,
    admin_identity: str,
    reason: str,
    scope: str,
    hours: float,
    user_authorised_at: Optional[datetime],
) -> None:
    """Refuse a grant that should not exist, before it reaches the database.

    Each check raises with its own message. The schema enforces the same rules,
    but a constraint violation surfacing to an operator as
    `grant_window_is_bounded` is much worse than being told the window is capped
    at 8 hours.
    """
    if not admin_identity or not admin_identity.strip():
        raise AdminAccessError("a grant must name the administrator it authorises")
    if len(reason.strip()) < MIN_REASON_CHARS:
        raise AdminAccessError(
            f"a reason of at least {MIN_REASON_CHARS} characters is required. "
            f"This is shown to the user whose data will be read, and 'support' "
            f"tells them nothing."
        )
    if scope not in SCOPES:
        raise AdminAccessError(
            f"{scope!r} is not a scope. Valid: {', '.join(SCOPES)}"
        )
    if hours <= 0:
        raise AdminAccessError("a grant window must be positive")
    if hours > MAX_GRANT_HOURS:
        raise AdminAccessError(
            f"a grant window of {hours}h exceeds the {MAX_GRANT_HOURS}h maximum. "
            f"A window longer than a working day is not break-glass access, it "
            f"is access."
        )
    if user_authorised_at is None:
        raise AdminAccessError(
            "a grant requires the moment the USER authorised it. There is no "
            "path that lets an administrator authorise their own access."
        )


def build_grant(
    *,
    tenant_id: str,
    subject_user_id: str,
    admin_identity: str,
    reason: str,
    scope: str = "conversations",
    hours: float = 1.0,
    user_authorised_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The row to insert, validated. Does not touch the database."""
    validate_request(
        admin_identity=admin_identity, reason=reason, scope=scope, hours=hours,
        user_authorised_at=user_authorised_at,
    )
    moment = now or datetime.now(timezone.utc)
    return {
        "tenant_id": tenant_id,
        "subject_user_id": subject_user_id,
        "admin_identity": admin_identity.strip(),
        "reason": reason.strip(),
        "scope": scope,
        "user_authorised_at": user_authorised_at,
        "granted_at": moment,
        "expires_at": moment + timedelta(hours=hours),
    }


async def read_for_admin(
    grant: Optional[AccessGrant],
    *,
    admin_identity: str,
    scope: str,
    reader: Callable[[], Awaitable[Sequence[T]]],
    logger_fn: Callable[[AccessGrant, str, int], Awaitable[None]],
    now: Optional[datetime] = None,
) -> Sequence[T]:
    """The chokepoint. Read another user's content only through here.

    Order matters and is not negotiable: verify the grant, read, then LOG BEFORE
    RETURNING. Logging after returning would mean a crash between the two leaves
    an unlogged read — and an unlogged read is the thing this module exists to
    prevent.

    A logging failure therefore fails the whole read. That is the opposite of
    every other fail-soft decision in this codebase, and deliberately so: a
    metering row that goes missing costs a data point, whereas an administrator
    read that goes unlogged costs the only evidence that it was authorised.
    """
    if grant is None:
        raise NotAuthorised(
            "no grant covers this read. An administrator may read a user's "
            "content only inside a window that user authorised."
        )
    if grant.admin_identity != admin_identity:
        raise NotAuthorised(
            f"this grant authorises {grant.admin_identity}, not {admin_identity}"
        )
    if grant.scope != scope:
        raise NotAuthorised(
            f"this grant covers {grant.scope!r}, not {scope!r}. A separate scope "
            f"needs a separate authorisation."
        )
    if not grant.is_live(now):
        raise NotAuthorised(
            f"this grant is not currently valid: {grant.why_not_live(now)}"
        )

    rows = await reader()

    # Shape only, like an Action Receipt: the count and the scope, never the
    # content. A log that quoted what was read would itself become a copy of the
    # user's data, held for longer and read by more people.
    try:
        await logger_fn(grant, f"{len(rows)} {grant.scope} record(s)", len(rows))
    except Exception as exc:  # noqa: BLE001 - deliberately NOT swallowed
        raise AdminAccessError(
            f"the access could not be logged, so it is refused: {exc}. An "
            f"unlogged administrator read is exactly what this path exists to "
            f"prevent."
        ) from exc

    return rows


__all__ = [
    "MAX_GRANT_HOURS",
    "MIN_REASON_CHARS",
    "SCOPES",
    "AccessGrant",
    "AdminAccessError",
    "NotAuthorised",
    "build_grant",
    "read_for_admin",
    "validate_request",
]
