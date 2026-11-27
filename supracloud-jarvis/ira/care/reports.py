"""The daily carer report and the weekly family report.

Both are built from the care tables for one person and one period, in plain
words that describe what happened: readings seen, check-ins answered and
missed, routines kept, doses confirmed, alerts and who acknowledged them, and
the notable points from the check-in summaries. Neither contains the person's
own words, whatever the consents say: a report travels (push, e-mail) in a way
a screen does not. Neither judges: there are no diagnosis words, and readings
are reported against the range the carer set, or as "no range set".

Reports are generated once per person and period (`care_reports` has a unique
key on that) by the `care_reports` worker job, which runs every hour and does
the daily report after 07:00 in the person's own timezone and the weekly report
on Monday mornings. They are also generated on demand from the API.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Optional
from zoneinfo import ZoneInfo

from core.identity.principal import Principal

from . import checkins as rules
from .monitoring import no_diagnosis_words

logger = logging.getLogger("ira.care.reports")

DAILY = "daily_carer"
WEEKLY = "weekly_family"
ALERT_WORDS = {
    "help_requested": "asked for help", "no_answer_no_movement": "no answer and no movement",
    "heart_rate_outside_range": "heart rate outside the set range", "off_wrist_during_check": "no answer and watch off the wrist",
    "missed_checkins": "several check-ins missed in a row", "off_wrist": "watch off the wrist", "low_battery": "watch battery low",
    "sensor_permission_off": "sensor permission switched off", "no_sync": "watch not synced", "manual": "raised by a carer",
    "off_wrist_night": "watch off the wrist overnight", "suspected_fall": "suspected fall", "concerning_phrase": "said something that needed a check",
    "visit_late": "a planned visit was late", "visit_missed": "a planned visit did not happen", "watch_unpaired": "watch unpaired from the wrist",
}
VISIT_OUTCOME_WORDS = {"all_well": "all well", "concern_noted": "a concern was noted", "needed_help": "needed help",
                       "not_at_home": "not at home", "declined": "declined the visit", "other": "other"}


@dataclass
class Period:
    start: dt.date
    end: dt.date          # inclusive
    tz: ZoneInfo

    @property
    def start_at(self) -> dt.datetime:
        return dt.datetime.combine(self.start, dt.time(0, 0), self.tz)

    @property
    def end_at(self) -> dt.datetime:
        return dt.datetime.combine(self.end + dt.timedelta(days=1), dt.time(0, 0), self.tz)


@dataclass
class ReportData:
    person: dict
    period: Period
    readings: list = field(default_factory=list)
    checkins: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    acknowledgements: list = field(default_factory=list)
    routines: list = field(default_factory=list)
    medications: list = field(default_factory=list)
    medication_events: list = field(default_factory=list)
    settings: Optional[dict] = None
    device: Optional[dict] = None
    visits: list = field(default_factory=list)
    reminders: list = field(default_factory=list)
    memory: list = field(default_factory=list)


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


async def gather(conn, person: dict, period: Period) -> ReportData:
    pid = person["id"]
    a, b = period.start_at, period.end_at
    data = ReportData(person=dict(person), period=period)
    data.readings = [dict(r) for r in await conn.fetch(
        "SELECT kind, value, unit, recorded_at FROM care_vitals WHERE person_id = $1 AND recorded_at >= $2 AND recorded_at < $3",
        pid, a, b)]
    data.checkins = [dict(r) for r in await conn.fetch(
        "SELECT * FROM care_checkins WHERE person_id = $1 AND scheduled_at >= $2 AND scheduled_at < $3 ORDER BY scheduled_at", pid, a, b)]
    data.alerts = [dict(r) for r in await conn.fetch(
        "SELECT * FROM care_alerts WHERE person_id = $1 AND opened_at >= $2 AND opened_at < $3 ORDER BY opened_at", pid, a, b)]
    if data.alerts:
        data.acknowledgements = [dict(r) for r in await conn.fetch(
            "SELECT k.*, m.display_name FROM care_acknowledgements k LEFT JOIN care_circle_members m ON m.id = k.member_id "
            "WHERE k.alert_id = ANY($1::uuid[]) ORDER BY k.acknowledged_at", [x["id"] for x in data.alerts])]
    data.routines = [dict(r) for r in await conn.fetch("SELECT * FROM care_routines WHERE person_id = $1 AND active", pid)]
    data.medications = [dict(r) for r in await conn.fetch("SELECT * FROM care_medications WHERE person_id = $1 AND active", pid)]
    data.medication_events = [dict(r) for r in await conn.fetch(
        "SELECT * FROM care_medication_events WHERE person_id = $1 AND due_at >= $2 AND due_at < $3 ORDER BY due_at", pid, a, b)]
    s = await conn.fetchrow("SELECT * FROM care_safety_settings WHERE person_id = $1", pid)
    data.settings = dict(s) if s else None
    d = await conn.fetchrow("SELECT * FROM care_devices WHERE person_id = $1 AND status = 'paired' ORDER BY last_seen_at DESC NULLS LAST LIMIT 1", pid)
    data.device = dict(d) if d else None
    # Phase 11: the visits, the reminders and what IRA remembers (summaries only) belong in the record too.
    data.visits = [dict(r) for r in await conn.fetch(
        "SELECT v.*, (SELECT count(*) FROM care_visit_tasks t WHERE t.visit_id = v.id) AS tasks, "
        "(SELECT count(*) FROM care_visit_tasks t WHERE t.visit_id = v.id AND t.done) AS tasks_done "
        "FROM care_visits v WHERE v.person_id = $1 AND v.planned_start >= $2 AND v.planned_start < $3 ORDER BY v.planned_start", pid, a, b)]
    data.reminders = [dict(r) for r in await conn.fetch(
        "SELECT * FROM care_reminders WHERE person_id = $1 AND due_at >= $2 AND due_at < $3 ORDER BY due_at", pid, a, b)]
    data.memory = [dict(r) for r in await conn.fetch(
        "SELECT day, summary, exchanges, edited_by FROM care_conversation_memory WHERE person_id = $1 AND day >= $2 AND day <= $3 ORDER BY day",
        pid, period.start, period.end)]
    return data


def summarise_visits(visits: list) -> dict:
    out = {"planned": len(visits), "done": 0, "missed": 0, "late": 0, "cancelled": 0, "outcomes": {}, "handovers": [], "tasks": 0, "tasks_done": 0}
    for v in visits:
        st = v["status"]
        if st == "departed":
            out["done"] += 1
            out["outcomes"][v["outcome"]] = out["outcomes"].get(v["outcome"], 0) + 1
            if v.get("handover"):
                out["handovers"].append({"at": v["departed_at"].isoformat() if v.get("departed_at") else None,
                                         "carer": v.get("carer_name") or "a carer", "handover": v["handover"][:400]})
        elif st == "missed":
            out["missed"] += 1
        elif st == "cancelled":
            out["cancelled"] += 1
        if v.get("late_alerted_at"):
            out["late"] += 1
        out["tasks"] += int(v.get("tasks") or 0)
        out["tasks_done"] += int(v.get("tasks_done") or 0)
    return out


def summarise_reminders(reminders: list) -> dict:
    out = {"set": len(reminders), "confirmed": 0, "skipped": 0, "missed": 0, "medication": 0, "medication_confirmed": 0, "hydration": 0, "hydration_confirmed": 0}
    for r in reminders:
        if r["status"] == "confirmed":
            out["confirmed"] += 1
        elif r["status"] == "skipped":
            out["skipped"] += 1
        elif r["status"] == "missed":
            out["missed"] += 1
        if r["kind"] == "medication":
            out["medication"] += 1
            out["medication_confirmed"] += int(r["status"] == "confirmed")
        if r["kind"] == "hydration":
            out["hydration"] += 1
            out["hydration_confirmed"] += int(r["status"] == "confirmed")
    return out


# ── Summaries (pure) ─────────────────────────────────────────────────────────

def summarise_readings(readings: list, settings: Optional[dict]) -> dict:
    hr = [float(r["value"]) for r in readings if r["kind"] == "heart_rate"]
    steps = sum(float(r["value"]) for r in readings if r["kind"] in ("steps", "movement"))
    on_wrist = [float(r["value"]) for r in readings if r["kind"] == "on_wrist"]
    out: dict = {"heart_rate_readings": len(hr), "steps_total": round(steps), "on_wrist_readings": len(on_wrist)}
    if hr:
        out.update({"heart_rate_min": round(min(hr)), "heart_rate_max": round(max(hr)), "heart_rate_avg": round(sum(hr) / len(hr))})
        lo = settings.get("heart_rate_min") if settings else None
        hi = settings.get("heart_rate_max") if settings else None
        if lo is None and hi is None:
            out["range"] = "no range set"
            out["outside_range"] = None
        else:
            out["range"] = f"{lo if lo is not None else 'no lower'} to {hi if hi is not None else 'no upper'}"
            out["outside_range"] = sum(1 for v in hr if (lo is not None and v < lo) or (hi is not None and v > hi))
    if on_wrist:
        out["on_wrist_share"] = round(100 * sum(1 for v in on_wrist if v >= 0.5) / len(on_wrist))
    return out


def summarise_checkins(checkins: list) -> dict:
    counts = rules.missed_in_a_row  # imported for symmetry; the count below is simpler
    _ = counts
    asked = [c for c in checkins if c["status"] not in ("scheduled", "cancelled", "skipped_quiet")]
    answered = [c for c in asked if c["status"] == "answered"]
    missed = [c for c in asked if c["status"] in ("missed", "escalated")]
    notable = []
    for c in answered:
        summary = c.get("answer_summary") or ""
        if "(" in summary and any(w in summary for w in ("pain", "low", "help")):
            notable.append({"at": c["scheduled_at"].isoformat(), "kind": c["question_kind"], "summary": summary})
    meals = [c for c in answered if c["question_kind"] == "meal"]
    meals_yes = sum(1 for c in meals if "had eaten" in (c.get("answer_summary") or ""))
    return {"asked": len(asked), "answered": len(answered), "missed": len(missed),
            "quiet_skipped": sum(1 for c in checkins if c["status"] == "skipped_quiet"),
            "meals_asked": len(meals), "meals_eaten": meals_yes, "notable": notable[:6]}


def summarise_medication(medications: list, events: list) -> dict:
    taken = sum(1 for e in events if e["status"] == "taken")
    missed = sum(1 for e in events if e["status"] == "missed")
    unknown = sum(1 for e in events if e["status"] in ("unknown", "due"))
    return {"medications": len(medications), "doses_recorded": len(events), "taken": taken, "missed": missed, "unknown": unknown}


def summarise_alerts(alerts: list, acknowledgements: list) -> list[dict]:
    acks = {}
    for k in acknowledgements:
        acks.setdefault(k["alert_id"], k)
    out = []
    for a in alerts:
        k = acks.get(a["id"])
        out.append({
            "at": a["opened_at"].isoformat(), "kind": a["kind"], "words": ALERT_WORDS.get(a["kind"], a["kind"].replace("_", " ")),
            "severity": a["severity"], "status": a["status"], "tier": a["current_tier"],
            "acknowledged_by": (k or {}).get("display_name"), "acknowledged_at": k["acknowledged_at"].isoformat() if k else None,
            "outcome": a.get("outcome") or "", "summary_by": "IRA",
        })
    return out


def _day_name(d: dt.date) -> str:
    return d.strftime("%A %d %B %Y")


def render_daily(data: ReportData) -> tuple[str, str, dict]:
    """Title, plain text and the structured body of the carer's daily report."""
    name = data.person["display_name"]
    r = summarise_readings(data.readings, data.settings)
    c = summarise_checkins(data.checkins)
    m = summarise_medication(data.medications, data.medication_events)
    alerts = summarise_alerts(data.alerts, data.acknowledgements)
    lines = [f"Daily report for {name}: {_day_name(data.period.start)}", ""]
    lines.append(f"Check-ins: {c['answered']} answered, {c['missed']} missed, of {c['asked']} asked; {c['quiet_skipped']} skipped at night.")
    if c["meals_asked"]:
        lines.append(f"Meals: asked about {c['meals_asked']} time(s); said they had eaten {c['meals_eaten']} time(s).")
    if m["medications"]:
        lines.append(f"Medication: {m['taken']} dose(s) confirmed taken, {m['missed']} said not taken, {m['unknown']} not confirmed, "
                     f"of {m['doses_recorded']} recorded.")
    if r["heart_rate_readings"]:
        lines.append(f"Heart rate: {r['heart_rate_readings']} readings, {r['heart_rate_min']} to {r['heart_rate_max']} "
                     f"(average {r['heart_rate_avg']}); range set for this person: {r['range']}"
                     + (f"; {r['outside_range']} reading(s) outside it." if r.get("outside_range") is not None else "."))
    else:
        lines.append("Heart rate: no readings today.")
    lines.append(f"Movement: {r['steps_total']} steps or movement counts recorded.")
    if "on_wrist_share" in r:
        lines.append(f"Watch worn for about {r['on_wrist_share']} % of the readings.")
    if data.device:
        d = data.device
        lines.append(f"Watch now: battery {d.get('battery_percent') if d.get('battery_percent') is not None else 'unknown'} %, "
                     f"{'on' if d.get('on_wrist') else 'not on' if d.get('on_wrist') is False else 'unknown whether on'} the wrist, "
                     f"sensor permission {'on' if d.get('background_sensor_permission') else 'OFF' if d.get('background_sensor_permission') is False else 'unknown'}.")
    v = summarise_visits(data.visits)
    if v["planned"]:
        words = f"Visits: {v['planned']} planned, {v['done']} done"
        if v["missed"]:
            words += f", {v['missed']} did not happen"
        if v["late"]:
            words += f", {v['late']} started late"
        if v["tasks"]:
            words += f"; {v['tasks_done']} of {v['tasks']} planned tasks done"
        if v["outcomes"]:
            words += "; outcomes: " + ", ".join(f"{VISIT_OUTCOME_WORDS.get(k, k)} ({n})" for k, n in v["outcomes"].items())
        lines.append(words + ".")
        for h in v["handovers"]:
            lines.append(f"  Handover from {h['carer']}: {h['handover']}")
    rm = summarise_reminders(data.reminders)
    if rm["set"]:
        lines.append(f"Reminders: {rm['set']} due, {rm['confirmed']} confirmed, {rm['skipped']} skipped, {rm['missed']} unanswered"
                     + (f"; medication {rm['medication_confirmed']} of {rm['medication']} confirmed in two steps" if rm["medication"] else "")
                     + (f"; drinks {rm['hydration_confirmed']} of {rm['hydration']}" if rm["hydration"] else "") + ".")
    if alerts:
        lines.append("")
        lines.append(f"Alerts ({len(alerts)}):")
        for a in alerts:
            who = f" acknowledged by {a['acknowledged_by']}" if a["acknowledged_by"] else ""
            outcome = f", outcome {a['outcome'].replace('_', ' ')}" if a.get("outcome") else ""
            lines.append(f"  - {a['at'][11:16]} {a['words']} ({a['severity']}, reached tier {a['tier']}, now {a['status']}{who}{outcome})")
    else:
        lines.append("")
        lines.append("Alerts: none.")
    if data.memory:
        lines.append("")
        lines.append("What IRA remembers (summary by IRA, never the person's words):")
        for m in data.memory:
            for ln in (m["summary"] or "").split("\n")[:6]:
                if ln.strip():
                    lines.append(f"  - {ln.strip()}" + (" (edited by the " + m["edited_by"] + ")" if m.get("edited_by") else ""))
    if c["notable"]:
        lines.append("")
        lines.append("Worth a look (from the check-in summaries):")
        for n in c["notable"]:
            lines.append(f"  - {n['at'][11:16]} {n['summary']}")
    text = "\n".join(lines)
    assert no_diagnosis_words(text), "a report must describe, never diagnose"
    body = {"readings": r, "checkins": c, "medication": m, "alerts": alerts, "device": _public_device(data.device),
            "visits": v, "reminders": rm, "memory": [{"day": x["day"].isoformat(), "summary": x["summary"], "edited_by": x.get("edited_by") or ""} for x in data.memory]}
    return lines[0], text, body


def render_weekly(data: ReportData, days: list[ReportData]) -> tuple[str, str, dict]:
    """Title, plain text and body of the family's weekly report: gentler words,
    day by day, summaries only."""
    name = data.person["preferred_name"] or data.person["display_name"]
    p = data.period
    lines = [f"{name}'s week: {p.start.strftime('%d %B')} to {p.end.strftime('%d %B %Y')}", ""]
    per_day = []
    total_answered = total_missed = 0
    for d in days:
        c = summarise_checkins(d.checkins)
        total_answered += c["answered"]; total_missed += c["missed"]
        m = summarise_medication(d.medications, d.medication_events)
        alerts = summarise_alerts(d.alerts, d.acknowledgements)
        vv = summarise_visits(d.visits)
        per_day.append({"day": d.period.start.isoformat(), "checkins": c, "medication": m, "alerts": alerts, "visits": vv})
        words = f"{d.period.start.strftime('%A')}: {c['answered']} of {c['asked']} check-ins answered"
        if m["doses_recorded"]:
            words += f", {m['taken']} dose(s) confirmed"
        if vv["planned"]:
            words += f", {vv['done']} of {vv['planned']} visit(s) made" + (f" ({vv['missed']} did not happen)" if vv["missed"] else "")
        if alerts:
            words += f", {len(alerts)} alert(s): " + "; ".join(a["words"] + (" (someone went)" if a["acknowledged_by"] else "") for a in alerts)
        lines.append(words + ".")
    lines.append("")
    lines.append(f"Over the week {name} answered {total_answered} check-ins and missed {total_missed}.")
    r = summarise_readings(data.readings, data.settings)
    if r["heart_rate_readings"]:
        lines.append(f"Heart rate readings stayed between {r['heart_rate_min']} and {r['heart_rate_max']} "
                     f"(range set by the carer: {r['range']}).")
    vw = summarise_visits(data.visits)
    if vw["handovers"]:
        lines.append("")
        lines.append("Handovers from the carers this week:")
        for h in vw["handovers"][-5:]:
            lines.append(f"  - {h['carer']}: {h['handover']}")
    lines.append("")
    lines.append("This report has summaries only. What the person said in their own words is private, "
                 "and shown in the app only with their recorded consent.")
    text = "\n".join(lines)
    assert no_diagnosis_words(text), "a report must describe, never diagnose"
    return lines[0], text, {"days": per_day, "readings": r, "answered": total_answered, "missed": total_missed, "visits": vw}


def _public_device(d: Optional[dict]) -> Optional[dict]:
    if not d:
        return None
    return {k: d.get(k) for k in ("battery_percent", "on_wrist", "background_sensor_permission", "last_sync_at")}


# ── Generation and storage ───────────────────────────────────────────────────

def _jsonable(obj):
    return json.loads(json.dumps(obj, default=str))


async def generate(conn, person: dict, kind: str, day: dt.date) -> Optional[dict]:
    """Generate and store one report; None if it already exists for the period."""
    tz = _tz(person["timezone"])
    if kind == DAILY:
        period = Period(day, day, tz)
        data = await gather(conn, person, period)
        title, text, body = render_daily(data)
    elif kind == WEEKLY:
        start = day - dt.timedelta(days=day.weekday())      # Monday
        period = Period(start, start + dt.timedelta(days=6), tz)
        data = await gather(conn, person, period)
        days = [await gather(conn, person, Period(start + dt.timedelta(days=i), start + dt.timedelta(days=i), tz)) for i in range(7)]
        title, text, body = render_weekly(data, days)
    else:
        raise ValueError(f"unknown report kind {kind!r}")
    row = await conn.fetchrow(
        """
        INSERT INTO care_reports (person_id, kind, period_start, period_end, title, body_text, body)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        ON CONFLICT (person_id, kind, period_start) DO NOTHING
        RETURNING *
        """, person["id"], kind, period.start, period.end, title[:200], text[:20000], json.dumps(_jsonable(body)))
    return _public(row) if row else None


async def rebuild_covering(conn, person: dict, day: dt.date) -> int:
    """Rebuild every stored report whose period covers `day`, from what is stored now.

    Called when the person or their representative forgets or corrects a day of memory
    (register F6.b, F6.c; D-106): a report generated before the change copied the old summary,
    and the family and carers would otherwise go on reading what was deleted. Runs on the
    caller's connection, inside its transaction, so the memory change and the rebuilt reports
    commit together. Returns the number of reports rebuilt.
    """
    rows = await conn.fetch("DELETE FROM care_reports WHERE person_id = $1 AND period_start <= $2 AND period_end >= $2 "
                            "RETURNING kind, period_start", person["id"], day)
    for r in rows:
        await generate(conn, person, r["kind"], r["period_start"])
    return len(rows)


def _public(row) -> dict:
    d = dict(row)
    if isinstance(d.get("body"), str):
        d["body"] = json.loads(d["body"])
    return d


async def list_reports(conn, person_id, *, kind: Optional[str], limit: int) -> list[dict]:
    rows = await conn.fetch(
        "SELECT * FROM care_reports WHERE person_id = $1 AND ($2::text IS NULL OR kind = $2) "
        "ORDER BY period_start DESC, generated_at DESC LIMIT $3", person_id, kind, limit)
    return [_public(r) for r in rows]


async def get_report(conn, report_id) -> Optional[dict]:
    row = await conn.fetchrow("SELECT * FROM care_reports WHERE id = $1", report_id)
    return _public(row) if row else None


async def deliver(conn, principal: Principal, report: dict, *, push_provider=None) -> list[str]:
    """Push a stored report to the phones of this person's circle members who
    have push on (care/push.py: their registered tokens only, never the
    deployment-wide notifier), and record where it went. The report itself is
    read in the app; the push carries the title and the first lines."""
    from . import push
    channels: list[str] = []
    try:
        members = [dict(r) for r in await conn.fetch(
            "SELECT id FROM care_circle_members WHERE person_id = $1 AND active AND push_enabled", report["person_id"])]
        pseudo_alert = {"id": report["id"], "kind": f"report_{report['kind']}", "severity": "notice"}
        outcomes = []
        for m in members:
            outcome, _detail, _tickets = await push.send_to_member(
                conn, m, pseudo_alert, report["title"], report["body_text"][:200], push_provider)
            outcomes.append(outcome)
        if "sent" in outcomes:
            channels = ["push"]
        elif "logged" in outcomes:
            channels = ["push_logged"]
        else:
            channels = ["app"]   # readable in the app and on the dashboard; nobody had a phone registered
    except Exception as exc:  # noqa: BLE001
        logger.warning("care report delivery failed: %s", type(exc).__name__)
        channels = ["failed"]
    await conn.execute("UPDATE care_reports SET delivered_channels = $2 WHERE id = $1", report["id"], channels)
    return channels


@dataclass
class ReportsOutcome:
    daily: int = 0
    weekly: int = 0
    errors: int = 0


async def run_workspace(principal: Principal, now: Optional[dt.datetime] = None, *, deliver_reports: bool = True) -> ReportsOutcome:
    """Generate (and deliver) what is due in one workspace: yesterday's daily
    report once the person's local clock has passed 07:00, and last week's
    family report on Monday after 08:00."""
    from core.identity.db_context import workspace_connection
    now = now or dt.datetime.now(dt.timezone.utc)
    out = ReportsOutcome()
    async with workspace_connection(principal) as conn:
        people = [dict(p) for p in await conn.fetch("SELECT * FROM care_people WHERE status = 'active'")]
        for person in people:
            try:
                local = now.astimezone(_tz(person["timezone"]))
                if local.hour >= 7:
                    rep = await generate(conn, person, DAILY, local.date() - dt.timedelta(days=1))
                    if rep:
                        out.daily += 1
                        if deliver_reports:
                            await deliver(conn, principal, rep)
                if local.weekday() == 0 and local.hour >= 8:
                    rep = await generate(conn, person, WEEKLY, local.date() - dt.timedelta(days=7))
                    if rep:
                        out.weekly += 1
                        if deliver_reports:
                            await deliver(conn, principal, rep)
            except Exception:  # noqa: BLE001
                logger.exception("care reports: generation failed for a person")
                out.errors += 1
    return out


async def run_once(now: Optional[dt.datetime] = None) -> ReportsOutcome:
    from core.identity.db_context import scheduler_connection
    from utils.db import get_pool

    from .checkin_cycle import job_principal

    total = ReportsOutcome()
    async with get_pool().acquire() as conn:
        tenant_ids = [r["id"] for r in await conn.fetch("SELECT id FROM tenants WHERE status = 'active' ORDER BY id")]
    for tenant_id in tenant_ids:
        try:
            async with scheduler_connection(tenant_id) as conn:
                workspaces = [r["id"] for r in await conn.fetch(
                    "SELECT id FROM workspaces WHERE tenant_id = $1 AND status = 'active'", tenant_id)]
            for ws in workspaces:
                out = await run_workspace(job_principal(tenant_id, ws), now)
                total.daily += out.daily; total.weekly += out.weekly; total.errors += out.errors
        except Exception:  # noqa: BLE001
            logger.exception("care reports: tenant pass failed")
            total.errors += 1
    return total
