"""Owner profile endpoints — read/update the brain's "who I am" record.

GET  /api/v1/profile  -> the current owner profile
PUT  /api/v1/profile  -> overwrite-in-place (partial updates allowed)

The profile is small business data (Postgres) injected into every chat turn; see
ira/owner_profile.py. PR #66 adds the identity/onboarding fields (preferred_title,
wake_word, voice_enabled). `role` and `first_run_completed` are read-only here:
role is fixed to owner_admin, and the first-run flag is owned by the
/onboarding endpoints.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.middleware.auth import require_owner
import owner_profile as _profile

router = APIRouter(prefix="/profile", tags=["profile"])


class ProfileBody(BaseModel):
    name: str | None = None
    goals: str | None = None
    projects: str | None = None
    preferences: str | None = None
    preferred_title: str | None = Field(None, max_length=40)
    wake_word: str | None = Field(None, max_length=24)
    voice_enabled: bool | None = None


#: `require_owner`, not `require_auth` — P12-06 FINDING L.
#:
#: `owner_profile` is a SINGLETON: `INSERT … (id) VALUES (TRUE)`,
#: `UPDATE … WHERE id IS TRUE`. One row for the whole deployment, no tenant
#: column, no RLS. These two routes took `require_auth` alone and the router is
#: mounted with no dependencies, so ANY authenticated account read the owner's
#: name, goals and projects — and, through the PUT, overwrote the record
#: `owner_profile.py` injects into every chat turn.
#:
#: The 3C-10 debt list already records this table as *"reachable only through an
#: owner-gated route, and `is_owner` refuses in cloud"*. That was not a
#: description; there was no owner gate anywhere on this path. This makes the
#: recorded mitigation TRUE rather than inventing a new one.
#:
#: **Deliberately not a schema change.** `TENANCY_COMPLETION_MIGRATION_PLAN.md`
#: §2 decides `owner_profile`'s future — *decompose* into `user_profiles`
#: (USER_PRIVATE) + `tenant_settings` (TENANT_SCOPED) + `organisation_profiles`,
#: **"Not migrated in Phase 1"**, scheduled for batch 10. Bolting a `tenant_id`
#: onto the singleton here would build the thing that plan says to replace, and
#: would do it inside a truth-pass batch. The gate closes the live defect now
#: and leaves the decomposition where its owner put it.
@router.get("")
async def get_owner_profile(_user: str = Depends(require_owner)) -> _profile.OwnerProfile:
    return await _profile.get_profile()


@router.put("")
async def put_owner_profile(
    body: ProfileBody,
    _user: str = Depends(require_owner),
) -> _profile.OwnerProfile:
    # Only send provided fields so omitted ones keep their current value.
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    return await _profile.update_profile(**fields)
