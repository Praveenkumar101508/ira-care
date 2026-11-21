"""Talk to IRA, against the database: the person's context, the exchange, the
transcript rows (under the existing consent rules), the rolling memory, and
what happens when the person says something that needs a check right now."""
from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import AsyncIterator, Optional

from core.identity.db_context import workspace_connection
from core.identity.principal import Principal

from . import askfirst, conversation as conv
from . import checkins as rules
from .monitoring import SOS, Observation

CONCERNING = "concerning_phrase"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def carer_name(conn, person_id) -> str:
    row = await conn.fetchrow(
        "SELECT display_name FROM care_circle_members WHERE person_id = $1 AND active "
        "ORDER BY (role = 'carer') DESC, tier, priority LIMIT 1", person_id)
    return row["display_name"] if row else "your carer"


async def build_context(conn, person: dict, now: dt.datetime, *, for_talk: bool = False) -> conv.Context:
    """Name, routines, medication due, earlier answers today, the rolling summary."""
    tz_now = rules.local_now(now, person["timezone"])
    routines = [f"{r['name']}" + (f" at {r['time_of_day'].strftime('%H:%M')}" if r["time_of_day"] else "")
                for r in await conn.fetch(
                    "SELECT name, time_of_day FROM care_routines WHERE person_id = $1 AND active ORDER BY time_of_day NULLS LAST",
                    person["id"])]
    meds = await conn.fetch("SELECT * FROM care_medications WHERE person_id = $1 AND active", person["id"])
    due_now = [m["name"] for m in meds if any(abs((tz_now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0) - tz_now)
                                                   .total_seconds()) <= 3600 for t in (m["times"] or ()))]
    day_start = tz_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc)
    earlier = [r["answer_summary"] for r in await conn.fetch(
        "SELECT answer_summary FROM care_checkins WHERE person_id = $1 AND status = 'answered' AND answered_at >= $2 "
        "ORDER BY answered_at DESC LIMIT 6", person["id"], day_start) if r["answer_summary"]]
    summary = await memory_summary(conn, person["id"], tz_now.date())
    hour = tz_now.hour
    time_of_day = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"
    name = person.get("preferred_name") or person["display_name"]
    base = dict(name=name, language=person.get("language") or conv.DEFAULT_LANGUAGE, time_of_day=time_of_day,
                routines=routines, medication_due=due_now, earlier_today=earlier, summary=summary,
                carer_name=await carer_name(conn, person["id"]))
    if not for_talk:
        return conv.Context(**base)
    contacts = [dict(r) for r in await conn.fetch(
        "SELECT display_name, relationship, phone FROM care_circle_members WHERE person_id = $1 AND active ORDER BY tier, priority",
        person["id"])]
    doses = [f"{r['name']} at {r['due_at'].astimezone(tz_now.tzinfo).strftime('%H:%M')}: {r['status']}" for r in await conn.fetch(
        "SELECT m.name, e.due_at, e.status FROM care_medication_events e JOIN care_medications m ON m.id = e.medication_id "
        "WHERE e.person_id = $1 AND e.due_at >= $2 ORDER BY e.due_at", person["id"], day_start)]
    return conv.TalkContext(**base, contacts=contacts, doses_today=doses)


# ── Memory ───────────────────────────────────────────────────────────────────

async def memory_summary(conn, person_id, day: dt.date) -> str:
    """Today's rolling summary and, briefly, the week before it."""
    rows = await conn.fetch(
        "SELECT day, summary FROM care_conversation_memory WHERE person_id = $1 AND day > $2 AND day <= $3 ORDER BY day",
        person_id, day - dt.timedelta(days=7), day)
    parts = []
    for r in rows:
        if r["summary"]:
            label = "Today" if r["day"] == day else r["day"].strftime("%A")
            parts.append(f"{label}: {r['summary'].replace(chr(10), ' ')}")
    return " | ".join(parts)[:1500]


async def remember(conn, person_id, day: dt.date, notes: list[str]) -> None:
    """Append notes to today's rolling summary. Summaries only, never the words."""
    row = await conn.fetchrow("SELECT summary FROM care_conversation_memory WHERE person_id = $1 AND day = $2", person_id, day)
    rolled = conv.roll_summary(row["summary"] if row else "", notes)
    await conn.execute(
        """
        INSERT INTO care_conversation_memory (person_id, day, summary, exchanges, updated_at)
        VALUES ($1, $2, $3, 1, NOW())
        ON CONFLICT (person_id, day) DO UPDATE SET summary = EXCLUDED.summary, exchanges = care_conversation_memory.exchanges + 1,
                                                  updated_at = NOW()
        """, person_id, day, rolled)


async def memory_for(principal: Principal, person_id: uuid.UUID, days: int = 7) -> list[dict]:
    async with workspace_connection(principal) as conn:
        return [dict(r) for r in await conn.fetch(
            "SELECT day, summary, exchanges, updated_at FROM care_conversation_memory WHERE person_id = $1 "
            "ORDER BY day DESC LIMIT $2", person_id, days)]


# ── The exchange ─────────────────────────────────────────────────────────────

async def concern(conn, person: dict, settings: dict, text_kind: str, now: dt.datetime) -> Optional[dict]:
    """A concerning phrase: ask first, then the escalation chain (D-034)."""
    obs = Observation(CONCERNING, SOS,
                      "The person said something that needs a check right now. The person is being asked if they are OK.",
                      {"heard_in": text_kind})
    return await askfirst.start(conn, person, settings, obs, now)


async def exchange(principal: Principal, person_id: uuid.UUID, text: str, *, source: str,
                   now: Optional[dt.datetime] = None) -> AsyncIterator[dict]:
    """Yields {"sentence": ...} as IRA speaks, then one {"done": true, ...}.
    Stores both sides in the transcript and rolls the memory."""
    now = now or _now()
    text = (text or "").strip()[:2000]
    async with workspace_connection(principal) as conn:
        person = await conn.fetchrow("SELECT * FROM care_people WHERE id = $1 AND status = 'active'", person_id)
        if person is None:
            yield {"done": True, "error": "person not found"}
            return
        person = dict(person)
        settings = dict(await conn.fetchrow("SELECT * FROM care_safety_settings WHERE person_id = $1", person_id) or {})
        ctx = await build_context(conn, person, now, for_talk=True)
        recent = [(r["speaker"], r["text"]) for r in reversed(await conn.fetch(
            "SELECT speaker, text FROM care_transcripts WHERE person_id = $1 AND checkin_id IS NULL "
            "ORDER BY created_at DESC LIMIT 6", person_id))]
        if text:
            await conn.execute(
                "INSERT INTO care_transcripts (person_id, checkin_id, speaker, text) VALUES ($1, NULL, 'person', $2)",
                person_id, text)
    spoken: list[str] = []
    turn = await conv.talk(text, ctx, recent=recent, on_sentence=spoken.append)
    for s in turn.sentences:
        yield {"sentence": s}
    action = dict(turn.action) if turn.action else None
    async with workspace_connection(principal) as conn:
        await conn.execute(
            "INSERT INTO care_transcripts (person_id, checkin_id, speaker, text) VALUES ($1, NULL, 'companion', $2)",
            person_id, " ".join(turn.sentences)[:8000])
        day = rules.local_now(now, person["timezone"]).date()
        notes = [f"Talked ({source}): {_note_for(turn, text)}"]
        if action and action.get("kind") == "reminder":
            notes.append(f"Asked to be reminded about {action['text']}.")
        await remember(conn, person_id, day, notes)
        if turn.needs_help:
            alert = await concern(conn, person, settings, "talk", now)
            action = {"kind": "safety_check", "alert_id": str(alert["id"]) if alert else None}
    yield {"done": True, "action": action, "source": turn.source, "needs_help": turn.needs_help}


def _note_for(turn: conv.Turn, text: str) -> str:
    """One line for the memory: what kind of exchange, never the words."""
    if turn.needs_help:
        return "said something that needed a check."
    if turn.action and turn.action.get("kind") == "call":
        return f"asked to call {turn.action['name']}."
    if turn.source == "rules":
        return "asked about the day, the tablets or a reminder."
    return "had a chat with IRA."


def ndjson(it: AsyncIterator[dict]) -> AsyncIterator[bytes]:
    async def gen():
        async for item in it:
            yield (json.dumps(item) + "\n").encode()
    return gen()
