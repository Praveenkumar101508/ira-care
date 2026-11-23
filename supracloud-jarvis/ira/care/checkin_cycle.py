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
    morning_checks: int = 0
    workspaces: int = 0
    people: int = 0
    scheduled: int = 0
    skipped_quiet: int = 0
    missed: int = 0
    paused_for_visit: int = 0
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
    await _heartbeat("care_checkins", outcome)
    return outcome


async def _heartbeat(job: str, outcome) -> None:
    """Record that this pass ran (care/ops.py). Never raises: the pass's work
    is done, and a failed heartbeat is itself what the operations check sees."""
    try:
        from utils.db import get_pool

        from . import ops
        async with get_pool().acquire() as conn:
            await ops.heartbeat(conn, job, outcome.__dict__ if hasattr(outcome, "__dict__") else None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("care %s: heartbeat not recorded (%s)", job, exc)


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
    # A carer in the room (care/visits.py): IRA does not interrupt the visit with
    # a scheduled question. Due check-ins wait, ten minutes at a time.
    from . import visits
    carer_here = visits.carer_in_the_room(await visits.open_visits(conn, person["id"], now))
    for c in open_rows:
        local = rules.local_now(now, tz)
        if c["status"] == "scheduled":
            if c["scheduled_at"] > now:
                continue
            if carer_here:
                await conn.execute("UPDATE care_checkins SET scheduled_at = $2 WHERE id = $1",
                                   c["id"], now + VISIT_PAUSE)
                outcome.paused_for_visit += 1
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
    await _morning_check(conn, person, settings, now, outcome)
    still_open = await conn.fetchval(
        "SELECT count(*) FROM care_checkins WHERE person_id = $1 AND status IN ('scheduled', 'asked', 'repeated')",
        person["id"])
    if still_open == 0:
        when = rules.next_checkin_at(now, settings, tz, rng)
        await conn.execute(
            "INSERT INTO care_checkins (person_id, scheduled_at, status, question_kind) "
            "VALUES ($1, $2, 'scheduled', 'wellbeing')", person["id"], when)
        outcome.scheduled += 1


#: How far a due check-in moves while a carer is with the person.
VISIT_PAUSE = dt.timedelta(minutes=10)

#: How long after the wake deadline the morning check can still be asked.
MORNING_WINDOW = dt.timedelta(hours=3)


async def _morning_check(conn, person, settings, now, outcome: CycleOutcome) -> None:
    """No movement by the usual wake time plus the grace: ask a morning check-in
    now, once per day. No answer then follows the normal missed-check-in path."""
    from . import continuous
    tz = person["timezone"]
    local = rules.local_now(now, tz)
    wake = settings.get("usual_wake_time")
    if wake is None:
        return
    wake_at = local.replace(hour=wake.hour, minute=wake.minute, second=0, microsecond=0)
    deadline = wake_at + dt.timedelta(minutes=int(settings.get("wake_grace_minutes", 60)))
    if local < deadline or local >= deadline + MORNING_WINDOW:
        return
    if rules.in_quiet_hours(local.time(), settings["quiet_start"], settings["quiet_end"]):
        return
    wake_utc = wake_at.astimezone(dt.timezone.utc)
    asked_today = await conn.fetchval(
        "SELECT count(*) FROM care_checkins WHERE person_id = $1 AND scheduled_at >= $2 "
        "AND status NOT IN ('scheduled', 'skipped_quiet', 'cancelled')", person["id"], wake_utc)
    if asked_today:
        return
    readings = [dict(r) for r in await conn.fetch(
        "SELECT kind, value, recorded_at FROM care_vitals WHERE person_id = $1 AND kind IN ('steps', 'movement') "
        "AND recorded_at >= $2", person["id"], wake_utc)]
    moved = continuous.movement_since(readings, wake_utc)
    if moved is None:
        return  # no report since waking: the watch is silent, and the no-sync rule covers that
    if moved > int(settings["movement_floor_steps"]):
        return
    await conn.execute(
        "UPDATE care_checkins SET status = 'cancelled' WHERE person_id = $1 AND status = 'scheduled'", person["id"])
    await conn.execute(
        "INSERT INTO care_checkins (person_id, scheduled_at, status, question_kind) VALUES ($1, $2, 'scheduled', 'morning')",
        person["id"], now)
    outcome.morning_checks += 1


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
            "AND scheduled_at <= $2 ORDER BY (question_kind = 'safety') DESC, scheduled_at LIMIT 1", person_id, now)
        if row is None:
            return None
        if row["status"] != "scheduled":
            return _public_checkin(dict(row))
        local = rules.local_now(now, person["timezone"])
        if row["question_kind"] == "safety":
            # "Are you OK?": the words are set, it is asked at any hour, and the
            # watch is told to vibrate and speak at once.
            row = await conn.fetchrow(
                "UPDATE care_checkins SET status = 'asked', asked_at = $2 WHERE id = $1 RETURNING *", row["id"], now)
            await conn.execute(
                "INSERT INTO care_transcripts (person_id, checkin_id, speaker, text) VALUES ($1, $2, 'companion', $3)",
                person_id, row["id"], row["question_text"])
            return _public_checkin(dict(row))
        if settings and rules.in_quiet_hours(local.time(), settings["quiet_start"], settings["quiet_end"]):
            return None
        if row["question_kind"] == "morning":
            # The morning check keeps its own words and is not re-chosen.
            text = rules.question_text(rules.QuestionChoice("morning", "morning check"), person["preferred_name"] or person["display_name"],
                                       time_of_day=local.time(), rng=rng)
            row = await conn.fetchrow(
                "UPDATE care_checkins SET status = 'asked', asked_at = $2, question_text = $3 WHERE id = $1 RETURNING *",
                row["id"], now, text)
            await conn.execute(
                "INSERT INTO care_transcripts (person_id, checkin_id, speaker, text) VALUES ($1, $2, 'companion', $3)",
                person_id, row["id"], row["question_text"])
            return _public_checkin(dict(row))
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
        text, source = await _compose(conn, dict(person), choice, text, now)
        row = await conn.fetchrow(
            "UPDATE care_checkins SET status = 'asked', asked_at = $2, question_kind = $3, question_text = $4, "
            "question_source = $5 WHERE id = $1 RETURNING *", row["id"], now, choice.kind, text, source)
        await conn.execute(
            "INSERT INTO care_transcripts (person_id, checkin_id, speaker, text) VALUES ($1, $2, 'companion', $3)",
            person_id, row["id"], text)
        out = _public_checkin(dict(row))
        out["subject_id"] = choice.subject_id
        return out


def _public_checkin(row: dict) -> dict:
    out = {k: row[k] for k in ("id", "person_id", "scheduled_at", "asked_at", "repeated_at", "status",
                               "question_kind", "question_text")}
    out["question_source"] = row.get("question_source", "template")
    out["safety"] = row["question_kind"] == "safety"
    return out


async def _compose(conn, person: dict, choice: rules.QuestionChoice, template: str, now) -> tuple[str, str]:
    """The model composes the question from the person's context (D-033); the
    template is what is asked when the model fails, is slow, or fails the guard."""
    try:
        from . import conversation, talk
        ctx = await talk.build_context(conn, person, now)
        return await conversation.compose_checkin(ctx, choice.kind, template, subject=choice.subject)
    except Exception as exc:  # noqa: BLE001 - the template is always there
        logger.info("care check-ins: composing with the model failed (%s); template used", exc)
        return template, "template"


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
        from . import conversation
        merged = await conversation.read_answer(row["question_kind"], row["question_text"] or "", text, subject=subject,
                                                language=(await conn.fetchval(
                                                    "SELECT language FROM care_people WHERE id = $1", person_id)) or "en-GB")
        reading = merged.reading
        if row["question_kind"] == "safety" and not (text or "").strip():
            reading = rules.AnswerReading(summary="Pressed I'm OK.", said="yes", signals=(), asked_for_help=False)
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
    if row["question_kind"] == "safety":
        from . import askfirst
        async with workspace_connection(principal) as conn:
            await askfirst.answered(conn, dict(row), ok=not reading.asked_for_help, now=now, words=text or "")
    elif merged.red_flag:
        # "I fell", "I can't breathe", "chest pain": ask first, then the chain (D-034).
        from . import talk
        async with workspace_connection(principal) as conn:
            person = dict(await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", person_id))
            settings = dict(await conn.fetchrow("SELECT * FROM care_safety_settings WHERE person_id = $1", person_id) or {})
            await talk.concern(conn, person, settings, "checkin", now)
    await _remember(principal, row, reading)
    try:
        from . import talk
        async with workspace_connection(principal) as conn:
            tz = await conn.fetchval("SELECT timezone FROM care_people WHERE id = $1", person_id)
            await talk.remember(conn, person_id, rules.local_now(now, tz).date(),
                                [f"Check-in ({row['question_kind']}): {reading.summary}"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("care check-ins: conversation memory skipped (%s)", exc)
    out = _public_checkin(dict(row))
    out.update({"answer_summary": reading.summary, "said": reading.said, "signals": list(reading.signals),
                "asked_for_help": reading.asked_for_help, "structured": merged.structured.model_dump(),
                "reading_source": merged.source, "safety_check": bool(merged.red_flag and row["question_kind"] != "safety")})
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
    from . import askfirst
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow(
            "UPDATE care_checkins SET status = 'missed', answer_summary = "
            "CASE WHEN question_kind = 'safety' THEN 'No answer to the safety check.' ELSE 'No answer.' END "
            "WHERE id = $1 AND person_id = $2 AND status IN ('asked', 'repeated') RETURNING *",
            checkin_id, device["person_id"])
        if row is not None and row["question_kind"] == "safety":
            await askfirst.no_answer(conn, dict(row), now)
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
