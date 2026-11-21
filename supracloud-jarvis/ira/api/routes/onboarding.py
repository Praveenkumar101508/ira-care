"""First-run onboarding endpoints — phone-style setup for a fresh IRA install.

GET  /api/v1/onboarding/status    -> is this a first run? + current owner identity
POST /api/v1/onboarding/complete  -> persist setup choices, mark first run done
POST /api/v1/onboarding/reset     -> clear the flag so the wizard runs again (testing)

The wizard itself lives in the frontend; these endpoints only own the state.
first_run_completed flips to True exclusively via /complete, so an interrupted
setup shows the wizard again on the next login. System readiness (Ollama, DB,
Redis) is checked by the wizard through the existing /health endpoints — it is
deliberately not duplicated here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.middleware.auth import require_owner
import owner_profile as _profile

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

# Suggested titles the wizard offers; free text is allowed but kept short and
# stripped of control characters so it stays a form of address, not a prompt.
_MAX_NAME_LEN = 80
_MAX_TITLE_LEN = 40
_MAX_WAKE_LEN = 24


def _clean(value: str, max_len: int) -> str:
    cleaned = "".join(ch for ch in (value or "") if ch.isprintable()).strip()
    return cleaned[:max_len]


class OnboardingStatus(BaseModel):
    first_run_completed: bool
    owner_name: str
    preferred_title: str
    role: str
    wake_word: str
    voice_enabled: bool


class CompleteBody(BaseModel):
    owner_name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    preferred_title: str = Field("", max_length=_MAX_TITLE_LEN)
    wake_word: str = Field("ira", max_length=_MAX_WAKE_LEN)
    voice_enabled: bool = False


def _to_status(profile: _profile.OwnerProfile) -> OnboardingStatus:
    return OnboardingStatus(
        first_run_completed=bool(profile.get("first_run_completed")),
        owner_name=profile.get("name", ""),
        preferred_title=profile.get("preferred_title", ""),
        role=profile.get("role", "owner_admin"),
        wake_word=profile.get("wake_word", "ira"),
        voice_enabled=bool(profile.get("voice_enabled")),
    )


#: All three routes are `require_owner` — P12-06 FINDING L, second half.
#:
#: They read and write the same DEPLOYMENT-WIDE SINGLETON `api/routes/profile.py`
#: does: `owner_profile`, keyed `id IS TRUE`, with no tenant column and no RLS.
#: On `require_auth` alone that meant any authenticated account could read
#: another's onboarding state, overwrite `owner_name` — which
#: `owner_profile.py` injects into every chat turn — and, via `/reset`, re-arm
#: the first-run wizard for the whole deployment.
#:
#: Gating this does not remove a cloud capability, because there was never a
#: coherent one here: a single `first_run_completed` flag shared by every tenant
#: cannot express per-tenant onboarding, and whichever tenant wrote last won.
#: Per-tenant onboarding needs the decomposition
#: `TENANCY_COMPLETION_MIGRATION_PLAN.md` §2 schedules for batch 10, not a
#: wider gate on a shared row.
@router.get("/status", response_model=OnboardingStatus)
async def onboarding_status(_user: str = Depends(require_owner)) -> OnboardingStatus:
    """First-run detection: false/missing first_run_completed means show the wizard."""
    return _to_status(await _profile.get_profile())


@router.post("/complete", response_model=OnboardingStatus)
async def complete_onboarding(
    body: CompleteBody,
    _user: str = Depends(require_owner),
) -> OnboardingStatus:
    """Persist the wizard's choices and mark the first run as completed."""
    owner_name = _clean(body.owner_name, _MAX_NAME_LEN)
    if not owner_name:
        raise HTTPException(status_code=400, detail="owner_name must not be blank.")
    profile = await _profile.complete_onboarding(
        owner_name=owner_name,
        preferred_title=_clean(body.preferred_title, _MAX_TITLE_LEN),
        wake_word=_clean(body.wake_word, _MAX_WAKE_LEN).lower() or "ira",
        voice_enabled=bool(body.voice_enabled),
    )
    return _to_status(profile)


@router.post("/reset", response_model=OnboardingStatus)
async def reset_onboarding(_user: str = Depends(require_owner)) -> OnboardingStatus:
    """Re-arm the first-run wizard (keeps the rest of the profile intact)."""
    return _to_status(await _profile.reset_onboarding())
