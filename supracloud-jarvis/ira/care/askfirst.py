"""Ask first, then escalate.

When a safety rule fires (a heart rate confirmed outside the range, a
suspected fall, no movement after the usual wake time), IRA does not go
straight to the SOS chain. The watch vibrates and asks "Are you OK?" with big
"I'm OK" and "Help" buttons and speech:

  * "I'm OK" (button or words): the alert is resolved as answered OK, and the
    daily report lists it under safety checks;
  * "Help" (button or words): the alert opens at once and the chain runs;
  * no answer within `ask_first_wait_s` (60 by default): the alert opens and
    the chain runs.

The alert row carries the state: `asking` with `next_action_at` as the
deadline and `checkin_id` pointing at the safety check-in that is the
question. `care/escalation.py` promotes an overdue `asking` alert to `open`
in its normal pass, so the deadline is kept by the server even if the watch
has gone quiet.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Optional

from .monitoring import Observation

logger = logging.getLogger("ira.care.askfirst")

QUESTION = "{name}, are you OK? Tap I'm OK, or say help."


async def start(conn, person: dict, settings: dict, obs: Observation, now: dt.datetime,
                severity: str = "sos") -> Optional[dict]:
    """Open an `asking` alert and the safety check-in that carries the question.
    None if an alert of this kind is already unresolved for the person."""
    exists = await conn.fetchval(
        "SELECT id FROM care_alerts WHERE person_id = $1 AND kind = $2 "
        "AND status IN ('asking', 'open', 'escalating', 'acknowledged')", person["id"], obs.kind)
    if exists is not None:
        return None
    name = person.get("preferred_name") or person["display_name"]
    checkin = await conn.fetchrow(
        """
        INSERT INTO care_checkins (person_id, scheduled_at, status, question_kind, question_text)
        VALUES ($1, $2, 'scheduled', 'safety', $3) RETURNING *
        """, person["id"], now, QUESTION.format(name=name))
    wait = int(settings.get("ask_first_wait_s", 60))
    details = dict(obs.details)
    details.update({"summary": obs.summary, "ask_first": True, "ask_first_wait_s": wait})
    alert = await conn.fetchrow(
        """
        INSERT INTO care_alerts (person_id, checkin_id, kind, severity, status, details, opened_at, next_action_at)
        VALUES ($1, $2, $3, $4, 'asking', $5::jsonb, $6, $7) RETURNING *
        """, person["id"], checkin["id"], obs.kind, severity, json.dumps(details), now,
        now + dt.timedelta(seconds=wait))
    out = dict(alert)
    out["details"] = details
    return out


async def answered(conn, checkin: dict, *, ok: bool, now: dt.datetime, words: str = "") -> Optional[dict]:
    """The safety question was answered. OK resolves the asking alert; help
    opens it now. Returns the alert, or None if the check-in was not a safety one."""
    alert = await conn.fetchrow(
        "SELECT * FROM care_alerts WHERE checkin_id = $1 AND status = 'asking'", checkin["id"])
    if alert is None:
        return None
    details = alert["details"]
    if isinstance(details, str):
        details = json.loads(details)
    if ok:
        details["outcome"] = "answered_ok"
        row = await conn.fetchrow(
            "UPDATE care_alerts SET status = 'resolved', resolved_at = $2, next_action_at = NULL, "
            "resolution = $3, details = $4::jsonb WHERE id = $1 RETURNING *",
            alert["id"], now, "The person answered the safety check: OK.", json.dumps(details))
    else:
        details["outcome"] = "asked_for_help"
        row = await conn.fetchrow(
            "UPDATE care_alerts SET status = 'open', next_action_at = $2, details = $3::jsonb WHERE id = $1 RETURNING *",
            alert["id"], now, json.dumps(details))
    return dict(row)


async def no_answer(conn, checkin: dict, now: dt.datetime) -> Optional[dict]:
    """The safety question went unanswered: the alert opens now."""
    row = await conn.fetchrow(
        "UPDATE care_alerts SET status = 'open', next_action_at = $2, "
        "details = details || '{\"outcome\": \"no_answer\"}'::jsonb "
        "WHERE checkin_id = $1 AND status = 'asking' RETURNING *", checkin["id"], now)
    return dict(row) if row else None


async def promote_overdue(conn, now: dt.datetime) -> int:
    """Every `asking` alert whose deadline passed opens now. The question's
    check-in is marked missed so the record says nobody answered."""
    rows = await conn.fetch(
        "UPDATE care_alerts SET status = 'open', next_action_at = $1, "
        "details = details || '{\"outcome\": \"no_answer\"}'::jsonb "
        "WHERE status = 'asking' AND next_action_at <= $1 RETURNING checkin_id", now)
    for r in rows:
        if r["checkin_id"] is not None:
            await conn.execute(
                "UPDATE care_checkins SET status = 'missed', answer_summary = 'No answer to the safety check.' "
                "WHERE id = $1 AND status IN ('scheduled', 'asked', 'repeated')", r["checkin_id"])
    return len(rows)


async def promote_on_help(conn, person_id, now: dt.datetime) -> Optional[dict]:
    """The Help button while a question is out: open the asking alert instead
    of a second row."""
    row = await conn.fetchrow(
        "UPDATE care_alerts SET status = 'open', next_action_at = $2, "
        "details = details || '{\"outcome\": \"asked_for_help\"}'::jsonb "
        "WHERE person_id = $1 AND status = 'asking' RETURNING *", person_id, now)
    return dict(row) if row else None
