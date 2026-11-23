"""The SOS chain: who is told, in what order, and what happens when nobody answers.

    tier 1  family and carer (circle tier 1, by priority): push, text, call
            -> wait `ack_wait_minutes` for an acknowledgement
    tier 2  the care organisation (circle tier 2): the same
            -> wait again
    tier 3  a monitoring-centre adapter if one is configured; otherwise tiers 1
            and 2 are repeated and the family app shows "call 999 now"
            -> and again, until somebody acknowledges or resolves

Every attempt is a `care_alert_attempts` row, whatever the outcome. An
acknowledgement ("I'm on my way") sets `next_action_at` to NULL and the chain
stops. A `notice` tells the tier-1 circle once and does not escalate.

**The system never dials 999.** `care/providers.py` refuses emergency numbers
before any provider is reached.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from core.identity.principal import Principal

from . import monitoring, providers
from .monitoring import Observation

logger = logging.getLogger("ira.care.escalation")

CALL_999_FLAG = "show_call_999_screen"


@dataclass
class EscalationOutcome:
    alerts_opened: int = 0
    promoted: int = 0
    tiers_run: int = 0
    attempts: int = 0
    notices: int = 0
    repeated_tier3: int = 0
    errors: int = 0
    opened: list = field(default_factory=list)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def open_from_observation(conn, person_id, obs: Observation, now: dt.datetime) -> Optional[dict]:
    """Open an alert for `obs` unless one of its kind is already unresolved.

    An `asking` alert counts as unresolved: while "Are you OK?" is out for a
    heart rate outside the range, a missed check-in that sees the same reading
    must not open a second SOS beside it (found by the sample-to-alert
    measurement in round 3, phase 1, which produced two alerts for one event)."""
    existing = await conn.fetchval(
        "SELECT id FROM care_alerts WHERE person_id = $1 AND kind = $2 "
        "AND status IN ('asking', 'open', 'escalating', 'acknowledged')", person_id, obs.kind)
    if existing is not None:
        return None
    details = dict(obs.details)
    details["summary"] = obs.summary
    row = await conn.fetchrow(
        """
        INSERT INTO care_alerts (person_id, checkin_id, kind, severity, status, details, opened_at, next_action_at)
        VALUES ($1, $2, $3, $4, 'open', $5::jsonb, $6, $6)
        RETURNING *
        """, person_id, obs.checkin_id, obs.kind, obs.severity, json.dumps(details), now)
    return dict(row)


async def _members(conn, person_id, tier: int) -> list[dict]:
    rows = await conn.fetch(
        "SELECT * FROM care_circle_members WHERE person_id = $1 AND tier = $2 AND active "
        "ORDER BY priority, display_name", person_id, tier)
    return [dict(r) for r in rows]


async def _attempt(conn, alert_id, tier, member_id, channel, delivery: providers.Delivery) -> None:
    await conn.execute(
        "INSERT INTO care_alert_attempts (alert_id, tier, member_id, channel, outcome, detail, provider_ref) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        alert_id, tier, member_id, channel, delivery.outcome, delivery.detail[:1000], delivery.provider_ref[:200])


def alert_text(person: dict, alert: dict, tier: int) -> tuple[str, str]:
    """Title and body for every channel: what was observed, who, what to do."""
    name = person["display_name"]
    summary = (alert.get("details") or {}).get("summary") or alert["kind"].replace("_", " ")
    if isinstance(alert.get("details"), str):
        try:
            summary = json.loads(alert["details"]).get("summary", summary)
        except ValueError:
            pass
    if alert["severity"] == monitoring.NOTICE:
        return f"IRA CARE: {name}'s watch needs attention", f"{summary} Please check the watch when you can."
    if alert["kind"] == "help_requested":
        summary = f"{name} asked for help from the watch."
    from . import location
    body = (f"{summary} {location.whereabouts(alert, person)} Please check on {name} now, and tap "
            f"'I'm on my way' in the IRA CARE app so everyone knows who is going. "
            f"Alert: {location.alert_link(alert['id'])}")
    if tier >= 3:
        body += " Nobody has acknowledged this alert. If you cannot reach anyone, call 999 now."
    return f"IRA CARE alert for {name}", body


async def _tell_tier(conn, principal: Principal, person: dict, alert: dict, tier: int,
                     provider: providers.MessageProvider, outcome: EscalationOutcome,
                     *, wording_tier: Optional[int] = None) -> int:
    """Reach every active member of circle `tier`. `wording_tier` is the tier
    the alert has REACHED (tier 3 wording adds 'call 999 now' when tiers 1 and
    2 are being repeated)."""
    members = await _members(conn, person["id"], tier)
    title, body = alert_text(person, alert, wording_tier or tier)
    attempts = 0
    for m in members:
        if m["push_enabled"]:
            delivery = await _push(principal, title, body, alert, m)
            await _attempt(conn, alert["id"], tier, m["id"], "push", delivery)
            attempts += 1
        if m["sms_enabled"] and m["phone"]:
            try:
                delivery = await provider.send_sms(m["phone"], f"{title}. {body}")
            except providers.EmergencyNumberRefused as exc:
                delivery = providers.Delivery("skipped", str(exc)[:200])
            await _attempt(conn, alert["id"], tier, m["id"], "sms", delivery)
            attempts += 1
        if m["call_enabled"] and m["phone"] and alert["severity"] != monitoring.NOTICE:
            try:
                delivery = await provider.call(m["phone"], f"{title}. {body}")
            except providers.EmergencyNumberRefused as exc:
                delivery = providers.Delivery("skipped", str(exc)[:200])
            await _attempt(conn, alert["id"], tier, m["id"], "call", delivery)
            attempts += 1
    if tier == 2 and members:
        await _attempt(conn, alert["id"], tier, None, "dashboard",
                       providers.Delivery("logged", "shown as a banner on the care organisation dashboard"))
        attempts += 1
    outcome.attempts += attempts
    return attempts


async def _push(principal: Principal, title: str, body: str, alert: dict, member: dict) -> providers.Delivery:
    """A push through the existing notifier (in-app, mobile push when enabled)."""
    try:
        from worker.notifier import notify_report
        priority = "critical" if alert["severity"] in (monitoring.SOS, monitoring.URGENT) else "warning"
        _id, channels = await notify_report(
            title, body, category="system", priority=priority,
            metadata={"care_alert_id": str(alert["id"]), "member_id": str(member["id"]),
                      "kind": alert["kind"], "severity": alert["severity"]},
            principal=principal)
        return providers.Delivery("sent" if "mobile" in channels else "logged",
                                  "channels: " + ", ".join(channels))
    except Exception as exc:  # noqa: BLE001 - a push failure is recorded, never raised
        logger.warning("care push failed: %s", exc)
        return providers.Delivery("failed", f"notifier error: {type(exc).__name__}")


async def run_due_escalations(conn, principal: Principal, now: dt.datetime, *,
                              provider: Optional[providers.MessageProvider] = None,
                              centre: Optional[providers.MonitoringCentreProvider] = None,
                              outcome: Optional[EscalationOutcome] = None) -> EscalationOutcome:
    """Advance every alert whose next action is due, inside one workspace."""
    outcome = outcome or EscalationOutcome()
    provider = provider or providers.provider_from_env()
    centre = centre or providers.monitoring_centre_from_env()
    # "Are you OK?" went unanswered: the question's deadline is kept here, by
    # the server, whatever the watch did (care/askfirst.py).
    from . import askfirst
    outcome.promoted = getattr(outcome, "promoted", 0) + await askfirst.promote_overdue(conn, now)
    due = await conn.fetch(
        "SELECT * FROM care_alerts WHERE status IN ('open', 'escalating') AND next_action_at IS NOT NULL "
        "AND next_action_at <= $1 ORDER BY opened_at", now)
    for row in due:
        alert = dict(row)
        if isinstance(alert.get("details"), str):
            alert["details"] = json.loads(alert["details"])
        person = await conn.fetchrow("SELECT * FROM care_people WHERE id = $1", alert["person_id"])
        settings = await conn.fetchrow("SELECT * FROM care_safety_settings WHERE person_id = $1", alert["person_id"])
        if person is None:
            continue
        person = dict(person)
        wait = dt.timedelta(minutes=int(settings["ack_wait_minutes"]) if settings else 5)
        if alert["severity"] == monitoring.NOTICE:
            await _tell_tier(conn, principal, person, alert, 1, provider, outcome)
            await conn.execute("UPDATE care_alerts SET current_tier = 1, next_action_at = NULL WHERE id = $1",
                               alert["id"])
            outcome.notices += 1
            continue
        tier = int(alert["current_tier"]) + 1
        if tier == 1 or tier == 2:
            await _tell_tier(conn, principal, person, alert, tier, provider, outcome)
            await conn.execute(
                "UPDATE care_alerts SET status = 'escalating', current_tier = $2, next_action_at = $3 WHERE id = $1",
                alert["id"], tier, now + wait)
        else:
            details = dict(alert["details"] or {})
            if getattr(centre, "configured", False):
                delivery = await centre.raise_alarm(person_label=person["display_name"],
                                                    summary=details.get("summary", alert["kind"]),
                                                    reference=str(alert["id"]))
                await _attempt(conn, alert["id"], 3, None, "monitoring_centre", delivery)
                outcome.attempts += 1
            else:
                await _attempt(conn, alert["id"], 3, None, "monitoring_centre",
                               providers.Delivery("skipped", "no monitoring centre configured; the family "
                                                             "is shown 'call 999 now' and tiers 1 and 2 repeat"))
                details[CALL_999_FLAG] = True
                for repeat_tier in (1, 2):
                    await _tell_tier(conn, principal, person, alert, repeat_tier, provider, outcome,
                                     wording_tier=3)
                outcome.repeated_tier3 += 1
            await conn.execute(
                "UPDATE care_alerts SET status = 'escalating', current_tier = 3, next_action_at = $2, "
                "details = $3::jsonb WHERE id = $1", alert["id"], now + wait, json.dumps(details))
        outcome.tiers_run += 1
    return outcome


async def evaluate_person(conn, person: dict, settings: dict, now: dt.datetime,
                          outcome: EscalationOutcome) -> list[dict]:
    """Run the rules for one person and open what they produce."""
    opened: list[dict] = []
    device = await conn.fetchrow(
        "SELECT * FROM care_devices WHERE person_id = $1 AND status = 'paired' ORDER BY last_seen_at DESC NULLS LAST LIMIT 1",
        person["id"])
    device = dict(device) if device else None
    window = max(int(settings["no_movement_window_s"]) * 2, int(settings["sync_alert_minutes"]) * 60)
    readings = [dict(r) for r in await conn.fetch(
        "SELECT kind, value, recorded_at FROM care_vitals WHERE person_id = $1 AND recorded_at >= $2",
        person["id"], now - dt.timedelta(seconds=window))]
    recent = [dict(r) for r in await conn.fetch(
        "SELECT id, status, question_kind, scheduled_at FROM care_checkins WHERE person_id = $1 "
        "ORDER BY scheduled_at DESC LIMIT 12", person["id"])]
    observations: list[Observation] = []
    # A miss that has not been looked at yet: status 'missed' with no alert
    # row pointing at it (the sensor check happens once per miss).
    unexamined = await conn.fetch(
        "SELECT c.* FROM care_checkins c WHERE c.person_id = $1 AND c.status = 'missed' "
        "AND NOT EXISTS (SELECT 1 FROM care_alerts a WHERE a.checkin_id = c.id) "
        "AND c.answer_summary = 'No answer.' ORDER BY c.scheduled_at DESC LIMIT 3", person["id"])
    for c in unexamined:
        obs = monitoring.evaluate_missed_checkin(dict(c), settings, readings, device, recent, now)
        observations.extend(obs)
        if not obs:
            # Examined, nothing to escalate: say so on the row, so it is not re-examined.
            await conn.execute("UPDATE care_checkins SET status = 'missed', answer_summary = $2 WHERE id = $1",
                               c["id"], "No answer. Readings in range and movement seen; logged as a missed check-in.")
    if device is not None:
        off_since = None
        if device.get("on_wrist") is False:
            last_on = await conn.fetchval(
                "SELECT max(recorded_at) FROM care_vitals WHERE person_id = $1 AND kind = 'on_wrist' AND value > 0",
                person["id"])
            off_since = last_on or device.get("last_sync_at") or device.get("paired_at")
        observations.extend(monitoring.evaluate_device(device, settings, person["timezone"], now, off_since))
    for obs in observations:
        alert = await open_from_observation(conn, person["id"], obs, now)
        if alert is not None:
            opened.append(alert)
            outcome.alerts_opened += 1
            outcome.opened.append({"kind": alert["kind"], "severity": alert["severity"], "id": alert["id"]})
    return opened


async def run_workspace(principal: Principal, now: Optional[dt.datetime] = None, *,
                        provider: Optional[providers.MessageProvider] = None,
                        centre: Optional[providers.MonitoringCentreProvider] = None) -> EscalationOutcome:
    from core.identity.db_context import workspace_connection
    now = now or _now()
    outcome = EscalationOutcome()
    async with workspace_connection(principal) as conn:
        people = await conn.fetch("SELECT * FROM care_people WHERE status = 'active'")
        for person in people:
            settings = await conn.fetchrow("SELECT * FROM care_safety_settings WHERE person_id = $1", person["id"])
            if settings is None:
                continue
            try:
                await evaluate_person(conn, dict(person), dict(settings), now, outcome)
            except Exception:  # noqa: BLE001
                logger.exception("care monitoring: evaluation failed for a person")
                outcome.errors += 1
        await run_due_escalations(conn, principal, now, provider=provider, centre=centre, outcome=outcome)
    return outcome


async def run_once(now: Optional[dt.datetime] = None) -> EscalationOutcome:
    """One pass over every tenant and workspace (the `care_monitoring` job)."""
    from core.identity.db_context import scheduler_connection
    from utils.db import get_pool

    from .checkin_cycle import job_principal

    now = now or _now()
    total = EscalationOutcome()
    async with get_pool().acquire() as conn:
        tenant_ids = [r["id"] for r in await conn.fetch("SELECT id FROM tenants WHERE status = 'active' ORDER BY id")]
    for tenant_id in tenant_ids:
        try:
            async with scheduler_connection(tenant_id) as conn:
                workspaces = [r["id"] for r in await conn.fetch(
                    "SELECT id FROM workspaces WHERE tenant_id = $1 AND status = 'active'", tenant_id)]
        except Exception:  # noqa: BLE001
            logger.exception("care monitoring: could not list workspaces")
            total.errors += 1
            continue
        for ws in workspaces:
            try:
                out = await run_workspace(job_principal(tenant_id, ws), now)
            except Exception:  # noqa: BLE001
                logger.exception("care monitoring: workspace pass failed")
                total.errors += 1
                continue
            for k in ("alerts_opened", "promoted", "tiers_run", "attempts", "notices", "repeated_tier3", "errors"):
                setattr(total, k, getattr(total, k) + getattr(out, k))
            total.opened.extend(out.opened)
    from .checkin_cycle import _heartbeat
    await _heartbeat("care_monitoring", total)
    return total


def tier_for_role(role: str) -> int:
    return {"family": 1, "carer": 1, "clinician": 1, "care_organisation": 2}.get(role, 1)


__all__ = ["run_once", "run_workspace", "run_due_escalations", "evaluate_person", "open_from_observation",
           "alert_text", "EscalationOutcome", "CALL_999_FLAG", "tier_for_role"]
