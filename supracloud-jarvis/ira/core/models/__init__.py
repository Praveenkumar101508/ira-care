"""The Model Gateway — privacy-aware routing, quotas, and break-glass access.

Phase 8. Implements the `ModelGateway` seam from Phase 1, whose contract is the
one this package is built around:

    routing must consider the principal's privacy policy and the content's
    sensitivity BEFORE availability or cost, and must refuse rather than
    downgrade privacy.

    policy.py        may this provider carry this at all
        ↓
    gateway.py       quota → availability → cost → call → meter
        ↓
    store.py         tenant-scoped, RLS, metering that cannot hold a prompt
        ↓
    admin_access.py  time-bound, user-authorised, logged reads

── Three claims, and their limits ───────────────────────────────────────────

**"Nothing sensitive reaches a third party unless you allowed it."** Enforced by
`policy.admits`, tested exhaustively over every privacy mode × sensitivity ×
provider posture, and written into the billing record: `model_usage` has CHECK
constraints making a `local_only` row with a non-local success, or a `credential`
row with a non-local success, unrepresentable.

**"Unknown retention is not permission."** A provider whose `retains_prompts` is
NULL has not stated its posture, and an unstated posture is refused for anything
above `normal`. The database column is nullable on purpose — defaulting it to
FALSE would silently convert "we have not checked" into "it does not retain".

**"No administrator reads your content without your authorisation."** Bounded by
the schema: a grant needs `user_authorised_at`, cannot exceed 8 hours, and the
access log's trigger refuses any row outside a live grant.

The honest limit on that last one: a schema cannot force code to log, and someone
with database credentials can read `messages` directly. What this design achieves
is that the audit log cannot contain a plausible-looking entry that was not
authorised — an unauthorised read leaves the log EMPTY rather than forged. That
is narrower than it first sounds, and it is stated in MODEL_GATEWAY.md rather
than left to be assumed.
"""
from __future__ import annotations

from core.models.admin_access import (
    MAX_GRANT_HOURS,
    SCOPES,
    AccessGrant,
    AdminAccessError,
    NotAuthorised,
    build_grant,
    read_for_admin,
)
from core.models.gateway import (
    IRAModelGateway,
    Quota,
    QuotaRefusal,
    UsageSoFar,
    check_quota,
    estimate_cost,
)
from core.models.policy import (
    NEVER_EXTERNAL,
    SENSITIVE,
    SENSITIVITY_LEVELS,
    PrivacyMode,
    RoutingRefusal,
    admissible,
    admits,
    describe_mode,
    normalise_mode,
    normalise_sensitivity,
)

__all__ = [
    "MAX_GRANT_HOURS",
    "NEVER_EXTERNAL",
    "SCOPES",
    "SENSITIVE",
    "SENSITIVITY_LEVELS",
    "AccessGrant",
    "AdminAccessError",
    "IRAModelGateway",
    "NotAuthorised",
    "PrivacyMode",
    "Quota",
    "QuotaRefusal",
    "RoutingRefusal",
    "UsageSoFar",
    "admissible",
    "admits",
    "build_grant",
    "check_quota",
    "describe_mode",
    "estimate_cost",
    "normalise_mode",
    "normalise_sensitivity",
    "read_for_admin",
]
