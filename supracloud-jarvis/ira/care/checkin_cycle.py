"""The check-in cycle as a background job, and the watch's side of it.

Every minute (`worker/scheduler.py`, job `care_checkins`), for every active
tenant, for every workspace with people in it:

* a person with no open check-in gets the next one scheduled, 15 to 20
  minutes on (the person's own settings), pushed past quiet hours;
* a scheduled check-in that fell due inside quiet hours is marked
  `skipped_quiet` and the next one is scheduled for after the quiet window;
* a check-in the watch asked and nobody answered is marked `missed` once the
  repeat and the sensor-check window have passed (plus a minute's grace);
* a check-in scheduled but never picked up by the watch (offline, flat, not
  worn) is marked `missed` once a whole check-in window has passed.

The watch drives the conversation itself: it asks for what is due
(`GET /care/devices/me/checkins/due`), which composes the question and marks
the check-in `asked`; it reports the louder repeat; and it posts the answer or
that nobody answered. `record_answer` stores the words, a one-line summary,
transcript rows for both sides, and a medication event when the question was
about a dose.

Deciding what a miss MEANS (sensors, escalation) is phase 4. This module only
records what happened, accurately.
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import uuid
from dataclasses import dataclass, replace
from typing import Optional

from core.identity.background import background_principal
from core.identity.db_context import scheduler_connection, workspace_connection
from core.identity.principal import Principal

from . import checkins as rules
from . import repository as repo

logger = logging.getLogger("ira.care.checkins")

JOB = "care_checkins"

#: Grace after the repeat and sensor-check windows before an asked check-in
#: counts as missed. Seconds.
MISS_GRACE_S = 60


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def job_principal(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> Principal:
    return replace(background_principal(tenant_id, job=JOB), workspace_id=workspace_id)


@dataclass
class CycleOutcome:
    tenants: int = 0
    workspaces: int = 0
    people: int = 0
    scheduled: int = 0
    skipped_quiet: int = 0
    missed: int = 0
    errors: int = 0


async def run_cycle_once(now: Optional[dt.datetime] = None, *, rng: Optional[random.Random] = None) -> CycleOutcome:
    """One pass over every tenant. Never raises for one person's trouble."""
    from utils.db import get_pool

    now = now or _now()
    rng = rng or random.Random()
    outcome = CycleOutcome()
    async with get_pool().acquire() as conn:
        tenant_ids = [r["id"] for r in await conn.fetch(
            "SELECT id FROM tenants WHERE status = 'active' ORDER BY id")]
    for tenant_id in tenant_ids:
        outcome.tenants += 1
        try:
            async with scheduler_connection(tenant_id) as conn:
                workspaces = [r["id"] for r in await conn.fetch(
                    "SELECT id FROM workspaces WHERE tenant_id = $1 AND status = 'active' ORDER BY id",
                    tenant_id)]
        except Exception:  # noqa: BLE001
            logger.exception("care check-ins: could not list workspaces for a tenant")
            outcome.errors += 1
            continue
        for workspace_id in workspaces:
            principal = job_principal(tenant_id, workspace_id)
            try:
                await run_workspace(principal, now, outcome, rng=rng)
            except Exception:  # noqa: BLE001
                logger.exception("care check-ins: workspace pass failed")
                outcome.errors += 1
    return outcome


async def run_workspace(principal: Principal, now: dt.datetime, outcome: Optional[CycleOutcome] = None,
                        *, rng: Optional[random.Random] = None) -> CycleOutcome:
    """The pass for one workspace, as its job principal."""
    outcome = outcome or CycleOutcome()
    rng = rng or random.Random()
    async with workspace_connection(principal) as conn:
        people = await conn.fetch("SELECT * FROM care_people WHERE status = 'active'")
        if not people:
            return outcome
        outcome.workspaces += 1
        for person in people:
            outcome.people += 1
            settings = await conn.fetchrow(
                "SELECT * FROM care_safety_settings WHERE person_id = $1", person["id"])
            if settings is None:
                settings = await conn.fetchrow(
                    "INSERT INTO care_safety_settings (person_id) VALUES ($1) RETURNING *", person["id"])
            await _advance_person(conn, person, settings, now, outcome, rng)
    return outcome


async def _advance_person(conn, person, settings, now, outcome: CycleOutcome, rng) -> None:
    tz = person["timezone"]
    open_rows = await conn.fetch(
        "SELECT * FROM care_checkins WHERE person_id = $1 AND status IN ('scheduled', 'asked', 'repeated') "
        "ORDER BY scheduled_at", person["id"])
    window_s = int(settings["checkin_max_minutes"]) * 60
    for c in open_rows:
        local = rules.local_now(now, tz)
        if c["status"] == "scheduled":
            if c["scheduled_at"] > now:
                continue
            if rules.in_quiet_hours(local.time(), settings["quiet_start"], settings["quiet_end"]):
                await conn.execute("UPDATE care_checkins SET status = 'skipped_quiet' WHERE id = $1", c["id"])
                outcome.skipped_quiet += 1
            elif (now - c["scheduled_at"]).total_seconds() > window_s + MISS_GRACE_S:
                # Never picked up by the watch: offline, flat, or not worn.
                await conn.execute(
                    "UPDATE care_checkins SET status = 'missed', answer_summary = $2 WHERE id = $1",
                    c["id"], "Not delivered: the watch did not collect this check-in.")
                outcome.missed += 1
            else:
                continue  # due; the watch will collect it
        else:  # asked or repeated
            deadline_s = int(settings["repeat_after_s"]) + int(settings["sensor_check_after_s"]) + MISS_GRACE_S
            asked_at = c["asked_at"] or c["scheduled_at"]
            if (now - asked_at).total_seconds() > deadline_s:
                await conn.execute(
                    "UPDATE care_checkins SET status = 'missed', answer_summary = $2 WHERE id = $1",
                    c["id"], "No answer.")
                outcome.missed += 1
            else:
                continue
    still_open = await conn.fetchval(
        "SELECT count(*) FROM care_checkins WHERE person_id = $1 AND status IN ('scheduled', 'asked', 'repeated')",
        person["id"])
    if still_open == 0:
        when = rules.next_checkin_at(now, settings, tz, rng)
        await conn.execute(
            "INSERT INTO care_checkins (person_id, scheduled_at, status, question_kind) "
            "VALUES ($1, $2, 'scheduled', 'wellbeing')", person["id"], when)
        outcome.scheduled += 1


# ── The watch's side ─────────────────────────────────────────────────────────

async def due_checkin(principal: Principal, device: dict, now: Optional[dt.datetime] = None,
                      *, rng: Optional[random.Random] = None) -> Optional[dict]:
    """The check-in the watch should ask now, with its question composed, or
    None. Composing happens once: a second call returns the same words."""
    now = now or _now()
    rng = rng or random.Random()
    person_id = device["person_id"]
    async with workspace_connection(principal) as conn:
        person = await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", person_id)
        if person is None or person["status"] != "active":
            return None
        settings = await conn.fetchrow("SELECT * FROM care_safety_settings WHERE person_id = $1", person_id)
        row = await conn.fetchrow(
            "SELECT * FROM care_checkins WHERE person_id = $1 AND status IN ('scheduled', 'asked', 'repeated') "
            "AND scheduled_at <= $2 ORDER BY scheduled_at LIMIT 1", person_id, now)
        if row is None:
            return None
        if row["status"] != "scheduled":
            return _public_checkin(dict(row))
        local = rules.local_now(now, person["timezone"])
        if settings and rules.in_quiet_hours(local.time(), settings["quiet_start"], settings["quiet_end"]):
            return None
        routines = await conn.fetch(
            "SELECT * FROM care_routines WHERE person_id = $1 AND active", person_id)
        medications = await conn.fetch(
            "SELECT * FROM care_medications WHERE person_id = $1 AND active", person_id)
        since = now - dt.timedelta(hours=36)
        events = await conn.fetch(
            "SELECT * FROM care_medication_events WHERE person_id = $1 AND due_at >= $2", person_id, since)
        recent = await conn.fetch(
            "SELECT * FROM care_checkins WHERE person_id = $1 AND id <> $2 ORDER BY scheduled_at DESC LIMIT 12",
            person_id, row["id"])
        choice = rules.choose_question(local, routines=routines, medications=medications,
                                       medication_events=events, recent=recent, rng=rng)
        text = rules.question_text(choice, person["preferred_name"] or person["display_name"],
                                   time_of_day=local.time(), rng=rng)
        text = await _maybe_rephrase(text, choice, person)
        row = await conn.fetchrow(
            "UPDATE care_checkins SET status = 'asked', asked_at = $2, question_kind = $3, question_text = $4 "
            "WHERE id = $1 RETURNING *", row["id"], now, choice.kind, text)
        await conn.execute(
            "INSERT INTO care_transcripts (person_id, checkin_id, speaker, text) VALUES ($1, $2, 'companion', $3)",
            person_id, row["id"], text)
        out = _public_checkin(dict(row))
        out["subject_id"] = choice.subject_id
        return out


def _public_checkin(row: dict) -> dict:
    return {k: row[k] for k in ("id", "person_id", "scheduled_at", "asked_at", "repeated_at", "status",
                                "question_kind", "question_text")}


async def _maybe_rephrase(text: str, choice: rules.QuestionChoice, person) -> str:
    """Let a language model make the question more personal, when switched on.

    Off by default (`IRA_CARE_MODEL_PHRASING`), and on failure the template is
    used unchanged, so the cycle never depends on a model being available.
    """
    import os
    if (os.getenv("IRA_CARE_MODEL_PHRASING") or "").strip().lower() not in ("1", "true", "yes", "on"):
        return text
    try:
        from utils.llm import chat_complete
        name = person["preferred_name"] or person["display_name"]
        prompt = (
            "You help an older person who lives alone. Rephrase the check-in question below "
            "so it sounds like a kind, patient companion speaking aloud. Rules: at most two "
            "short sentences, one question only, use the name given, plain everyday words, "
            "no medical terms, no advice, no diagnosis, nothing new that was not in the "
            "original. Reply with the words only.\n\n"
            f"Name: {name}\nQuestion: {text}")
        out = await chat_complete([{"role": "user", "content": prompt}], max_tokens=60, temperature=0.4)
        out = (out or "").strip().strip('"')
        if 5 <= len(out) <= 200 and out.count("?") <= 1:
            return out
    except Exception as exc:  # noqa: BLE001
        logger.info("care check-ins: model phrasing unavailable (%s); using the template", exc)
    return text


async def mark_repeated(principal: Principal, device: dict, checkin_id: uuid.UUID,
                        now: Optional[dt.datetime] = None) -> Optional[dict]:
    now = now or _now()
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow(
            "UPDATE care_checkins SET status = 'repeated', repeated_at = COALESCE(repeated_at, $3) "
            "WHERE id = $1 AND person_id = $2 AND status = 'asked' RETURNING *",
            checkin_id, device["person_id"], now)
    return _public_checkin(dict(row)) if row else None


async def record_answer(principal: Principal, device: dict, checkin_id: uuid.UUID, text: str, *,
                        via: str = "voice", now: Optional[dt.datetime] = None) -> Optional[dict]:
    """Store the answer: words, summary, transcript, and a medication event if
    the question was about a dose. Returns the check-in, with what was read
    from the answer, or None if there is no such open check-in."""
    now = now or _now()
    person_id = device["person_id"]
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM care_checkins WHERE id = $1 AND person_id = $2 AND status IN ('asked', 'repeated', 'scheduled')",
            checkin_id, person_id)
        if row is None:
            return None
        subject = None
        med = None
        if row["question_kind"] == "medication":
            med = await _medication_asked_about(conn, person_id, row["question_text"])
            subject = med["name"] if med else None
        reading = rules.summarise_answer(row["question_kind"], text, subject=subject)
        row = await conn.fetchrow(
            "UPDATE care_checkins SET status = 'answered', answered_at = $2, answer_text = $3, "
            "answer_summary = $4, answered_via = $5 WHERE id = $1 RETURNING *",
            checkin_id, now, (text or "")[:4000], reading.summary, via)
        if text:
            await conn.execute(
                "INSERT INTO care_transcripts (person_id, checkin_id, speaker, text) VALUES ($1, $2, 'person', $3)",
                person_id, checkin_id, text[:8000])
        if med is not None and reading.said in ("yes", "no"):
            due = _nearest_due(now, med, rules.local_now(now, (await conn.fetchrow(
                "SELECT timezone FROM care_people WHERE id = $1", person_id))["timezone"]).tzinfo)
            if due is not None:
                await conn.execute(
                    """
                    INSERT INTO care_medication_events (medication_id, person_id, checkin_id, due_at, status, confirmed_via)
                    VALUES ($1, $2, $3, $4, $5, 'checkin')
                    ON CONFLICT (medication_id, due_at) DO UPDATE
                       SET status = EXCLUDED.status, confirmed_via = 'checkin', checkin_id = EXCLUDED.checkin_id,
                           recorded_at = NOW()
                    """, med["id"], person_id, checkin_id, due,
                    "taken" if reading.said == "yes" else "missed")
    await _remember(principal, row, reading)
    out = _public_checkin(dict(row))
    out.update({"answer_summary": reading.summary, "said": reading.said, "signals": list(reading.signals),
                "asked_for_help": reading.asked_for_help})
    return out


async def _medication_asked_about(conn, person_id, question_text: str):
    meds = await conn.fetch("SELECT * FROM care_medications WHERE person_id = $1 AND active", person_id)
    lowered = (question_text or "").lower()
    for m in meds:
        if m["name"].lower() in lowered:
            return m
    return meds[0] if len(meds) == 1 else None


def _nearest_due(now: dt.datetime, med, tz) -> Optional[dt.datetime]:
    local = now.astimezone(tz)
    best, best_gap = None, None
    for t in med["times"] or ():
        candidate = local.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        for shift in (-1, 0, 1):
            c = candidate + dt.timedelta(days=shift)
            gap = abs((c - local).total_seconds())
            if best_gap is None or gap < best_gap:
                best, best_gap = c, gap
    if best is None or best_gap > 6 * 3600:
        return None
    return best.astimezone(dt.timezone.utc)


async def _remember(principal: Principal, row, reading: rules.AnswerReading) -> None:
    """Best effort: a short note into the memory store, so later questions can
    refer back. Failure (no embedder, no model) is logged, never raised."""
    try:
        from memory.store import remember
        note = f"Check-in ({row['question_kind']}): {reading.summary}"
        await remember(principal, note, kind="care_checkin")
    except Exception as exc:  # noqa: BLE001
        logger.debug("care check-ins: memory write skipped (%s)", exc)


async def mark_missed(principal: Principal, device: dict, checkin_id: uuid.UUID,
                      now: Optional[dt.datetime] = None) -> Optional[dict]:
    """The watch says nobody answered after the repeat. Recorded as missed;
    what that means is decided by the monitoring rules (phase 4)."""
    now = now or _now()
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow(
            "UPDATE care_checkins SET status = 'missed', answer_summary = 'No answer.' "
            "WHERE id = $1 AND person_id = $2 AND status IN ('asked', 'repeated') RETURNING *",
            checkin_id, device["person_id"])
    return _public_checkin(dict(row)) if row else None


async def ask_now(principal: Principal, person_id: uuid.UUID, *, kind: str = "wellbeing",
                  custom_text: str = "", now: Optional[dt.datetime] = None) -> Optional[dict]:
    """A family member or carer asks for a check-in straight away."""
    now = now or _now()
    async with workspace_connection(principal) as conn:
        if not await conn.fetchval("SELECT 1 FROM care_people WHERE id = $1", person_id):
            return None
        await conn.execute(
            "UPDATE care_checkins SET status = 'cancelled' WHERE person_id = $1 AND status = 'scheduled'",
            person_id)
        row = await conn.fetchrow(
            "INSERT INTO care_checkins (person_id, scheduled_at, status, question_kind, question_text) "
            "VALUES ($1, $2, 'scheduled', $3, $4) RETURNING *",
            person_id, now, kind, custom_text[:400])
    return _public_checkin(dict(row))
