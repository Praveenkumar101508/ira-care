"""The live overview for a care team (phase 11): who needs attention right now.

Four lists, each a plain statement of fact from the record, never a judgement:

    attention      people with an alert awaiting a response (open or escalating), or an
                   acknowledged alert whose follow-up deadline has passed, or no
                   check-in answered today after the usual wake time
    visits         today's visits that are late or missed, and the ones due within
                   the next two hours
    alerts         every open, escalating or acknowledged alert, oldest first, with
                   whether a responder has said they are coming
    stale_watches  paired watches that have not synced within the person's
                   `sync_alert_minutes`, are off the wrist, or report a low battery
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from . import checkins as rules


def _iso(v):
    return v.isoformat() if isinstance(v, dt.datetime) else v


async def build(conn, now: Optional[dt.datetime] = None, *, visible: Optional[set] = None) -> dict:
    """`visible`: the people the caller may see (care/authz.visible_people); None for a manager."""
    now = now or dt.datetime.now(dt.timezone.utc)
    people = [dict(r) for r in await conn.fetch(
        "SELECT p.id, p.display_name, p.preferred_name, p.timezone, s.sync_alert_minutes, s.battery_alert_percent, "
        "s.usual_wake_time, s.wake_grace_minutes FROM care_people p LEFT JOIN care_safety_settings s ON s.person_id = p.id "
        "WHERE p.status = 'active' ORDER BY p.display_name")]
    if visible is not None:
        people = [p for p in people if p["id"] in visible]
    by_id = {p["id"]: p for p in people}
    name = lambda pid: (by_id[pid].get("preferred_name") or by_id[pid]["display_name"]) if pid in by_id else "someone"  # noqa: E731

    alerts = [dict(r) for r in await conn.fetch(
        "SELECT a.id, a.person_id, a.kind, a.severity, a.status, a.opened_at, a.current_tier, a.acknowledged_at, a.follow_up_due_at, "
        "a.follow_up_asked_at, a.re_escalations, "
        "(SELECT m.display_name FROM care_acknowledgements k LEFT JOIN care_circle_members m ON m.id = k.member_id "
        "  WHERE k.alert_id = a.id AND k.kind = 'on_my_way' ORDER BY k.acknowledged_at DESC LIMIT 1) AS responder, "
        "(SELECT count(*) FROM care_acknowledgements k WHERE k.alert_id = a.id AND k.kind IN ('arrived', 'checked_in_person')) AS arrivals "
        "FROM care_alerts a JOIN care_people p ON p.id = a.person_id "
        "WHERE p.status = 'active' AND a.status IN ('open', 'escalating', 'acknowledged') ORDER BY a.opened_at")]
    for a in alerts:
        a["person_name"] = name(a["person_id"])
        a["awaiting_response"] = a["status"] in ("open", "escalating")
        a["follow_up_overdue"] = bool(a["status"] == "acknowledged" and a["follow_up_due_at"] and a["follow_up_due_at"] <= now and not a["arrivals"])
        a["arrived"] = bool(a["arrivals"])
        for k in ("opened_at", "acknowledged_at", "follow_up_due_at", "follow_up_asked_at"):
            a[k] = _iso(a[k])
        a["id"], a["person_id"] = str(a["id"]), str(a["person_id"])
        a.pop("arrivals", None)

    visits = [dict(r) for r in await conn.fetch(
        "SELECT v.id, v.person_id, v.carer_name, v.planned_start, v.planned_end, v.status, v.arrived_at FROM care_visits v "
        "JOIN care_people p ON p.id = v.person_id WHERE p.status = 'active' AND v.status IN ('planned', 'late', 'arrived', 'missed') "
        "AND v.planned_start < $1::timestamptz + interval '2 hours' AND v.planned_end > $1::timestamptz - interval '12 hours' ORDER BY v.planned_start", now)]
    for v in visits:
        v["person_name"] = name(v["person_id"])
        v["state"] = ("overdue" if v["status"] in ("late", "missed") else "in_progress" if v["status"] == "arrived"
                      else "due_soon" if v["planned_start"] <= now + dt.timedelta(hours=2) else "planned")
        for k in ("planned_start", "planned_end", "arrived_at"):
            v[k] = _iso(v[k])
        v["id"], v["person_id"] = str(v["id"]), str(v["person_id"])

    devices = [dict(r) for r in await conn.fetch(
        "SELECT d.id, d.person_id, d.battery_percent, d.on_wrist, d.last_sync_at, d.background_sensor_permission FROM care_devices d "
        "JOIN care_people p ON p.id = d.person_id WHERE p.status = 'active' AND d.status = 'paired'")]
    stale = []
    for d in devices:
        p = by_id.get(d["person_id"], {})
        limit = int(p.get("sync_alert_minutes") or 30)
        reasons = []
        if d["last_sync_at"] is None or now - d["last_sync_at"] > dt.timedelta(minutes=limit):
            reasons.append(f"not synced for {int((now - d['last_sync_at']).total_seconds() // 60) if d['last_sync_at'] else 'ever'} min" if d["last_sync_at"] else "never synced")
        if d["on_wrist"] is False:
            reasons.append("off the wrist")
        if d["battery_percent"] is not None and d["battery_percent"] <= int(p.get("battery_alert_percent") or 15):
            reasons.append(f"battery {d['battery_percent']} %")
        if d["background_sensor_permission"] is False:
            reasons.append("sensor permission off")
        if reasons:
            stale.append({"device_id": str(d["id"]), "person_id": str(d["person_id"]), "person_name": name(d["person_id"]),
                          "reasons": reasons, "last_sync_at": _iso(d["last_sync_at"]), "battery_percent": d["battery_percent"]})

    # No check-in answered today, after the usual wake time plus the grace.
    answered_today = {r["person_id"] for r in await conn.fetch(
        "SELECT DISTINCT person_id FROM care_checkins WHERE status = 'answered' AND answered_at >= $1", now - dt.timedelta(hours=24))}
    quiet_people = []
    for p in people:
        local = rules.local_now(now, p["timezone"])
        wake = p.get("usual_wake_time")
        if wake is None:
            continue
        deadline = local.replace(hour=wake.hour, minute=wake.minute, second=0, microsecond=0) + dt.timedelta(minutes=int(p.get("wake_grace_minutes") or 60))
        if local >= deadline and p["id"] not in answered_today:
            quiet_people.append(p["id"])

    attention = []
    for p in people:
        reasons = []
        mine = [a for a in alerts if a["person_id"] == str(p["id"])]
        if any(a["awaiting_response"] for a in mine):
            reasons.append("an alert is waiting for someone")
        if any(a["follow_up_overdue"] for a in mine):
            reasons.append("a responder said they were coming and has not arrived")
        if any(v["person_id"] == str(p["id"]) and v["state"] == "overdue" for v in visits):
            reasons.append("a visit is late or did not happen")
        if p["id"] in quiet_people:
            reasons.append("no check-in answered since the usual wake time")
        if reasons:
            attention.append({"person_id": str(p["id"]), "person_name": name(p["id"]), "reasons": reasons})

    if visible is not None:
        mine = {str(pid) for pid in visible}
        visits = [v for v in visits if str(v["person_id"]) in mine]
        alerts = [a for a in alerts if str(a["person_id"]) in mine]
        stale = [d for d in stale if str(d["person_id"]) in mine]
    return {"now": now.isoformat(), "people": len(people), "attention": attention,
            "visits": visits, "alerts": alerts, "stale_watches": stale}


__all__ = ["build"]
