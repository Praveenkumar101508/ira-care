""""I'm on my way" is a promise with a deadline.

When a responder acknowledges an alert, `follow_up_due_at` is set to now plus
the person's `follow_up_minutes` (30 by default). This module runs on every
monitoring pass:

    acknowledged, deadline passed, responder not yet asked
        -> push the responder: "Have you reached [Name]? Tap I've arrived, or
           record what happened." Recorded as an attempt. A grace period starts.
    acknowledged, asked, grace passed, still no arrival and no outcome
        -> the alert goes back to `escalating` and the chain restarts from the
           next tier (tier 3 repeats itself). `re_escalations` counts it.

An arrival (`arrived`, `checked_in_person`, `spoke_to_person`) clears the
deadline. Nothing closes the alert except a resolution with an outcome.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Optional

from core.identity.principal import Principal

from . import push

logger = logging.getLogger("ira.care.followup")


@dataclass
class FollowUpOutcome:
    asked: int = 0
    re_escalated: int = 0


async def _responder(conn, alert_id) -> Optional[dict]:
    """The newest member who said they were on their way."""
    row = await conn.fetchrow(
        """
        SELECT m.* FROM care_acknowledgements a
          JOIN care_circle_members m ON m.id = a.member_id
         WHERE a.alert_id = $1 AND a.kind = 'on_my_way'
         ORDER BY a.acknowledged_at DESC LIMIT 1
        """, alert_id)
    return dict(row) if row else None


def question(person_name: str) -> str:
    return (f"Have you reached {person_name}? Tap I've arrived, or record what happened. "
            f"If nobody answers, the alert will be raised again.")


async def run(conn, principal: Principal, now: dt.datetime, *,
              push_provider: Optional[push.PushProvider] = None) -> FollowUpOutcome:
    out = FollowUpOutcome()
    push_provider = push_provider or push.provider_from_env()
    due = await conn.fetch(
        "SELECT a.*, p.display_name, p.preferred_name FROM care_alerts a JOIN care_people p ON p.id = a.person_id "
        "WHERE a.status = 'acknowledged' AND a.follow_up_due_at IS NOT NULL AND a.follow_up_due_at <= $1 "
        "ORDER BY a.follow_up_due_at", now)
    for row in due:
        alert = dict(row)
        name = alert["preferred_name"] or alert["display_name"]
        settings = await conn.fetchrow("SELECT follow_up_grace_minutes FROM care_safety_settings WHERE person_id = $1",
                                       alert["person_id"])
        grace = int(settings["follow_up_grace_minutes"]) if settings else 10
        if alert["follow_up_asked_at"] is None:
            responder = await _responder(conn, alert["id"])
            outcome, detail, tickets = ("skipped", "no responder on record", [])
            if responder is not None:
                outcome, detail, tickets = await push.send_to_member(
                    conn, responder, alert, f"IRA CARE: have you reached {name}?", question(name), push_provider)
            attempt_id = await conn.fetchval(
                "INSERT INTO care_alert_attempts (alert_id, tier, member_id, channel, outcome, detail) "
                "VALUES ($1, $2, $3, 'push', $4, $5) RETURNING id",
                alert["id"], max(1, int(alert["current_tier"])), responder["id"] if responder else None,
                outcome, ("follow-up: " + detail)[:1000])
            await push.record_tickets(conn, alert["id"], attempt_id, tickets)
            await conn.execute(
                "UPDATE care_alerts SET follow_up_asked_at = $2, follow_up_due_at = $3 "
                "WHERE id = $1 AND status = 'acknowledged'", alert["id"], now, now + dt.timedelta(minutes=grace))
            out.asked += 1
        else:
            details = alert["details"] if isinstance(alert["details"], dict) else json.loads(alert["details"] or "{}")
            responder = await _responder(conn, alert["id"])
            who = responder["display_name"] if responder else "the responder"
            details["summary"] = (details.get("summary", "") + f" {who} said they were on their way but nobody has "
                                  f"arrived or recorded an outcome; the alert has been raised again.").strip()
            details["re_escalated_at"] = now.isoformat()
            await conn.execute(
                "UPDATE care_alerts SET status = 'escalating', next_action_at = $2, follow_up_due_at = NULL, "
                "follow_up_asked_at = NULL, re_escalations = re_escalations + 1, details = $3::jsonb "
                "WHERE id = $1 AND status = 'acknowledged'", alert["id"], now, json.dumps(details))
            out.re_escalated += 1
            logger.info("care follow-up: alert %s re-escalated after no arrival", alert["id"])
    return out


__all__ = ["run", "FollowUpOutcome", "question"]
