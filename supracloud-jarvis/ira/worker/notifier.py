"""
IRA Multi-Channel Notifier.

Delivers proactive alerts through all configured channels simultaneously:
  WebSocket  — real-time push to any connected browser/app client (always on)
  Telegram   — mobile push via bot (if TELEGRAM_BOT_TOKEN set)
  Email      — SMTP (if SMTP_HOST set)
  Redis      — internal pub/sub (always; other services can subscribe)

Usage:
    await notify("Security Alert", "3 failed SSH attempts from 1.2.3.4",
                 category="security", priority="high")
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import smtplib
import ssl
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Literal

import httpx

from config import get_settings
from utils.redis_client import get_redis

logger = logging.getLogger("ira.notifier")

Priority = Literal["info", "warning", "critical"]
Category = Literal["briefing", "security", "business", "reminder", "task", "system"]

# Redis channel name — WebSocket handler subscribes to this
#: The legacy global channel. Retained ONLY as the private edition's channel
#: name, so an existing single-tenant deployment keeps working across this
#: change. It must never be subscribed in cloud — see `notify_channel`.
REDIS_NOTIFY_CHANNEL = "ira:notifications"


def notify_channel(principal=None) -> str:
    """The Redis channel a notification belongs on.

    ── The finding this exists to close (3C-03) ─────────────────────────────

    Every notification used to be published to one hardcoded global channel,
    and `/ws/notifications` subscribed any client with a decodable token to it
    and forwarded everything. So every authenticated user in every tenant
    received every notification generated anywhere in the deployment — a live
    cross-tenant leak of `title` and `body`, which for a briefing is the
    owner's own content.

    ── Why the private edition keeps the old name ───────────────────────────

    A single-tenant deployment has exactly one tenant, so the global channel was
    never a leak there and renaming it would break clients mid-upgrade for no
    security gain. Cloud gets a per-tenant channel and cloud is where the
    boundary exists.

    ── Why this refuses rather than defaulting ─────────────────────────────

    A cloud notification with no principal has no channel it could safely go
    to. Publishing it to the global one is the defect; publishing it to a
    guessed tenant is worse. So it raises, and the caller has to say who the
    notification is for.
    """
    from core.editions import EDITION_CLOUD, current_edition

    if current_edition() != EDITION_CLOUD:
        return REDIS_NOTIFY_CHANNEL

    from core.identity.db_context import MissingTenantContext

    tenant = getattr(principal, "tenant_id", None) if principal is not None else None
    if tenant is None:
        raise MissingTenantContext(
            "notify() needs a Principal in the cloud edition: a notification "
            "carries a title and a body, and there is no channel it can safely "
            "go to without naming a tenant. The global channel is the defect "
            "this replaces, and a guessed tenant would be worse."
        )
    return f"ira:t:{tenant}:notifications"


# ── Core notify function ───────────────────────────────────────────────────────

async def notify(
    title: str,
    body: str,
    *,
    category: Category = "system",
    priority: Priority = "info",
    metadata: dict | None = None,
    principal=None,
) -> str:
    """
    Send a notification via all configured channels.
    Returns the notification UUID for reference.
    """
    notif_id, _channels = await notify_report(
        title, body, category=category, priority=priority,
        metadata=metadata, principal=principal,
    )
    return notif_id


async def notify_report(
    title: str,
    body: str,
    *,
    category: Category = "system",
    priority: Priority = "info",
    metadata: dict | None = None,
    principal=None,
) -> tuple[str, list[str]]:
    """Send like `notify`, and also return the channels that actually took it.

    A caller that tells the owner where an alert went must use this, not `notify`:
    Telegram, email and mobile push are each skipped when unconfigured and logged
    when they fail, so "sent to your phone" is true only if "telegram" or "mobile"
    is in the list.
    """
    notif_id = str(uuid.uuid4())
    channels_sent: list[str] = []
    cfg = get_settings()

    payload = {
        "id": notif_id,
        "category": category,
        "title": title,
        "body": body,
        "priority": priority,
        "metadata": metadata or {},
    }

    # Always publish to Redis (WebSocket clients and internal subscribers), on
    # the channel that names this notification's tenant.
    await _publish_redis(payload, principal=principal)
    channels_sent.append("websocket")

    # Telegram (if configured)
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        try:
            await _send_telegram(title, body, priority, cfg)
            channels_sent.append("telegram")
        except Exception as e:
            logger.warning(f"Telegram delivery failed: {type(e).__name__}")

    # Email (if configured) — only for warning/critical or briefings
    if cfg.smtp_host and cfg.smtp_user and (priority in ("warning", "critical") or category == "briefing"):
        try:
            await _send_email(title, body, category, cfg)
            channels_sent.append("email")
        except Exception as e:
            logger.warning(f"Email delivery failed: {type(e).__name__}")

    # Mobile push (Expo) — if enabled and devices are registered (#3 mobile app)
    try:
        from worker.push_mobile import mobile_push_enabled, send_push
        if mobile_push_enabled():
            sent = await send_push(title, body, priority=priority,
                                   data={"category": category, "id": notif_id})
            if sent:
                channels_sent.append("mobile")
    except Exception as e:
        logger.warning(f"Mobile push delivery failed: {type(e).__name__}")

    # Persist to notifications table
    await _persist(notif_id, category, title, body, priority, channels_sent,
                   metadata or {}, principal=principal)

    logger.info(f"Notification [{priority}] '{title}' → {channels_sent}")
    return notif_id, channels_sent


# ── Redis pub/sub ─────────────────────────────────────────────────────────────

async def _publish_redis(payload: dict, *, principal=None) -> None:
    await get_redis().publish(notify_channel(principal), json.dumps(payload))


# ── Telegram ──────────────────────────────────────────────────────────────────

_PRIORITY_EMOJI = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}

# Fix P8: use HTML mode + html.escape() so user-controlled content (lead names,
# scan output) cannot inject Markdown formatting or break the message (Telegram 400).
async def _send_telegram(title: str, body: str, priority: Priority, cfg) -> None:
    emoji = _PRIORITY_EMOJI.get(priority, "ℹ️")
    safe_title = html.escape(str(title))
    safe_body  = html.escape(str(body))
    text = f"{emoji} <b>{safe_title}</b>\n\n{safe_body}"
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json={
            "chat_id": cfg.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        resp.raise_for_status()


# ── Email (SMTP) ──────────────────────────────────────────────────────────────

async def _send_email(title: str, body: str, category: str, cfg) -> None:
    loop = asyncio.get_running_loop()  # Fix L2: get_event_loop() deprecated in Python 3.10+
    await loop.run_in_executor(None, _send_email_sync, title, body, category, cfg)


def _send_email_sync(title: str, body: str, category: str, cfg) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[IRA] {title}"
    msg["From"] = cfg.smtp_user
    msg["To"] = cfg.smtp_to or cfg.smtp_user

    # Plain text
    msg.attach(MIMEText(body, "plain"))

    # Simple HTML version — escape body/title to prevent HTML injection
    html_body = f"""<html><body>
    <h2 style="color:#1a1a2e;">IRA — {html.escape(title)}</h2>
    <div style="font-family:sans-serif;white-space:pre-wrap;">{html.escape(body)}</div>
    <hr><p style="color:#888;font-size:12px;">SupraCloud IRA — Private AI Assistant</p>
    </body></html>"""
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as server:
        # A verifying context: without one, starttls() checks neither the
        # certificate nor the name, and the password follows.
        server.starttls(context=ssl.create_default_context())
        server.login(cfg.smtp_user, cfg.smtp_password)
        server.send_message(msg)


# ── Database persistence ──────────────────────────────────────────────────────

async def _persist(
    notif_id: str,
    category: str,
    title: str,
    body: str,
    priority: str,
    channels: list[str],
    metadata: dict,
    *,
    principal=None,
) -> None:
    """Write the notification row, on a connection that names its tenant.

    3C-10: this used a bare `acquire()`, and `notifications` had no tenant
    column — so the same `title` and `body` 3C-03 stopped broadcasting over
    Redis were still readable by every tenant through
    `GET /api/v1/notifications`. `051` gave the table a tenant and RLS; this is
    the write side of it.

    The connection is chosen rather than assumed:

      * a principal names its own tenant, so `tenant_connection` binds it;
      * without one, the private edition falls back to `owner_connection`, which
        is what that helper is for — a single-owner deployment has exactly one
        tenant and the thirteen callers audited in 3C-09 all live there;
      * cloud without a principal **refuses**. `notify()` already raises earlier,
        when `notify_channel` cannot name a channel — this is the second,
        independent closure, so a reordering of the delivery blocks cannot turn
        an unattributable notification into a stored row.
    """
    from core.editions import EDITION_CLOUD, current_edition
    from core.identity.db_context import (
        MissingTenantContext,
        owner_connection,
        tenant_connection,
    )

    if principal is not None:
        connection = tenant_connection(principal)
    elif current_edition() == EDITION_CLOUD:
        raise MissingTenantContext(
            "Persisting a notification in the cloud edition requires a "
            "Principal: the row needs a tenant, and there is no safe tenant to "
            "guess. See 3C-10."
        )
    else:
        connection = owner_connection("persist a notification (private edition)")

    async with connection as conn:
        await conn.execute(
            """INSERT INTO notifications
               (id, category, title, body, priority, channels_sent, metadata,
                tenant_id, owner_user_id)
               VALUES ($1, $2, $3, $4, $5, $6::text[], $7,
                       NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                       (SELECT id FROM users WHERE id = NULLIF(current_setting('app.user_id', true), '')::uuid))""",
            uuid.UUID(notif_id), category, title, body, priority, channels,
            json.dumps(metadata),
        )
