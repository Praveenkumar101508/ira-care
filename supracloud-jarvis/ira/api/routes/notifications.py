"""
Real-time notification endpoints.

GET  /notifications           — list recent notifications (paginated)
POST /notifications/{id}/read — mark as read
DELETE /notifications/read    — clear all read notifications
WS   /ws/notifications        — WebSocket: real-time push stream

WebSocket protocol:
  Client connects:  ws://host/ws/notifications?token=<JWT>
  Server sends:     {"id":"...","category":"security","title":"...","body":"...","priority":"critical"}
  Client sends:     {"action":"ping"} → {"action":"pong"}
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from api.middleware.auth import require_auth, decode_token
from api.middleware.workspace import require_workspace_principal
from core.identity.db_context import workspace_connection
from core.identity.principal import Principal
from utils.redis_client import get_pubsub_redis, get_redis
from worker.notifier import notify_channel

router = APIRouter(tags=["notifications"])
logger = logging.getLogger("ira.notifications")


# ── REST endpoints ────────────────────────────────────────────────────────────

@router.get("/notifications")
async def list_notifications(
    limit: int = Query(50, le=200),
    unread_only: bool = Query(False),
    _user: str = Depends(require_auth),
    _p: Principal = Depends(require_workspace_principal),
):
    # 3C-10: bound, because `notifications` now carries a tenant and RLS. This
    # handler used to open a bare connection and select with no predicate, so it
    # returned every tenant's notification `title` and `body`.
    async with workspace_connection(_p) as conn:
        if unread_only:
            rows = await conn.fetch(
                """SELECT id, category, title, body, priority, channels_sent, read, created_at
                   FROM notifications
                   WHERE read = FALSE
                   ORDER BY created_at DESC LIMIT $1""",
                limit,
            )
        else:
            rows = await conn.fetch(
                """SELECT id, category, title, body, priority, channels_sent, read, created_at
                   FROM notifications
                   ORDER BY created_at DESC LIMIT $1""",
                limit,
            )
    return [
        {
            "id": str(r["id"]),
            "category": r["category"],
            "title": r["title"],
            "body": r["body"],
            "priority": r["priority"],
            "channels_sent": r["channels_sent"],
            "read": r["read"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


@router.post("/notifications/{notif_id}/read")
async def mark_read(
    notif_id: str,
    _user: str = Depends(require_auth),
    _p: Principal = Depends(require_workspace_principal),
):
    async with workspace_connection(_p) as conn:
        await conn.execute(
            "UPDATE notifications SET read=TRUE WHERE id=$1",
            uuid.UUID(notif_id),
        )
    return {"status": "ok"}


@router.delete("/notifications/read")
async def clear_read(
    _user: str = Depends(require_auth),
    _p: Principal = Depends(require_workspace_principal),
):
    # The DELETE below has no predicate beyond `read`. Unbound, that cleared the
    # whole deployment for every tenant on one authenticated call; bound, RLS
    # confines it to the caller's own rows.
    async with workspace_connection(_p) as conn:
        rows = await conn.fetch(
            "DELETE FROM notifications WHERE read=TRUE RETURNING id"
        )
    return {"deleted": len(rows)}


# ── WebSocket real-time stream ────────────────────────────────────────────────

@router.websocket("/ws/notifications")
async def ws_notifications(
    websocket: WebSocket,
):
    """
    Real-time notification stream via WebSocket.
    Subscribes to Redis pub/sub and forwards all IRA notifications to the client.

    Authentication: send the JWT via the Sec-WebSocket-Protocol header using the
    format "bearer.<token>" — this keeps the token out of nginx access logs.
    Fallback: token query param is accepted for backwards compatibility in dev mode.
    """
    # Prefer token from Sec-WebSocket-Protocol header (avoids nginx log exposure)
    token: str | None = None
    selected_subprotocol: str | None = None
    protocol_header = websocket.headers.get("sec-websocket-protocol", "")
    for part in protocol_header.split(","):
        part = part.strip()
        if part.startswith("bearer."):
            token = part[len("bearer."):]
            selected_subprotocol = part  # must be echoed back in accept() — Fix #69
            break

    # Fallback: query param (dev / legacy clients)
    if not token:
        token = websocket.query_params.get("token")

    if not token:
        await websocket.close(code=4001)
        return

    # P2-06: the same gate every HTTP route gets. This used to be a bare
    # `decode_token`, which checks a signature and an expiry and nothing else —
    # so a *refresh* token opened this stream, and so did one that had already
    # been logged out. The watchdog started after accept() keeps that check
    # running for the socket's lifetime.
    try:
        from api.middleware.auth import authorise_socket_token

        claims = await authorise_socket_token(token)
    except Exception:
        await websocket.close(code=4001)
        return

    # ── Whose notifications is this connection allowed to hear? (3C-03) ──────
    #
    # A decodable token used to be the whole check, and the socket then
    # subscribed to a single global channel and forwarded everything on it. So
    # any authenticated user in any tenant received every notification in the
    # deployment — `title` and `body` included, which for a briefing is somebody
    # else's content.
    #
    # Resolving a Principal is what turns "this token parses" into "this person
    # is a member of this tenant". It is re-derived here at connect time rather
    # than trusted from the token's claims, because a token outlives the
    # membership it was issued under and a long-lived socket outlives both.
    #
    # In the private edition there is one channel, and what goes on it is the
    # owner's: briefings, reminders, security alerts. A household account
    # resolves to a Principal in the same tenant, so membership alone would
    # subscribe it to all of that. `DelegatedScopeMiddleware` cannot stop it:
    # `BaseHTTPMiddleware` passes WebSocket traffic straight through. So the
    # private edition's socket is the owner's alone, as `/ws/brain` is.
    try:
        from api.middleware.auth import is_owner
        from api.middleware.workspace import principal_from_username
        from core.editions import EDITION_CLOUD, current_edition

        username = getattr(claims, "sub", None) or ""
        principal = await principal_from_username(username)
        if current_edition() != EDITION_CLOUD and not is_owner(username):
            raise PermissionError("the private notifications stream is the owner's")
    except Exception as exc:  # noqa: BLE001 — a refusal, not a crash
        logger.warning("notifications socket refused: %s", type(exc).__name__)
        await websocket.close(code=4003)
        return

    channel = notify_channel(principal)

    # Fix #69: echo the selected subprotocol back in the handshake response.
    # Per RFC 6455, if the client offered subprotocols the server MUST reply
    # with the chosen one; without this, browsers using the standard WebSocket
    # API reject the connection with a subprotocol mismatch error.
    await websocket.accept(subprotocol=selected_subprotocol)
    logger.info("WebSocket client connected for real-time notifications")

    # P14-03 finding AC: the SUBSCRIPTION client, not the command client. The
    # latter now carries a 5s socket timeout so a hung Redis cannot block a
    # request forever, and that timeout would tear this socket down after five
    # seconds of a quiet channel. `utils/redis_client` explains why the two are
    # separate and why they must not be merged back.
    redis = get_pubsub_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)

    async def _send_loop():
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    await websocket.send_text(message["data"])
                except Exception:
                    break

    async def _receive_loop():
        """Handle ping/pong keepalive from client."""
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                    if msg.get("action") == "ping":
                        await websocket.send_text('{"action":"pong"}')
                except Exception:
                    pass
        except WebSocketDisconnect:
            pass

    # P2-06. A socket authorised once and then left alone outlives every
    # revocation control there is: /auth/logout and /auth/logout/all could not
    # reach it, and neither could the access token's own thirty-minute expiry.
    # The watchdog closes it when the credential dies; `gather` cancels the
    # watchdog when the client disconnects first.
    from api.middleware.auth import watch_socket_credential

    try:
        await asyncio.gather(
            _send_loop(),
            _receive_loop(),
            watch_socket_credential(websocket, claims),
        )
    except Exception as e:
        logger.warning(f"WebSocket session ended: {e}")
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        logger.info("WebSocket client disconnected")
