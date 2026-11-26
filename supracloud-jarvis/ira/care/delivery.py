"""Getting an alert message out, and knowing honestly how far it got.

The escalation chain (care/escalation.py) decides WHO is told. It writes each
message as a `queued` row in `care_alert_attempts`, in the same transaction
that moves the alert on, and commits. This module then sends those rows, outside
that transaction:

* an outbox row (`care_alert_dispatch`) is CLAIMED with `FOR UPDATE SKIP
  LOCKED` and given a short lease (`next_try_at`), so two workers never send the
  same message at once, and a worker
  that dies mid-send leaves a row another worker picks up when the lease runs
  out (at least once: a person may get the same text twice, never none);
* each send has its own time limit, and sends run side by side up to a cap, so
  one slow provider cannot hold up another person's emergency;
* a send that fails is tried again with a growing wait, up to MAX_TRIES, then
  recorded as failed;
* the state recorded is what the provider said and no more:

      queued      written, not yet handed over
      accepted    the provider took it (HTTP 2xx). NOT delivered.
      delivered   the provider says it reached the phone, or the call connected
      undelivered / failed / no_answer / busy   it did not
      logged      the log-only provider: nothing was sent
      skipped     not sent on purpose (an emergency number, nobody to tell)

  None of these is a person reading it. Only an acknowledgement in the app
  ("I'm on my way") is (care/repository.acknowledge_alert).

Twilio tells us later how far a message or call got, by calling
`/care/providers/twilio/status/{tenant}/{workspace}/{attempt}` with a signed
form. The URL carries everything needed to find the row under the tenant's own
row-level security, plus a token of ours; Twilio's signature covers the whole
URL, so neither can be altered. The row exists before the message is sent, so a
callback can never arrive for a row that is not there yet. States only move
forward (`status_rank`), so a callback that comes twice, or late, or before the
send was recorded, changes nothing it should not. Every report is kept as a row
of `care_alert_attempt_events` (insert-only); the attempt itself is never
rewritten.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("ira.care.delivery")

#: How long a claimed row is held before another worker may take it.
LEASE = dt.timedelta(minutes=2)
#: Tries before a message is recorded as failed for good.
MAX_TRIES = 3
#: One send may take this long before it counts as failed (and is retried).
SEND_TIMEOUT_S = 20.0
#: Sends at once, across people.
CONCURRENCY = 8
#: Rows claimed per pass.
BATCH = 50

#: How far along each recorded state is. A state is only ever replaced by a
#: higher one, so the order of arrival does not matter.
RANK = {"queued": 0, "logged": 50, "skipped": 50, "accepted": 30, "sent": 30, "unknown": 35,
        "delivered": 90, "undelivered": 95, "failed": 95, "no_answer": 95, "busy": 95}

#: Twilio's words for a message or a call, to ours.
TWILIO_STATUS = {
    # messages
    "accepted": "accepted", "scheduled": "accepted", "queued": "accepted", "sending": "accepted",
    "sent": "accepted", "delivered": "delivered", "read": "delivered", "undelivered": "undelivered",
    "failed": "failed", "canceled": "failed",
    # calls
    "initiated": "accepted", "ringing": "accepted", "in-progress": "delivered", "answered": "delivered",
    "completed": "delivered", "busy": "busy", "no-answer": "no_answer",
}


def rank(outcome: str) -> int:
    return RANK.get(outcome, 0)


# ── Signed status callbacks ──────────────────────────────────────────────────

def _secret(env=None) -> bytes:
    env = os.environ if env is None else env
    return (env.get("IRA_CARE_CALLBACK_SECRET") or "").encode()


def callback_token(tenant_id, workspace_id, attempt_id, *, env=None) -> str:
    """Our own token in the callback URL: proof the URL was made here, for this row."""
    secret = _secret(env)
    if not secret:
        return ""
    msg = f"{tenant_id}:{workspace_id}:{attempt_id}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()[:40]


def status_callback_url(tenant_id, workspace_id, attempt_id, *, env=None) -> Optional[str]:
    """The URL Twilio reports back to, or None when this deployment has no
    public HTTPS address or no callback secret (then the state stays
    'accepted': honest, and visible as 'delivery not confirmed')."""
    env = os.environ if env is None else env
    base = (env.get("IRA_CARE_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    token = callback_token(tenant_id, workspace_id, attempt_id, env=env)
    if not base.startswith("https://") or not token:
        return None
    return f"{base}/api/v1/care/providers/twilio/status/{tenant_id}/{workspace_id}/{attempt_id}?t={token}"


#: An alarm-receiving centre's words for how far it got, to ours. "handling" means an
#: operator has the alarm: the furthest a centre can report, and still not a person
#: reaching the one who needs help.
ARC_STATUS = {"received": "accepted", "queued": "accepted", "handling": "delivered",
              "rejected": "failed", "failed": "failed"}


def arc_callback_url(tenant_id, workspace_id, alert_id, *, env=None) -> Optional[str]:
    """Where the centre reports back about one alert: our token in the URL (as for
    Twilio), the centre's own signature on the body. None without a public HTTPS
    address or a callback secret; the state then stays 'accepted'."""
    env = os.environ if env is None else env
    base = (env.get("IRA_CARE_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    token = callback_token(tenant_id, workspace_id, alert_id, env=env)
    if not base.startswith("https://") or not token:
        return None
    return f"{base}/api/v1/care/providers/arc/status/{tenant_id}/{workspace_id}/{alert_id}?t={token}"


def twilio_signature(url: str, params: dict, auth_token: str) -> str:
    """Twilio's request signature: HMAC-SHA1 over the full URL followed by each
    POST parameter name and value, sorted by name, base64-encoded."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def verify_twilio(url: str, params: dict, signature: str, auth_token: str) -> bool:
    if not auth_token or not signature:
        return False
    return hmac.compare_digest(twilio_signature(url, params, auth_token), signature)


def verify_token(tenant_id, workspace_id, attempt_id, token: str, *, env=None) -> bool:
    expected = callback_token(tenant_id, workspace_id, attempt_id, env=env)
    return bool(expected) and hmac.compare_digest(expected, token or "")


# ── Recording a state ────────────────────────────────────────────────────────

async def current_states(conn, attempt_ids) -> dict:
    """{attempt_id: the furthest state recorded}: the attempt's own outcome, or
    a later event that went further. Ties go to the most recent."""
    ids = list(attempt_ids)
    if not ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT a.id, a.outcome AS initial, a.provider_ref AS initial_ref, a.detail AS initial_detail,
               e.outcome, e.status_rank, e.provider_status, e.provider_ref, e.detail, e.recorded_at
          FROM care_alert_attempts a
          LEFT JOIN LATERAL (
                SELECT * FROM care_alert_attempt_events ev WHERE ev.attempt_id = a.id
                 ORDER BY ev.status_rank DESC, ev.recorded_at DESC LIMIT 1) e ON TRUE
         WHERE a.id = ANY($1::uuid[])
        """, ids)
    out = {}
    for r in rows:
        refs = await conn.fetchval(
            "SELECT provider_ref FROM care_alert_attempt_events WHERE attempt_id = $1 AND provider_ref <> '' "
            "ORDER BY recorded_at LIMIT 1", r["id"])
        if r["outcome"] is not None and r["status_rank"] > rank(r["initial"]):
            out[r["id"]] = {"outcome": r["outcome"], "status_rank": r["status_rank"],
                            "provider_status": r["provider_status"], "detail": r["detail"] or r["initial_detail"],
                            "provider_ref": refs or r["initial_ref"], "status_at": r["recorded_at"]}
        else:
            out[r["id"]] = {"outcome": r["initial"], "status_rank": rank(r["initial"]), "provider_status": "",
                            "detail": r["initial_detail"], "provider_ref": refs or r["initial_ref"], "status_at": None}
    return out


async def record_state(conn, attempt_id: uuid.UUID, outcome: str, *, source: str = "dispatcher",
                       provider_status: str = "", detail: str = "", provider_ref: str = "") -> bool:
    """Write what the provider reported as a new event. It becomes the
    message's state only if it went further than anything recorded before;
    otherwise it is kept as history and changes nothing (returns False)."""
    before = (await current_states(conn, [attempt_id])).get(attempt_id)
    if before is None:
        return False
    await conn.execute(
        """
        INSERT INTO care_alert_attempt_events
            (attempt_id, outcome, status_rank, provider_status, provider_ref, detail, source)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (attempt_id, status_rank, provider_status) DO NOTHING
        """, attempt_id, outcome, rank(outcome), (provider_status or outcome)[:40], (provider_ref or "")[:200],
        (detail or "")[:1000], source)
    return rank(outcome) > before["status_rank"]


@dataclass
class DispatchOutcome:
    claimed: int = 0
    accepted: int = 0
    delivered: int = 0
    failed: int = 0
    retried: int = 0
    logged: int = 0
    skipped: int = 0


# ── Sending what is queued ───────────────────────────────────────────────────

async def _claim(principal, now: dt.datetime) -> list[dict]:
    """Take up to BATCH outbox rows whose time has come and hold each for
    LEASE. Rows another worker holds are skipped, not waited for."""
    from core.identity.db_context import workspace_connection
    async with workspace_connection(principal) as conn:
        rows = [dict(r) for r in await conn.fetch(
            """
            SELECT d.id AS dispatch_id, d.tries, a.*
              FROM care_alert_dispatch d JOIN care_alert_attempts a ON a.id = d.attempt_id
             WHERE d.finished_at IS NULL AND d.next_try_at <= $1
             ORDER BY d.next_try_at
             LIMIT $2
             FOR UPDATE OF d SKIP LOCKED
            """, now, BATCH)]
        for r in rows:
            await conn.execute("UPDATE care_alert_dispatch SET tries = tries + 1, next_try_at = $2 WHERE id = $1",
                               r["dispatch_id"], now + LEASE)
            r["tries"] += 1
    return rows


async def _finish(principal, attempt: dict, outcome: str, now: dt.datetime, **state) -> str:
    from core.identity.db_context import workspace_connection
    async with workspace_connection(principal) as conn:
        await record_state(conn, attempt["id"], outcome, **state)
        await conn.execute("UPDATE care_alert_dispatch SET finished_at = $2 WHERE id = $1",
                           attempt["dispatch_id"], now)
    return outcome


async def _send_one(principal, attempt: dict, now: dt.datetime, provider, push_provider,
                    sem: asyncio.Semaphore, email_provider=None) -> str:
    """Send one claimed message and record what the provider said."""
    from core.identity.db_context import workspace_connection

    from . import escalation, providers, push
    async with sem:
        async with workspace_connection(principal) as conn:
            alert = await conn.fetchrow("SELECT * FROM care_alerts WHERE id = $1", attempt["alert_id"])
            member = await conn.fetchrow("SELECT * FROM care_circle_members WHERE id = $1", attempt["member_id"]) \
                if attempt["member_id"] else None
            person = await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", alert["person_id"]) if alert else None
        if alert is None or person is None or member is None or not member["active"]:
            return await _finish(principal, attempt, "skipped", now, detail="nobody to send to any more")
        alert, person, member = dict(alert), dict(person), dict(member)
        if isinstance(alert.get("details"), str):
            import json
            alert["details"] = json.loads(alert["details"])
        if alert["status"] in ("resolved", "cancelled"):
            return await _finish(principal, attempt, "skipped", now, detail="the alert was resolved before this was sent")
        title, body = escalation.alert_text(person, alert, attempt.get("wording_tier") or attempt["tier"])
        channel = attempt["channel"]
        try:
            if channel == "push":
                async with workspace_connection(principal) as conn:
                    out, detail, tickets = await asyncio.wait_for(
                        push.send_to_member(conn, member, alert, title, body, push_provider), SEND_TIMEOUT_S)
                    await push.record_tickets(conn, alert["id"], attempt["id"], tickets)
                delivery = providers.Delivery("accepted" if out == "sent" else out, detail)
            elif channel in ("sms", "call"):
                if not member["phone"]:
                    delivery = providers.Delivery("skipped", "no phone number")
                else:
                    callback = status_callback_url(principal.tenant_id, principal.workspace_id, attempt["id"])
                    send = provider.send_sms if channel == "sms" else provider.call
                    delivery = await asyncio.wait_for(
                        send(member["phone"], f"{title}. {body}", status_callback=callback), SEND_TIMEOUT_S)
            elif channel == "email":
                if email_provider is None or not getattr(email_provider, "configured", False):
                    delivery = providers.Delivery("skipped", "no mail server is configured")
                elif not member["email"]:
                    delivery = providers.Delivery("skipped", "no email address")
                else:
                    delivery = await asyncio.wait_for(
                        email_provider.send_email(member["email"], title, body), SEND_TIMEOUT_S)
            else:
                delivery = providers.Delivery("skipped", f"channel {channel} is not sent by the dispatcher")
        except providers.EmergencyNumberRefused as exc:
            delivery = providers.Delivery("skipped", str(exc)[:200])
        except asyncio.TimeoutError:
            delivery = providers.Delivery("failed", f"no answer from the provider within {SEND_TIMEOUT_S:g} s")
        except Exception as exc:  # noqa: BLE001 - a provider failure is recorded, never raised
            delivery = providers.Delivery("failed", f"provider error: {type(exc).__name__}")
        outcome = "accepted" if delivery.outcome == "sent" else delivery.outcome
        if outcome == "failed" and attempt["tries"] < MAX_TRIES:
            wait = dt.timedelta(seconds=30 * (2 ** (attempt["tries"] - 1)))
            async with workspace_connection(principal) as conn:
                # Kept as history (rank 0: it moves nothing), and tried again later.
                await record_state(conn, attempt["id"], "queued", provider_status=f"try {attempt['tries']} failed",
                                   detail=f"try {attempt['tries']} of {MAX_TRIES} failed: {delivery.detail}")
                await conn.execute("UPDATE care_alert_dispatch SET next_try_at = $2 WHERE id = $1",
                                   attempt["dispatch_id"], now + wait)
            return "retried"
        return await _finish(principal, attempt, outcome, now, detail=delivery.detail,
                             provider_ref=delivery.provider_ref)


async def dispatch(principal, now: Optional[dt.datetime] = None, *, provider=None, push_provider=None,
                   email_provider=None) -> DispatchOutcome:
    """Send every queued message of one workspace whose time has come."""
    from . import providers, push
    now = now or dt.datetime.now(dt.timezone.utc)
    provider = provider or providers.provider_from_env()
    push_provider = push_provider or push.provider_from_env()
    email_provider = email_provider or providers.email_from_env()
    out = DispatchOutcome()
    rows = await _claim(principal, now)
    out.claimed = len(rows)
    if not rows:
        return out
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(_send_one(principal, r, now, provider, push_provider, sem, email_provider)
                                     for r in rows),
                                   return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            logger.warning("care delivery: a send could not be recorded (%s)", type(r).__name__)
            out.failed += 1
        elif r == "retried":
            out.retried += 1
        elif hasattr(out, r):
            setattr(out, r, getattr(out, r) + 1)
        elif r in ("undelivered", "no_answer", "busy"):
            out.failed += 1
    return out


async def settle_push_attempts(conn, now: dt.datetime) -> int:
    """After push receipts arrive: a push with a delivered ticket is delivered;
    one whose every ticket failed is failed.

    Only attempts whose receipt event is not yet recorded are returned. Every
    escalation pass calls this; without that filter each pass re-read every
    push attempt that ever had a receipt and wrote a duplicate event for each
    (which the unique key then discarded), so a pass's work and memory grew
    with the history of the deployment. The filter matches that key
    (attempt, rank, provider status), so what is recorded is unchanged."""
    rows = await conn.fetch(
        """
        SELECT s.* FROM (
            SELECT t.attempt_id,
                   bool_or(t.status = 'delivered') AS any_delivered,
                   bool_and(t.status = 'failed') AS all_failed
              FROM care_push_tickets t JOIN care_alert_attempts a ON a.id = t.attempt_id
             WHERE a.channel = 'push' AND t.receipt_at IS NOT NULL
             GROUP BY t.attempt_id) s
         WHERE (s.any_delivered AND NOT EXISTS (
                    SELECT 1 FROM care_alert_attempt_events ev WHERE ev.attempt_id = s.attempt_id
                       AND ev.status_rank = $1 AND ev.provider_status = 'receipt: ok'))
            OR (NOT s.any_delivered AND s.all_failed AND NOT EXISTS (
                    SELECT 1 FROM care_alert_attempt_events ev WHERE ev.attempt_id = s.attempt_id
                       AND ev.status_rank = $2 AND ev.provider_status = 'receipt: error'))
        """, rank("delivered"), rank("failed"))
    moved = 0
    for r in rows:
        if r["any_delivered"]:
            moved += await record_state(conn, r["attempt_id"], "delivered", source="receipt", provider_status="receipt: ok")
        elif r["all_failed"]:
            moved += await record_state(conn, r["attempt_id"], "failed", source="receipt", provider_status="receipt: error")
    return moved


__all__ = ["dispatch", "record_state", "settle_push_attempts", "status_callback_url", "verify_twilio",
           "verify_token", "twilio_signature", "callback_token", "TWILIO_STATUS", "rank", "DispatchOutcome", "current_states",
           "MAX_TRIES", "LEASE", "SEND_TIMEOUT_S"]
