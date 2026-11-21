"""api/routes/brain.py — continuous realtime-brain WebSocket + lifecycle.

WS /ws/brain — stream the brain's private thoughts and suggested speech, and
feed it percepts. There is ONE persistent brain (single owner): it is created
and its tick loop is started in the app lifespan, so it survives across client
connections rather than being spun up per socket.

OFF BY DEFAULT. The brain calls the LLM continuously (reacting + idle
self-thought), so it must be explicitly opted into with IRA_BRAIN_ENABLED. When
disabled, the WebSocket accepts then closes with a clear message.

Protocol:
  Connect:  ws://host/ws/brain   (auth: Sec-WebSocket-Protocol: "bearer.<JWT>", or ?token=)
  Client →  {"type":"perceive","source":"user","text":"..."}   (source optional)
            {"action":"ping"}  →  {"action":"pong"}
  Server →  {"type":"thought","text":"..."}  |  {"type":"speak","text":"..."}
            {"type":"error","text":"..."}

Security: a client may NOT label a percept as a trusted source. Any client
supplied source is clamped to the external/untrusted set (never "internal"),
so all client input still flows through the brain's percept sanitization.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["brain"])
logger = logging.getLogger("ira.brain")

# Client-supplied percept sources are always external/untrusted. "internal"
# (IRA's own thought) and "timer" are reserved and never accepted from a client.
_CLIENT_SOURCES = {"user", "voice", "vision", "channel"}


def brain_enabled() -> bool:
    """Whether the continuous brain loop is opted in (default OFF).

    **Never in cloud, whatever the environment says.** The brain is a single
    process-global object — one event bus, one global workspace, one self-model
    — with no tenant dimension anywhere in it. `feed_percept` mixes every
    caller's transcripts into it and `/ws/brain` streams what comes out. In a
    multi-tenant deployment there is no correct per-tenant behaviour to give it,
    so the honest answer is that it does not run there. See 3C-05.
    """
    from core.editions import EDITION_CLOUD, current_edition

    if current_edition() == EDITION_CLOUD:
        return False
    return os.getenv("IRA_BRAIN_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def _safe_source(raw: object) -> str:
    """Clamp a client-supplied source to the untrusted external set."""
    s = str(raw or "user").strip().lower()
    return s if s in _CLIENT_SOURCES else "user"


async def feed_percept(app, source: str, text: str) -> bool:
    """Feed a percept to the running brain from another subsystem (voice, vision).

    The seam other IRA modules use to push perceptions in — e.g. an owner-gated
    voice transcript, or a vision caption. Returns True if delivered, False if the
    brain isn't running. The source is clamped to the untrusted external set and
    the call never raises, so callers can fire-and-forget safely.
    """
    text = (text or "").strip()
    if not text:
        return False
    safe_source = _safe_source(source)
    cognitive_delivered = False
    try:
        from cognition.ingest import feed_cognitive_percept
        from config import get_settings
        cognitive_delivered = await feed_cognitive_percept(
            get_settings().ira_admin_username, safe_source, text,
        )
    except Exception as exc:  # noqa: BLE001 - optional kernel must never break voice
        logger.debug("cognitive percept feed failed (non-fatal): %s", exc)
    brain = getattr(getattr(app, "state", None), "brain", None)
    if brain is None:
        return cognitive_delivered
    try:
        await brain.perceive(safe_source, text)
        return True
    except Exception as exc:  # noqa: BLE001 - never let a percept feed break a caller
        logger.debug("feed_percept failed (non-fatal): %s", exc)
        return False


# ── Lifecycle (called from main.lifespan) ────────────────────────────────────

async def start_brain(app) -> None:
    """Create the singleton brain and start its tick loop as a background task.

    No-op unless IRA_BRAIN_ENABLED. Failures are logged and swallowed so the
    brain can never block or crash API startup.
    """
    if not brain_enabled():
        logger.info("Realtime brain disabled (set IRA_BRAIN_ENABLED=true to enable)")
        return
    try:
        from agents.cortex_realtime_brain import (
            IraEmbedder, IraLLM, IraMemorySink, RealtimeBrain,
        )

        brain = RealtimeBrain(
            llm=IraLLM(), embedder=IraEmbedder(), memory=IraMemorySink(),
            affect=_build_affect(),
        )
        app.state.brain = brain
        brain.attn.load_weights(os.getenv("IRA_BRAIN_SALIENCE_PATH", ""))  # restore learned triage
        task = asyncio.create_task(brain.run())
        task.add_done_callback(
            lambda t: t.cancelled() or (t.exception() and logger.warning(
                "Realtime brain loop ended: %s", t.exception()))
        )
        app.state.brain_task = task

        # Periodic long-term memory consolidation (set IRA_BRAIN_CONSOLIDATE_SECS=0 to disable).
        secs = float(os.getenv("IRA_BRAIN_CONSOLIDATE_SECS", "300"))
        if secs > 0:
            app.state.brain_consolidate_task = asyncio.create_task(
                _consolidation_loop(brain, secs))
        logger.info("Realtime brain online")
    except Exception as exc:  # noqa: BLE001 - never block startup
        logger.warning("Realtime brain failed to start (non-fatal): %s", exc)
        app.state.brain = None


def _build_affect():
    """Construct the affective layer when IRA_BRAIN_AFFECT_ENABLED, else None.

    Wires relational recall to IRA's vector memory and loads any persisted mood.
    Fail-soft: any error → no affect layer (base behavior).
    """
    try:
        from agents.affect import AffectLayer, affect_enabled

        if not affect_enabled():
            return None

        # FINDING AH (P16-01). What stood here built a `_recall` closure calling
        # `retrieve(principal, ...)` where **`principal` was bound nowhere** —
        # not a parameter, not a module global, not an enclosing local. Every
        # call raised `NameError`, which the closure's own `except Exception`
        # swallowed into `[]`. The relational recall had never worked, and the
        # comment above it asserted the closure capture that was missing, so a
        # reader was told the design was deliberate.
        #
        # **It is not fixed by finding a principal, because there is none.**
        # This runs at API startup, `memory.store.retrieve` requires the
        # verified principal Phase 12 built, and minting one here would be
        # inventing workspace authority at startup — the single thing Phase 12
        # refuses to do. A background loop has no request to read a workspace
        # from, which is the true observation the old comment was reaching for.
        #
        # So the recall is **not wired**, and `AffectLayer` is told so:
        # `recall=None` takes a different branch from a recall that returns
        # nothing (`affect.py` checks `self.recall is None` before calling).
        # That restores the distinction Phase 13 exists to protect — "no recall
        # configured" is a state, "always empty" was a failure wearing one.
        logger.info(
            "affect: relational recall is NOT wired. It needs a verified "
            "principal and this runs at startup with no request (FINDING AH). "
            "The affect layer runs without relational memory rather than with "
            "a recall that silently returns nothing.")

        affect = AffectLayer(recall=None)
        affect.load()
        logger.info("Affective layer online")
        return affect
    except Exception as exc:  # noqa: BLE001
        logger.warning("Affective layer failed to start (non-fatal): %s", exc)
        return None


async def _consolidation_loop(brain, secs: float) -> None:
    """Summarize recent working memory into long-term memory every `secs` seconds."""
    while True:
        await asyncio.sleep(secs)
        try:
            await brain.consolidate()
            if getattr(brain, "affect", None) is not None:
                brain.affect.save()   # opportunistic mood persistence
            brain.attn.save_weights(os.getenv("IRA_BRAIN_SALIENCE_PATH", ""))  # + learned triage
        except Exception as exc:  # noqa: BLE001 - never let consolidation crash the task
            logger.warning("Realtime brain consolidation failed: %s", exc)


async def stop_brain(app) -> None:
    """Stop the tick loop and cancel its task on shutdown."""
    brain = getattr(app.state, "brain", None)
    if brain is not None:
        brain.stop()
        if getattr(brain, "affect", None) is not None:
            brain.affect.save()   # persist mood across restarts
        brain.attn.save_weights(os.getenv("IRA_BRAIN_SALIENCE_PATH", ""))  # persist learned triage
    for attr in ("brain_task", "brain_consolidate_task"):
        task = getattr(app.state, attr, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            setattr(app.state, attr, None)
    app.state.brain = None


def _get_brain(websocket: WebSocket):
    return getattr(websocket.app.state, "brain", None)


# ── WebSocket stream ─────────────────────────────────────────────────────────

@router.websocket("/ws/brain")
async def ws_brain(websocket: WebSocket):
    # Auth mirrors /ws/notifications: prefer the bearer.<token> subprotocol
    # (keeps the JWT out of proxy logs); fall back to ?token= for dev clients.
    token: str | None = None
    selected_subprotocol: str | None = None
    for part in websocket.headers.get("sec-websocket-protocol", "").split(","):
        part = part.strip()
        if part.startswith("bearer."):
            token = part[len("bearer."):]
            selected_subprotocol = part
            break
    if not token:
        token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001)
        return

    # lazy: keeps module import light
    from api.middleware.auth import authorise_socket_token

    # P2-06: the same gate every HTTP route gets. This was a bare
    # `decode_token` — signature and expiry, nothing else — so a *refresh*
    # token opened the brain stream, and so did one already logged out. Owner
    # authority below is a second closure, not a substitute: `is_owner` answers
    # for a username, and says nothing about whether that person's token is
    # still live.
    try:
        claims = await authorise_socket_token(token)
    except Exception:
        await websocket.close(code=4001)
        return

    # ── Whose brain is this connection allowed to hear? (3C-05) ──────────────
    #
    # A decodable token used to be the whole check — the same defect 3C-03 fixed
    # one file over, on `/ws/notifications`. Fixing that socket fixed a socket,
    # not the class.
    #
    # What is on the other side here is `app.state.brain`: one process-global
    # object, fed by `feed_percept` with chat and voice transcripts from every
    # caller, streaming `thought` and `speak` frames to every subscriber. The
    # code below calls it "the shared brain" in a comment, which is exactly what
    # it is.
    #
    # So: resolve a real Principal, and require owner authority. `is_owner`
    # refuses in cloud (3C-04), and `brain_enabled()` is False there anyway —
    # two independent closures, because this stream carries the deployment's own
    # inner monologue and one of them being edited away should not open it.
    try:
        from api.middleware.auth import is_owner
        from api.middleware.workspace import principal_from_username

        username = getattr(claims, "sub", None) or ""
        await principal_from_username(username)
        if not is_owner(username):
            raise PermissionError("brain stream is owner-only")
    except Exception as exc:  # noqa: BLE001 — a refusal, not a crash
        logger.warning("brain socket refused: %s", type(exc).__name__)
        await websocket.close(code=4003)
        return

    await websocket.accept(subprotocol=selected_subprotocol)

    brain = _get_brain(websocket)
    if brain is None:
        await websocket.send_text(json.dumps(
            {"type": "error", "text": "brain disabled (set IRA_BRAIN_ENABLED=true)"}))
        await websocket.close(code=1011)
        return

    async def _safe_send(payload: dict) -> None:
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception:  # noqa: BLE001 - socket may be gone
            pass

    # Fire-and-forget so a slow/stuck client can never stall the shared brain.
    def _frame(kind: str):
        def cb(text: str):
            asyncio.create_task(_safe_send({"type": kind, "text": text}))
        return cb

    thought_cb = _frame("thought")
    speak_cb = _frame("speak")
    brain.on_thought(thought_cb)
    brain.on_speak(speak_cb)
    logger.info("Brain WebSocket client connected")

    # P2-06. What is on the other side of this socket is the deployment's own
    # inner monologue, so a credential that has been revoked must stop it —
    # /auth/logout, /auth/logout/all and the token's own expiry all reached
    # every HTTP route and none of them reached here.
    from api.middleware.auth import watch_socket_credential

    watchdog = asyncio.create_task(watch_socket_credential(websocket, claims))

    async def _client_loop() -> None:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except Exception:  # noqa: BLE001
                continue
            if msg.get("action") == "ping":
                await _safe_send({"action": "pong"})
                continue
            if msg.get("type") == "perceive":
                text = str(msg.get("text") or "").strip()
                if text:
                    await brain.perceive(_safe_source(msg.get("source")), text)

    try:
        await _client_loop()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("Brain WebSocket session ended: %s", exc)
    finally:
        watchdog.cancel()
        brain.off_thought(thought_cb)
        brain.off_speak(speak_cb)
        logger.info("Brain WebSocket client disconnected")
