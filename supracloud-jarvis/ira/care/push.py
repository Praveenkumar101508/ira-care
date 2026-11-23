"""Care push: to the phones of the people an alert is for, and nobody else.

The server's general notifier (`worker/notifier.py`) has a mobile channel that
sends to every phone registered with the deployment. A care alert is for the
members of ONE person's care circle, so care push never goes through it
(`tests/care/test_care_push_rules.py` guards that). Instead:

* a phone registers for push with `POST /api/v1/care/push/register`; the token
  is tied to the signed-in account, to each active circle membership that
  account holds in the workspace, to the person, and to the tenant and the
  workspace, under forced row-level security (`care_push_tokens`, 107);
* the escalation chain hands each member's active tokens to the push provider
  and records one `care_push_tickets` row per token with the provider's ticket;
* receipts are fetched later (Expo answers them after delivery) and the ticket
  becomes `delivered` or `failed`; a token the service says is gone is revoked;
* a member who leaves the circle, and an account that is erased, lose their
  tokens; a revoked token is never sent to again.

Two providers: `LogOnlyPushProvider` (the default: records, sends nothing) and
`ExpoPushProvider`, used when `IRA_CARE_PUSH_PROVIDER=expo`. Nothing here is
paid; Expo's push service is the free channel the family app is built on.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional, Protocol

from core.identity.principal import Principal

logger = logging.getLogger("ira.care.push")

EXPO_SEND_URL = "https://exp.host/--/api/v2/push/send"
EXPO_RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"
_TOKEN_RE = re.compile(r"^Expo(?:nent)?PushToken\[[A-Za-z0-9_\-]+\]$")
#: Receipt errors that mean the token is dead and must not be used again.
DEAD_TOKEN_ERRORS = frozenset({"DeviceNotRegistered", "InvalidCredentials"})


def is_valid_token(token: str) -> bool:
    return bool(token) and bool(_TOKEN_RE.match(token.strip()))


@dataclass(frozen=True)
class Ticket:
    token: str
    ticket_id: str
    status: str      # sent | failed | logged
    detail: str = ""


@dataclass(frozen=True)
class Receipt:
    ticket_id: str
    status: str      # delivered | failed
    detail: str = ""
    dead_token: bool = False


class PushProvider(Protocol):
    name: str

    async def send(self, messages: list[dict]) -> list[Ticket]: ...
    async def receipts(self, ticket_ids: list[str]) -> list[Receipt]: ...


class LogOnlyPushProvider:
    """Records what would have been sent; sends nothing. The default."""
    name = "log_only"

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, messages: list[dict]) -> list[Ticket]:
        out = []
        for m in messages:
            self.sent.append(m)
            logger.info("care push (log only) to %s: %s", _mask(m["to"]), str(m.get("title", ""))[:60])
            out.append(Ticket(m["to"], f"log-{uuid.uuid4().hex[:12]}", "logged", "log-only provider: no push was sent"))
        return out

    async def receipts(self, ticket_ids: list[str]) -> list[Receipt]:
        return []


class ExpoPushProvider:
    """Expo's push service: tickets on send, receipts on request."""
    name = "expo"

    def __init__(self, access_token: str = "") -> None:
        self._headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if access_token:
            self._headers["Authorization"] = f"Bearer {access_token}"

    async def send(self, messages: list[dict]) -> list[Ticket]:
        import httpx
        out: list[Ticket] = []
        for i in range(0, len(messages), 100):
            batch = messages[i:i + 100]
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.post(EXPO_SEND_URL, json=batch, headers=self._headers)
                data = r.json().get("data", []) if r.status_code // 100 == 2 else []
            except Exception as exc:  # noqa: BLE001 - a provider failure is recorded, never raised
                out.extend(Ticket(m["to"], "", "failed", f"provider error: {type(exc).__name__}") for m in batch)
                continue
            for m, t in zip(batch, data if len(data) == len(batch) else [{}] * len(batch)):
                if t.get("status") == "ok":
                    out.append(Ticket(m["to"], str(t.get("id", ""))[:200], "sent"))
                else:
                    err = ((t.get("details") or {}).get("error") or t.get("message") or "provider refused")
                    out.append(Ticket(m["to"], "", "failed", str(err)[:400]))
        return out

    async def receipts(self, ticket_ids: list[str]) -> list[Receipt]:
        import httpx
        out: list[Receipt] = []
        for i in range(0, len(ticket_ids), 300):
            batch = ticket_ids[i:i + 300]
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.post(EXPO_RECEIPTS_URL, json={"ids": batch}, headers=self._headers)
                data = r.json().get("data", {}) if r.status_code // 100 == 2 else {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("care push receipts failed: %s", type(exc).__name__)
                return out
            for tid, rec in data.items():
                if rec.get("status") == "ok":
                    out.append(Receipt(tid, "delivered"))
                else:
                    err = str((rec.get("details") or {}).get("error") or rec.get("message") or "error")
                    out.append(Receipt(tid, "failed", err[:400], dead_token=err in DEAD_TOKEN_ERRORS))
        return out


def provider_from_env(env: Optional[dict] = None) -> PushProvider:
    env = os.environ if env is None else env
    if (env.get("IRA_CARE_PUSH_PROVIDER") or "").strip().lower() == "expo":
        return ExpoPushProvider((env.get("IRA_CARE_EXPO_ACCESS_TOKEN") or "").strip())
    return LogOnlyPushProvider()


def _mask(token: str) -> str:
    return token[:18] + "…" if len(token) > 18 else "…"


# ── Registration ──────────────────────────────────────────────────────────────

@dataclass
class Registration:
    memberships: int = 0
    tokens: list = field(default_factory=list)


async def register(conn, principal: Principal, token: str, platform: str = "",
                   person_id: Optional[uuid.UUID] = None) -> Registration:
    """Tie this phone's token to every active circle membership the signed-in
    account holds here (or to one person's). Re-registering refreshes
    `last_seen_at` and re-activates a token the phone had unregistered."""
    rows = await conn.fetch(
        "SELECT id, person_id FROM care_circle_members WHERE user_id = $1 AND active "
        "AND ($2::uuid IS NULL OR person_id = $2)", principal.user_id, person_id)
    reg = Registration(memberships=len(rows))
    for r in rows:
        row = await conn.fetchrow(
            """
            INSERT INTO care_push_tokens (user_id, member_id, person_id, token, platform)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (member_id, token) DO UPDATE
               SET status = 'active', revoked_at = NULL, revoked_reason = '', last_seen_at = NOW(),
                   platform = EXCLUDED.platform
            RETURNING id, member_id, person_id, status
            """, principal.user_id, r["id"], r["person_id"], token.strip(), platform or "")
        reg.tokens.append(dict(row))
    return reg


async def unregister(conn, principal: Principal, token: str) -> int:
    """The phone signed out or turned push off: its token is revoked for this account."""
    tag = await conn.execute(
        "UPDATE care_push_tokens SET status = 'revoked', revoked_at = NOW(), revoked_reason = 'unregistered by the phone' "
        "WHERE user_id = $1 AND token = $2 AND status = 'active'", principal.user_id, token.strip())
    return int(str(tag).rsplit(" ", 1)[-1] or 0)


async def revoke_for_member(conn, member_id: uuid.UUID, reason: str) -> int:
    """A member left the circle (or was deactivated): nothing more reaches their phone for this person."""
    tag = await conn.execute(
        "UPDATE care_push_tokens SET status = 'revoked', revoked_at = NOW(), revoked_reason = $2 "
        "WHERE member_id = $1 AND status = 'active'", member_id, reason[:120])
    return int(str(tag).rsplit(" ", 1)[-1] or 0)


async def registrations(conn, principal: Principal) -> list[dict]:
    rows = await conn.fetch(
        "SELECT id, member_id, person_id, platform, status, created_at, last_seen_at FROM care_push_tokens "
        "WHERE user_id = $1 ORDER BY created_at", principal.user_id)
    return [dict(r) for r in rows]


# ── Sending, as part of the escalation chain ─────────────────────────────────

def _message(token: str, title: str, body: str, alert: dict) -> dict:
    return {"to": token, "title": title, "body": body, "priority": "high", "sound": "default",
            "channelId": "care-alerts",
            "data": {"care_alert_id": str(alert["id"]), "kind": alert["kind"], "severity": alert["severity"]}}


async def send_to_member(conn, member: dict, alert: dict, title: str, body: str,
                         provider: Optional[PushProvider] = None) -> tuple[str, str, list[dict]]:
    """Push to this member's active tokens only. Returns (outcome, detail, tickets)
    for the attempt row: `sent` when the provider took at least one message,
    `logged` with the log-only provider, `skipped` when the member has no phone
    registered, `failed` when every message was refused."""
    provider = provider or provider_from_env()
    rows = await conn.fetch(
        "SELECT id, token FROM care_push_tokens WHERE member_id = $1 AND status = 'active' ORDER BY created_at",
        member["id"])
    if not rows:
        return "skipped", "no phone registered for push by this member", []
    by_token = {r["token"]: r["id"] for r in rows}
    tickets = await provider.send([_message(r["token"], title, body, alert) for r in rows])
    recorded = []
    for t in tickets:
        recorded.append({"token_id": by_token.get(t.token), "ticket_id": t.ticket_id, "status": t.status,
                         "detail": t.detail})
    statuses = {t.status for t in tickets}
    if "sent" in statuses:
        outcome = "sent"
    elif "logged" in statuses:
        outcome = "logged"
    else:
        outcome = "failed"
    detail = f"{len(rows)} phone(s); " + ", ".join(sorted(f"{s}: {sum(1 for t in tickets if t.status == s)}" for s in statuses))
    return outcome, detail, recorded


async def record_tickets(conn, alert_id: uuid.UUID, attempt_id: Optional[uuid.UUID], tickets: list[dict]) -> None:
    for t in tickets:
        await conn.execute(
            "INSERT INTO care_push_tickets (alert_id, attempt_id, token_id, ticket_id, status, detail) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            alert_id, attempt_id, t.get("token_id"), t.get("ticket_id", ""), t.get("status", "sent"), (t.get("detail") or "")[:400])


async def fetch_receipts(conn, provider: Optional[PushProvider] = None, *, now: Optional[dt.datetime] = None,
                         min_age_s: int = 15) -> dict:
    """Ask the provider about tickets still waiting for a receipt (Expo asks for
    about 15 seconds' patience). Marks tickets delivered or failed and revokes
    tokens the service says are gone."""
    provider = provider or provider_from_env()
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = await conn.fetch(
        "SELECT id, ticket_id, token_id FROM care_push_tickets WHERE status = 'sent' AND receipt_at IS NULL "
        "AND ticket_id <> '' AND created_at <= $1 ORDER BY created_at LIMIT 300",
        now - dt.timedelta(seconds=min_age_s))
    out = {"asked": len(rows), "delivered": 0, "failed": 0, "revoked": 0}
    if not rows:
        return out
    by_ticket = {r["ticket_id"]: r for r in rows}
    for rec in await provider.receipts(list(by_ticket)):
        row = by_ticket.get(rec.ticket_id)
        if row is None:
            continue
        await conn.execute(
            "UPDATE care_push_tickets SET status = $2, detail = $3, receipt_at = $4 WHERE id = $1",
            row["id"], rec.status, rec.detail[:400], now)
        out[rec.status] += 1
        if rec.dead_token and row["token_id"] is not None:
            tag = await conn.execute(
                "UPDATE care_push_tokens SET status = 'revoked', revoked_at = $2, revoked_reason = $3 "
                "WHERE id = $1 AND status = 'active'", row["token_id"], now, f"push service: {rec.detail[:90]}")
            out["revoked"] += int(str(tag).rsplit(" ", 1)[-1] or 0)
    return out


def delivery_state(tickets: list[dict]) -> str:
    """One word for an attempt's push delivery, from its tickets."""
    statuses = [t["status"] for t in tickets]
    if not statuses:
        return "no phone registered"
    if "delivered" in statuses:
        return "delivered"
    if all(s == "logged" for s in statuses):
        return "logged only"
    if "sent" in statuses:
        return "sent, delivery not yet confirmed"
    return "failed"


__all__ = ["is_valid_token", "provider_from_env", "LogOnlyPushProvider", "ExpoPushProvider", "register",
           "unregister", "revoke_for_member", "registrations", "send_to_member", "record_tickets",
           "fetch_receipts", "delivery_state", "Ticket", "Receipt", "DEAD_TOKEN_ERRORS"]
