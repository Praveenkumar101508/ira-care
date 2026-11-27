"""IRA, personal to the person (110).

    the communication profile   how IRA speaks: pace, how much prompting, which
                                channels; with the person's consent ('topics'),
                                topics to avoid, familiar topics and who to offer
                                when a worry repeats. Edited by the person or
                                their representative; applied on the watch and
                                the phone through `/devices/me` and the apps.
    reminders                   real ones: "remind me to ring Sravani at three"
                                becomes a row with a time, asked on the watch
                                as a check-in at that time, then confirmed,
                                snoozed ("later", ten minutes) or skipped.
                                A medication reminder confirms in two steps:
                                "Have you taken X?" then "You've taken X, is
                                that right?" before a dose is recorded as taken.
                                Hydration prompts come from a setting
                                (`hydration_every_minutes`) in waking hours.
    what IRA remembers          the person (or representative) can read, change
                                or clear each day's summary; an edit is marked.
    repeated worries            when the same worry comes back, IRA does not
                                argue or diagnose: it acknowledges, offers a
                                familiar topic, and offers to contact the named
                                person. The rule lives in care/conversation.py
                                so the evaluation set can exercise it.

Nothing here gives medical advice, decides a dose, or dials anyone.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import uuid
from typing import Optional

from core.identity.db_context import workspace_connection
from core.identity.principal import Principal

from . import checkins as rules
from . import reports

logger = logging.getLogger("ira.care.personal")

PACES = ("slow", "normal")
PROMPTING_LEVELS = ("light", "standard", "full")
REMINDER_KINDS = ("reminder", "medication", "hydration")
REMINDER_STATUSES = ("pending", "asked", "confirming", "confirmed", "snoozed", "skipped", "missed", "cancelled")
SNOOZE_MINUTES = 10
#: A reminder nobody answered is asked again once, then marked missed.
MAX_ASKS = 2

PROFILE_DEFAULTS = {"pace": "normal", "prompting_level": "standard", "voice_on": True, "touch_on": True,
                    "vibration_on": True, "topics_avoid": [], "familiar_topics": [], "redirect_member_id": None,
                    "updated_by_role": "", "updated_at": None}


def _public(row) -> dict:
    out = {}
    for k, v in dict(row).items():
        if k in ("tenant_id", "workspace_id"):
            continue
        out[k] = v.isoformat() if isinstance(v, dt.datetime) else str(v) if isinstance(v, uuid.UUID) else v
    return out


# ── The communication profile ────────────────────────────────────────────────

async def get_profile(conn, person_id) -> dict:
    row = await conn.fetchrow("SELECT * FROM care_communication_profiles WHERE person_id = $1", person_id)
    out = dict(PROFILE_DEFAULTS)
    out["person_id"] = str(person_id)
    if row:
        out.update(_public(row))
    out["topics_consented"] = await topics_consented(conn, person_id)
    if out.get("redirect_member_id"):
        out["redirect_name"] = await conn.fetchval("SELECT display_name FROM care_circle_members WHERE id = $1",
                                                   uuid.UUID(str(out["redirect_member_id"])))
    else:
        out["redirect_name"] = None
    return out


async def topics_consented(conn, person_id) -> bool:
    row = await conn.fetchrow(
        "SELECT granted, revoked_at FROM care_consents WHERE person_id = $1 AND kind = 'topics' "
        "ORDER BY recorded_at DESC LIMIT 1", person_id)
    return bool(row and row["granted"] and row["revoked_at"] is None)


async def put_profile(principal: Principal, person_id: uuid.UUID, body: dict, *, role: str) -> Optional[dict]:
    """Replace the profile. Topic lists are stored only once the 'topics' consent is recorded;
    without it they are cleared, so nothing is used that the person did not agree to."""
    async with workspace_connection(principal) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            return None
        consented = await topics_consented(conn, person_id)
        avoid = [t.strip()[:60] for t in (body.get("topics_avoid") or []) if t.strip()][:20] if consented else []
        familiar = [t.strip()[:60] for t in (body.get("familiar_topics") or []) if t.strip()][:20] if consented else []
        member = body.get("redirect_member_id")
        if member is not None:
            ok = await conn.fetchval("SELECT 1 FROM care_circle_members WHERE id = $1 AND person_id = $2", member, person_id)
            if not ok:
                return None
        await conn.execute(
            """
            INSERT INTO care_communication_profiles
                (person_id, pace, prompting_level, voice_on, touch_on, vibration_on, topics_avoid, familiar_topics,
                 redirect_member_id, updated_by, updated_by_role, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
            ON CONFLICT (person_id) DO UPDATE SET
                pace = EXCLUDED.pace, prompting_level = EXCLUDED.prompting_level, voice_on = EXCLUDED.voice_on,
                touch_on = EXCLUDED.touch_on, vibration_on = EXCLUDED.vibration_on, topics_avoid = EXCLUDED.topics_avoid,
                familiar_topics = EXCLUDED.familiar_topics, redirect_member_id = EXCLUDED.redirect_member_id,
                updated_by = EXCLUDED.updated_by, updated_by_role = EXCLUDED.updated_by_role, updated_at = NOW()
            """, person_id, body.get("pace", "normal"), body.get("prompting_level", "standard"),
            bool(body.get("voice_on", True)), bool(body.get("touch_on", True)), bool(body.get("vibration_on", True)),
            avoid, familiar, member, principal.user_id, role)
        return await get_profile(conn, person_id)


def watch_profile(profile: dict) -> dict:
    """What the watch applies: the speech rate, how much it prompts, which channels."""
    pace = profile.get("pace", "normal")
    level = profile.get("prompting_level", "standard")
    return {
        "pace": pace, "prompting_level": level,
        "speech_rate": 0.8 if pace == "slow" else 0.9,
        # How long the watch waits before repeating a question, and whether it repeats at all.
        "repeat_after_s": {"light": 90, "standard": 60, "full": 45}[level],
        "repeats": {"light": 0, "standard": 1, "full": 2}[level],
        "voice": bool(profile.get("voice_on", True)), "touch": bool(profile.get("touch_on", True)),
        "vibration": bool(profile.get("vibration_on", True)),
    }


# ── Reminders ────────────────────────────────────────────────────────────────

_AT = re.compile(r"\b(?:at|by)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|o'?clock)?\b", re.I)
_IN = re.compile(r"\bin\s+(\d{1,3}|an?|half an?)\s*(minutes?|mins?|hours?|hrs?|hour)\b", re.I)
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10, "eleven": 11, "twelve": 12}
_AT_WORDS = re.compile(r"\b(?:at|by)\s+(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b(?:\s*(o'?clock))?", re.I)
_PART = re.compile(r"\b(this evening|tonight|this afternoon|tomorrow morning|tomorrow|in the morning|after lunch|at lunch(?:time)?|at bedtime|later)\b", re.I)


def parse_reminder(text: str, now_local: dt.datetime) -> tuple[str, dt.datetime, str]:
    """What to remind, when (local time), and how the time was read.

    "ring Sravani at three" -> 15:00 today if that is still ahead (afternoon
    preferred for bare small numbers), else tomorrow; "in 20 minutes"; "this
    evening" (18:00); "tomorrow morning" (09:00); nothing said -> in an hour.
    """
    what = (text or "").strip().rstrip(".!")
    due = None
    how = "default (in an hour)"
    m = _IN.search(what)
    if m:
        n = m.group(1).lower()
        count = 0.5 if n.startswith("half") else 1 if n in ("a", "an") else int(n)
        minutes = count * (60 if m.group(2).lower().startswith(("h",)) else 1)
        due = now_local + dt.timedelta(minutes=minutes)
        how = f"in {minutes:g} minutes"
        what = (what[:m.start()] + what[m.end():]).strip()
    if due is None:
        m = _AT.search(what) or _AT_WORDS.search(what)
        if m:
            hour = int(m.group(1)) if m.group(1).isdigit() else _WORDS[m.group(1).lower()]
            minute = int(m.group(2)) if m.re is _AT and m.group(2) else 0
            suffix = (m.group(3) if m.re is _AT else m.group(2)) or ""
            suffix = suffix.lower()
            if "pm" in suffix and hour < 12:
                hour += 12
            elif "am" not in suffix and hour <= 7:
                hour += 12  # "at three" from an older person at home is the afternoon
            due = now_local.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
            if due <= now_local:
                due += dt.timedelta(days=1)
            how = f"at {due.strftime('%H:%M')}"
            what = (what[:m.start()] + what[m.end():]).strip()
    if due is None:
        m = _PART.search(what)
        if m:
            part = m.group(1).lower()
            hour = {"this evening": 18, "tonight": 20, "this afternoon": 15, "tomorrow morning": 9, "tomorrow": 9,
                    "in the morning": 9, "after lunch": 14, "at lunch": 12, "at lunchtime": 12, "at bedtime": 21, "later": None}[part]
            if hour is None:
                due = now_local + dt.timedelta(hours=2)
            else:
                due = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
                if part.startswith("tomorrow") or due <= now_local:
                    due += dt.timedelta(days=1)
            how = part
            what = (what[:m.start()] + what[m.end():]).strip()
    if due is None:
        due = now_local + dt.timedelta(hours=1)
    what = re.sub(r"\s+", " ", what).strip(" ,.")
    what = re.sub(r"^(to|about|that)\s+", "", what, flags=re.I) or "that"
    return what[:200], due, how


async def create_reminder(principal: Principal, person_id: uuid.UUID, body: dict, *, source: str) -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            return None
        row = await conn.fetchrow(
            "INSERT INTO care_reminders (person_id, kind, text, medication_id, due_at, source, created_by) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
            person_id, body.get("kind", "reminder"), (body.get("text") or "")[:200], body.get("medication_id"),
            body["due_at"], source, principal.user_id)
        return _public(row)


async def list_reminders(principal: Principal, person_id: uuid.UUID, *, limit: int = 100, open_only: bool = False) -> list[dict]:
    async with workspace_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT * FROM care_reminders WHERE person_id = $1 AND ($2 = FALSE OR status IN ('pending', 'asked', 'confirming', 'snoozed')) "
            "ORDER BY due_at DESC LIMIT $3", person_id, open_only, limit)
        return [_public(r) for r in rows]


async def act_on_reminder(principal: Principal, reminder_id: uuid.UUID, action: str, *, minutes: int = SNOOZE_MINUTES,
                          now: Optional[dt.datetime] = None) -> Optional[dict]:
    """From the phone or the dashboard: confirm, snooze, skip or cancel. Idempotent on a finished reminder."""
    now = now or dt.datetime.now(dt.timezone.utc)
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow("SELECT * FROM care_reminders WHERE id = $1", reminder_id)
        if row is None:
            return None
        if row["status"] in ("confirmed", "skipped", "cancelled", "missed"):
            return _public(row)
        # Who confirmed is read from the verified caller, never assumed: the
        # person themself, family, or a carer (care/repository.provenance_of).
        from . import repository as repo
        via, source = await repo.provenance_of(conn, principal, row["person_id"])
        if action == "confirm":
            row = await _finish(conn, dict(row), "confirmed", now, via=via, source=source, recorded_by=principal.user_id)
        elif action == "snooze":
            row = await conn.fetchrow(
                "UPDATE care_reminders SET status = 'snoozed', snoozed_until = $2, updated_at = NOW() WHERE id = $1 RETURNING *",
                reminder_id, now + dt.timedelta(minutes=max(1, min(minutes, 240))))
        elif action == "skip":
            row = await _finish(conn, dict(row), "skipped", now, via=via, source=source, recorded_by=principal.user_id)
        elif action == "cancel":
            row = await conn.fetchrow(
                "UPDATE care_reminders SET status = 'cancelled', done_at = $2, updated_at = NOW() WHERE id = $1 RETURNING *",
                reminder_id, now)
        else:
            raise ValueError("action must be confirm, snooze, skip or cancel")
        return _public(row)


async def _finish(conn, reminder: dict, status: str, now: dt.datetime, *, via: str, source: str = "self_report",
                  recorded_by=None, checkin_id=None) -> dict:
    row = await conn.fetchrow(
        "UPDATE care_reminders SET status = $2, done_at = $3, updated_at = NOW() WHERE id = $1 RETURNING *",
        reminder["id"], status, now)
    if reminder["kind"] == "medication" and reminder.get("medication_id") and status in ("confirmed", "skipped"):
        # The dose is the reminder's own medication and due time: bound when the
        # reminder was made, never guessed from words or the nearest schedule.
        from . import repository as repo
        try:
            await repo.write_medication_event(
                conn, medication_id=reminder["medication_id"], person_id=reminder["person_id"],
                due_at=reminder["due_at"], status="taken" if status == "confirmed" else "skipped",
                via=via, source=source, recorded_by=recorded_by, checkin_id=checkin_id)
        except repo.DoseConflict:
            # Already settled differently by someone else: left as it is, never overwritten.
            logger.warning("care reminder %s: dose already recorded differently; not overwritten", reminder["id"])
    return dict(row)


def question_for(reminder: dict, name: str, medication_name: str = "") -> str:
    kind = reminder["kind"]
    if kind == "medication":
        what = medication_name or reminder["text"] or "your tablets"
        if reminder["status"] == "confirming":
            return f"{name}, you've taken {what}. Is that right?"
        return f"{name}, it's time for {what}. Have you taken it? Say yes, later, or not today."
    if kind == "hydration":
        return f"{name}, time for a drink of water. Will you have one now?"
    return f"{name}, you asked me to remind you: {reminder['text']}. Shall I tick that off, or remind you later?"


async def due_to_checkins(conn, person: dict, settings: dict, now: dt.datetime) -> int:
    """Turn due reminders into check-ins the watch will ask, and plant the next
    hydration prompt when the setting is on. Returns how many were asked."""
    asked = 0
    name = person.get("preferred_name") or person["display_name"]
    local = rules.local_now(now, person["timezone"])
    quiet = settings and rules.in_quiet_hours(local.time(), settings["quiet_start"], settings["quiet_end"])
    every = int((settings or {}).get("hydration_every_minutes") or 0)
    if every and not quiet:
        pending = await conn.fetchval(
            "SELECT count(*) FROM care_reminders WHERE person_id = $1 AND kind = 'hydration' "
            "AND status IN ('pending', 'asked', 'confirming', 'snoozed')", person["id"])
        last = await conn.fetchval(
            "SELECT max(coalesce(done_at, due_at)) FROM care_reminders WHERE person_id = $1 AND kind = 'hydration'", person["id"])
        if not pending:
            # The next prompt is planted as soon as the last one is done, due `every` minutes after it.
            due_at = now if last is None else max(now, last + dt.timedelta(minutes=every))
            await conn.execute(
                "INSERT INTO care_reminders (person_id, kind, text, due_at, source) VALUES ($1, 'hydration', 'a drink of water', $2, 'routine')",
                person["id"], due_at)
    if quiet:
        return 0
    due = await conn.fetch(
        "SELECT r.*, m.name AS medication_name FROM care_reminders r LEFT JOIN care_medications m ON m.id = r.medication_id "
        "WHERE r.person_id = $1 AND ((r.status = 'pending' AND r.due_at <= $2) OR (r.status = 'snoozed' AND r.snoozed_until <= $2)) "
        "ORDER BY r.due_at", person["id"], now)
    for r in due:
        reminder = dict(r)
        await _ask(conn, reminder, name, now)
        asked += 1
    return asked


async def _ask(conn, reminder: dict, name: str, now: dt.datetime) -> None:
    kind = "medication" if reminder["kind"] == "medication" else "hydration" if reminder["kind"] == "hydration" else "reminder"
    text = question_for(reminder, name, reminder.get("medication_name") or "")
    # A reminder is asked instead of, not as well as, a scheduled question due at the same time.
    await conn.execute(
        "UPDATE care_checkins SET scheduled_at = scheduled_at + interval '10 minutes' WHERE person_id = $1 AND status = 'scheduled' "
        "AND reminder_id IS NULL AND scheduled_at <= $2", reminder["person_id"], now)
    checkin_id = await conn.fetchval(
        "INSERT INTO care_checkins (person_id, scheduled_at, status, question_kind, question_text, reminder_id) "
        "VALUES ($1, $2, 'scheduled', $3, $4, $5) RETURNING id", reminder["person_id"], now, kind, text, reminder["id"])
    await conn.execute(
        "UPDATE care_reminders SET status = CASE WHEN status = 'confirming' THEN 'confirming' ELSE 'asked' END, "
        "asked_count = asked_count + 1, updated_at = NOW() WHERE id = $1", reminder["id"])
    logger.info("care reminder %s asked as check-in %s", reminder["id"], checkin_id)


_LATER = re.compile(r"\b(later|not now|in a (bit|minute|while|moment)|give me|ask me again|snooze|five minutes|ten minutes|after)\b", re.I)
_SKIP = re.compile(r"\b(not today|skip( it)?|no thank|don'?t need|won'?t|leave it|forget it|no$)\b", re.I)
_YES = re.compile(r"\b(yes|yeah|yep|yes please|i have|i did|i will|taken|took|done|all done|just now|ok|okay|tick it|that'?s right|correct|right)\b", re.I)


def read_reminder_answer(text: str, kind: str = "medication", *, subject: Optional[str] = None,
                         other_medications: tuple = ()) -> str:
    """yes | later | skip | unclear, from the person's words.

    A medication reminder is read by the one conservative dose reader
    (`checkins.read_dose_answer`): "okay", "I will", "I think so" are never a
    dose. Other reminders ("ring Sravani", a drink of water) keep the plainer
    reading, where "okay" or "I will" is a fair answer. The default is the
    medication reading, so a caller that forgets the kind is safe, not lax."""
    if kind == "medication":
        return rules.read_dose_answer(text, subject=subject, other_medications=other_medications)
    t = (text or "").strip().lower()
    if not t:
        return "unclear"
    if _LATER.search(t) and not re.search(r"\b(taken|took|done|yes)\b", t):
        return "later"
    if _SKIP.search(t) and not _YES.search(t):
        return "skip"
    if re.search(r"\b(no|not yet|haven'?t|didn'?t|nope)\b", t) and not _YES.search(t):
        return "later" if re.search(r"\b(not yet|haven'?t|didn'?t)\b", t) else "skip"
    if _YES.search(t):
        return "yes"
    return "unclear"


#: A reminder's button on the wrist, and what it says. A button is as clear as words get, but a
#: dose still takes two: "Done" on a medication asks once more before anything is written down.
CHOICE_SAID = {"done": "yes", "later": "later", "skip": "skip"}


async def apply_answer(conn, checkin: dict, text: str, now: dt.datetime, *, choice: Optional[str] = None) -> Optional[dict]:
    """The person answered a reminder's question, in words or with a button. Two steps for a dose."""
    reminder = await conn.fetchrow("SELECT * FROM care_reminders WHERE id = $1", checkin["reminder_id"])
    if reminder is None:
        return None
    reminder = dict(reminder)
    subject, others = None, ()
    if reminder["kind"] == "medication":
        meds = await conn.fetch("SELECT id, name FROM care_medications WHERE person_id = $1 AND active", reminder["person_id"])
        subject = next((m["name"] for m in meds if reminder.get("medication_id") and m["id"] == reminder["medication_id"]), None)
        others = tuple(m["name"] for m in meds)
    said = CHOICE_SAID[choice] if choice else read_reminder_answer(
        text, reminder["kind"], subject=subject or reminder.get("text"), other_medications=others)
    if reminder["status"] in ("confirmed", "skipped", "cancelled", "missed"):
        return {"reminder": _public(reminder), "said": said, "next": "nothing"}
    nxt = "nothing"
    if said == "yes":
        if reminder["kind"] == "medication" and reminder["status"] != "confirming":
            # Step one of two: ask again, in different words, before a dose is written down as taken.
            reminder["status"] = "confirming"
            await conn.execute("UPDATE care_reminders SET status = 'confirming', updated_at = NOW() WHERE id = $1", reminder["id"])
            name = await conn.fetchval("SELECT coalesce(nullif(preferred_name, ''), display_name) FROM care_people WHERE id = $1",
                                       reminder["person_id"])
            med = await conn.fetchval("SELECT name FROM care_medications WHERE id = $1", reminder["medication_id"]) if reminder.get("medication_id") else ""
            reminder["medication_name"] = med or ""
            await _ask(conn, reminder, name, now)
            nxt = "confirm"
        else:
            reminder = await _finish(conn, reminder, "confirmed", now, via="checkin", checkin_id=checkin["id"])
            nxt = "done"
    elif said == "later":
        await conn.execute(
            "UPDATE care_reminders SET status = 'snoozed', snoozed_until = $2, updated_at = NOW() WHERE id = $1",
            reminder["id"], now + dt.timedelta(minutes=SNOOZE_MINUTES))
        reminder["status"], reminder["snoozed_until"] = "snoozed", now + dt.timedelta(minutes=SNOOZE_MINUTES)
        nxt = "snoozed"
    elif said == "skip":
        reminder = await _finish(conn, reminder, "skipped", now, via="checkin", checkin_id=checkin["id"])
        nxt = "skipped"
    else:
        # Unclear: for a dose in the confirming step, nothing is written down; it is asked again later.
        await conn.execute(
            "UPDATE care_reminders SET status = 'snoozed', snoozed_until = $2, updated_at = NOW() WHERE id = $1",
            reminder["id"], now + dt.timedelta(minutes=SNOOZE_MINUTES))
        reminder["status"] = "snoozed"
        nxt = "snoozed"
    return {"reminder": _public(reminder), "said": said, "next": nxt}


async def missed(conn, checkin: dict, now: dt.datetime) -> None:
    """Nobody answered the reminder's question: asked again once, then missed."""
    reminder = await conn.fetchrow("SELECT * FROM care_reminders WHERE id = $1", checkin["reminder_id"])
    if reminder is None or reminder["status"] not in ("asked", "confirming"):
        return
    if reminder["asked_count"] < MAX_ASKS:
        await conn.execute(
            "UPDATE care_reminders SET status = CASE WHEN status = 'confirming' THEN 'snoozed' ELSE 'snoozed' END, "
            "snoozed_until = $2, updated_at = NOW() WHERE id = $1", reminder["id"], now + dt.timedelta(minutes=SNOOZE_MINUTES))
    else:
        await conn.execute("UPDATE care_reminders SET status = 'missed', done_at = $2, updated_at = NOW() WHERE id = $1",
                           reminder["id"], now)
        if reminder["kind"] == "medication" and reminder["medication_id"]:
            await conn.execute(
                """
                INSERT INTO care_medication_events (medication_id, person_id, checkin_id, due_at, status, confirmed_via, source)
                VALUES ($1, $2, $3, $4, 'unknown', 'none', 'system')
                ON CONFLICT (medication_id, due_at) DO NOTHING
                """, reminder["medication_id"], reminder["person_id"], checkin["id"], reminder["due_at"])


def confirmation_line(result: dict, name: str) -> str:
    """What IRA says back after a reminder answer."""
    nxt = result["next"]
    if nxt == "confirm":
        return "Thank you. One more check in a moment."
    if nxt == "done":
        return f"Thank you, {name}. I've written that down."
    if nxt == "snoozed":
        return f"All right, {name}. I'll ask again in {SNOOZE_MINUTES} minutes."
    if nxt == "skipped":
        return f"All right, {name}. I'll leave it."
    return "Thank you."


# ── What IRA remembers: the person's own hand ────────────────────────────────

async def edit_memory(principal: Principal, person_id: uuid.UUID, day: dt.date, summary: str, *, role: str) -> Optional[dict]:
    async with workspace_connection(principal) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO care_conversation_memory (person_id, day, summary, exchanges, updated_at, edited_at, edited_by)
            VALUES ($1, $2, $3, 0, NOW(), NOW(), $4)
            ON CONFLICT (person_id, day) DO UPDATE SET summary = EXCLUDED.summary, updated_at = NOW(), edited_at = NOW(),
                                                      edited_by = EXCLUDED.edited_by
            RETURNING day, summary, exchanges, updated_at, edited_at, edited_by
            """, person_id, day, summary.strip()[:4000], role)
        await _rebuild_reports(conn, person_id, day)
        return _public(row)


async def forget_day(principal: Principal, person_id: uuid.UUID, day: dt.date) -> bool:
    async with workspace_connection(principal) as conn:
        # One transaction (workspace_connection): the forget and the rebuilt reports commit together.
        res = await conn.execute("DELETE FROM care_conversation_memory WHERE person_id = $1 AND day = $2", person_id, day)
        forgotten = res.endswith("1")
        if forgotten:
            await _rebuild_reports(conn, person_id, day)
        return forgotten


async def _rebuild_reports(conn, person_id: uuid.UUID, day: dt.date) -> None:
    """A forgotten or corrected day must not live on in a stored report (D-106)."""
    person = await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", person_id)
    if person is not None:
        await reports.rebuild_covering(conn, dict(person), day)


# ── Concern summaries: labelled, linked to their source ──────────────────────

def concern_details(summary: str, *, person_name: str, transcript_id, heard_in: str) -> dict:
    """The alert's details: a summary by IRA, never a quote; the quote stays in the
    transcript and is shown only to a role with the person's consent."""
    return {"summary": summary, "summary_by": "IRA", "said_by": person_name, "heard_in": heard_in,
            "source": {"kind": "transcript", "id": str(transcript_id) if transcript_id else None}}


def role_for(principal: Principal, members: list[dict]) -> str:
    """person | representative | carer | admin, for the record of who edited."""
    mine = [m for m in members if m.get("user_id") == principal.user_id]
    if any(str(m.get("relationship", "")).lower() == "self" for m in mine):
        return "person"
    if any(m.get("role") == "family" for m in mine):
        return "representative"
    if mine:
        return "carer"
    return "admin"


__all__ = ["PACES", "PROMPTING_LEVELS", "REMINDER_KINDS", "REMINDER_STATUSES", "SNOOZE_MINUTES", "get_profile", "put_profile",
           "watch_profile", "parse_reminder", "create_reminder", "list_reminders", "act_on_reminder", "question_for",
           "due_to_checkins", "read_reminder_answer", "apply_answer", "missed", "confirmation_line", "edit_memory",
           "forget_day", "concern_details", "role_for", "topics_consented"]
