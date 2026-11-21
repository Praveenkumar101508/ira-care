"""
Router registration — every API router IRA exposes, in registration order.

Order matters in two places: canary honeypot paths must be registered before
any catch-all, and health stays unprefixed for probes.
"""

from __future__ import annotations

from fastapi import FastAPI

from api.routes.chat import router as chat_router
from api.routes.memory_decisions import router as memory_decisions_router
from api.routes.health import router as health_router
from api.routes.tasks import router as tasks_router
from api.routes.notifications import router as notifications_router
from api.routes.briefing import router as briefing_router
from api.routes.voice import router as voice_router
from api.routes.backup import router as backup_router
from api.routes.image_gen import router as image_gen_router
from api.routes.architect import router as architect_router
from api.routes.video_gen import router as video_gen_router
from api.routes.document_create import router as document_create_router
from api.routes.design_tools import router as design_tools_router
from api.routes.audio_gen import router as audio_gen_router
from api.routes.deep_research import router as deep_research_router
from utils.canary import canary_router

def register_routers(app: FastAPI) -> None:
    """Include every router on the app (same order as the old main.py)."""
    # P5.2: honeypot paths FIRST — must be registered before any catch-all 404
    app.include_router(canary_router)
    app.include_router(health_router)
    app.include_router(chat_router, prefix="/api/v1")
    app.include_router(memory_decisions_router, prefix="/api/v1")
    app.include_router(tasks_router, prefix="/api/v1")
    app.include_router(briefing_router, prefix="/api/v1")
    app.include_router(notifications_router)         # /notifications + /ws/notifications
    app.include_router(voice_router, prefix="/api/v1")   # /voice/token + /voice/enroll
    app.include_router(backup_router, prefix="/api/v1")      # /backup/list + /backup/download + /backup/restore
    app.include_router(image_gen_router, prefix="/api/v1")       # /image/generate + /image/edit
    app.include_router(architect_router, prefix="/api/v1")       # /architect/propose + /implement + /apply
    app.include_router(video_gen_router, prefix="/api/v1")       # /video/generate + /video/understand
    app.include_router(document_create_router, prefix="/api/v1") # /document/create + /document/download
    app.include_router(design_tools_router, prefix="/api/v1")    # /design/generate + /design/download
    app.include_router(audio_gen_router, prefix="/api/v1")       # /audio/generate + /audio/tts + /audio/transcribe
    app.include_router(deep_research_router, prefix="/api/v1")   # /research/deep + /research/article + /research/report

    from api.routes.totp import router as totp_router
    app.include_router(totp_router)                              # Feat P26: /auth/totp/enroll + /auth/totp/verify

    from api.routes.profile import router as profile_router
    app.include_router(profile_router, prefix="/api/v1")         # v1 1.4: /profile owner profile (GET/PUT)

    from api.routes.brain import router as brain_router
    app.include_router(brain_router)                             # /ws/brain (continuous brain, OFF by default)

    from api.routes.mobile import router as mobile_router
    app.include_router(mobile_router)                            # /mobile/* (mobile app support, push OFF by default)

    from api.routes.trust import router as trust_router
    app.include_router(trust_router, prefix="/api/v1")           # Personal v1: /trust/status (privacy/locality console)

    # Mounted in EVERY edition, deliberately: it is the endpoint a client asks
    # when it does not know which product it is talking to, so an edition that
    # withheld it would be the one case where the answer is unavailable.
    from api.routes.edition import router as edition_router
    app.include_router(edition_router, prefix="/api/v1")         # /edition — which product this is, and what it offers

    # Billing mounts in CLOUD only. A private install has one owner and nobody
    # to bill, so the surface is absent rather than present-and-refusing — see
    # `core/editions.py` for why a 403 would be the wrong answer here.
    from core.editions import edition_default
    if edition_default("billing_available"):
        from api.routes.billing import router as billing_router
        app.include_router(billing_router, prefix="/api/v1")     # P11-05: /billing/status, /billing/invoices

    from api.routes.memory import router as memory_router
    app.include_router(memory_router, prefix="/api/v1")          # Personal v1: /memory vault (owner CRUD, forget gated)

    from api.routes.onboarding import router as onboarding_router
    app.include_router(onboarding_router, prefix="/api/v1")      # PR #66: /onboarding first-run setup state

    from api.routes.people import router as people_router
    app.include_router(people_router, prefix="/api/v1")          # PR #67: /people relationship memory (confirm-gated)

    from api.routes.access import router as access_router
    app.include_router(access_router, prefix="/api/v1")          # PR #67: /access delegated accounts (password+confirm gated)

    from api.routes.companion import router as companion_router
    app.include_router(companion_router, prefix="/api/v1")       # Phase 3: /companion avatar+voice catalogue and companion profile (licence-gated)

    from api.routes.governance import router as governance_router
    app.include_router(governance_router, prefix="/api/v1")      # Phase 4: /governance User Constitution + Permission Broker (deterministic, fail-closed)

    from api.routes.privacy import router as privacy_router
    app.include_router(privacy_router, prefix="/api/v1")         # Phase 5: /privacy Privacy Centre — transparent memory, honest erasure

    from api.routes.realtime import router as realtime_router
    app.include_router(realtime_router, prefix="/api/v1")        # Phase 6: /realtime live session ladder + telemetry (no audio, no transcripts)

    from api.routes.models import router as models_router
    app.include_router(models_router, prefix="/api/v1")           # Phase 8: /models provider disclosure + usage + break-glass access log

    # Phase 3B Batch 5C: /workspaces — the picker a client reads BEFORE it can
    # send X-Workspace-ID. Deliberately not workspace-scoped; see the module
    # docstring and tests/tenancy/test_workspace_scope_boundary.py.
    from api.routes.workspaces import router as workspaces_router
    app.include_router(workspaces_router, prefix="/api/v1")

    # Creating, renaming and leaving a workspace mount in CLOUD only. The picker
    # above stays in both editions — a client that could not list its workspaces
    # could not tell whether it needs to choose one. See `core/editions.py`.
    if edition_default("workspace_management_available"):
        from api.routes.workspaces import management_router
        app.include_router(management_router, prefix="/api/v1")  # P12-02: create, rename, leave
