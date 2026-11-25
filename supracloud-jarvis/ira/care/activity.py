"""IRA Care Activity in the product: hourly estimates from the watch, under consent (F7, D-084).

The data contract (docs/ai/activity/DATA_CONTRACT.md). The watch classifies 10-second windows
with IRA Care Activity (``care/activity_estimate.py``, ported to Kotlin) and sends only HOURLY
summaries, and only while the person's ``activity_sharing`` consent is granted:

    period_start, period_end   one hour at most
    model_version, model_sha256  the format string and the SHA-256 of the exact model file;
                               a summary from any model this server does not hold is refused
    source                     'device' (the watch's own sensors) or 'simulated'
    seconds                    seconds per label: the model's classes, 'unknown', 'unavailable'
    reasons                    seconds per reason: 'low_confidence' (all of 'unknown') and
                               'off_wrist', 'sensor_gap', 'too_short', 'sensor_fault' (all of
                               'unavailable', between them)
    windows                    how many 10-second windows
    mean_confidence            mean top-class probability of the windows that had one, or null

What the product does with it, and what it never does:

* Carers see minutes per day and per week in four groups: **active** (walking, stairs),
  **still** (sitting, standing), **other arm movement** (eating_drinking, other) and
  **unknown or unavailable** (including time the watch did not record). The class
  ``eating_drinking`` is never shown by name and never read as "ate": it is a hand-to-mouth
  wrist pattern, and the model mistakes other things for it.
* It is labelled as an estimate from the watch's motion sensor, not a measurement of health.
* **No alert, escalation, check-in or report reads it.** It is stored in its own table,
  ``care_activity_periods``, which none of the safety rules query; ingestion never calls
  ``evaluate_arrivals``. ``tests/care/test_activity_no_alert_path.py`` holds that in place.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from functools import lru_cache
from typing import Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from . import activity_estimate, consent

CONSENT_KIND = "activity_sharing"

UNKNOWN = "unknown"
UNAVAILABLE = "unavailable"
LOW_CONFIDENCE = "low_confidence"
UNAVAILABLE_REASONS = ("off_wrist", "sensor_gap", "too_short", "sensor_fault")
REASONS = (LOW_CONFIDENCE,) + UNAVAILABLE_REASONS
SOURCES = ("device", "simulated")

#: The groups a carer sees. Every class of the model is in exactly one (checked by the tests).
GROUPS: dict[str, tuple[str, ...]] = {
    "active": ("walking", "stairs"),
    "still": ("sitting", "standing"),
    "other_movement": ("eating_drinking", "other"),
}

#: How each group is described to a carer. The model's class names are never shown.
GROUP_WORDS = {
    "active": "walking or on stairs",
    "still": "sitting or standing",
    "other_movement": "other arm movement",
    "unknown_or_unavailable": "the watch could not tell, was not worn, or recorded nothing",
}

#: The words every view of it carries.
LABEL = "Estimated from the watch's motion sensor; not a measurement of health."

#: Upper bound on one period, seconds; one window is 10 s.
MAX_PERIOD_S = 3600
WINDOW_S = 10


@lru_cache(maxsize=1)
def known_model() -> tuple[str, str, tuple[str, ...]]:
    """(version, sha256, classes) of the model this server holds, byte for byte the watch's."""
    raw = activity_estimate.MODEL_PATH.read_bytes()
    spec = json.loads(raw.decode("utf-8"))
    return spec["format"], hashlib.sha256(raw).hexdigest(), tuple(spec["classes"])


def labels() -> tuple[str, ...]:
    """The closed set of labels: the model's classes, then 'unknown' and 'unavailable'."""
    return known_model()[2] + (UNKNOWN, UNAVAILABLE)


class PeriodRefused(ValueError):
    """A summary that breaks the contract. Nothing of the batch is stored."""


def check_period(p: Mapping, *, now: dt.datetime) -> None:
    """Raise :class:`PeriodRefused` unless ``p`` keeps the contract."""
    version, sha, _classes = known_model()
    start, end = p["period_start"], p["period_end"]
    if start.tzinfo is None or end.tzinfo is None:
        raise PeriodRefused("period_start and period_end need a timezone")
    span = (end - start).total_seconds()
    if not 0 < span <= MAX_PERIOD_S:
        raise PeriodRefused("a period is longer than zero and at most one hour")
    if end > now + dt.timedelta(minutes=5):
        raise PeriodRefused("a period cannot end in the future")
    if p["model_version"] != version or p["model_sha256"] != sha:
        raise PeriodRefused("the summary names a model this server does not hold")
    if p["source"] not in SOURCES:
        raise PeriodRefused(f"source must be one of {SOURCES}")
    seconds, reasons = dict(p["seconds"]), dict(p.get("reasons") or {})
    allowed = set(labels())
    if not set(seconds) <= allowed:
        raise PeriodRefused(f"unknown label(s) {sorted(set(seconds) - allowed)}; labels are {sorted(allowed)}")
    if not set(reasons) <= set(REASONS):
        raise PeriodRefused(f"unknown reason(s) {sorted(set(reasons) - set(REASONS))}")
    if any((not isinstance(v, int)) or isinstance(v, bool) or v < 0 for v in list(seconds.values()) + list(reasons.values())):
        raise PeriodRefused("seconds are whole numbers, zero or more")
    if sum(seconds.values()) > span:
        raise PeriodRefused("the labels add up to more than the period")
    if reasons.get(LOW_CONFIDENCE, 0) != seconds.get(UNKNOWN, 0):
        raise PeriodRefused("'unknown' time must all carry the reason low_confidence")
    if sum(reasons.get(r, 0) for r in UNAVAILABLE_REASONS) != seconds.get(UNAVAILABLE, 0):
        raise PeriodRefused("'unavailable' time must be accounted for by its reasons")
    windows = int(p["windows"])
    if not 0 <= windows <= MAX_PERIOD_S // WINDOW_S or windows * WINDOW_S < sum(seconds.values()) - WINDOW_S:
        raise PeriodRefused("the window count does not match the seconds")
    mc = p.get("mean_confidence")
    if mc is not None and not 0.0 <= float(mc) <= 1.0:
        raise PeriodRefused("mean_confidence lies between 0 and 1")


def sharing_granted(consent_rows: Iterable[Mapping]) -> bool:
    return consent.consent_granted(consent_rows, CONSENT_KIND)


# ── Ingestion (device-authenticated) ─────────────────────────────────────────

async def record_periods(conn, device: dict, periods: list[dict], *, now: Optional[dt.datetime] = None) -> dict:
    """Store a watch's hourly summaries, if the person's consent covers it.

    Checked here again, whatever the watch believes: with the consent not granted (never
    recorded, declined or revoked) nothing is stored and the answer says ``sharing: false``,
    so the watch clears what it holds. A re-sent hour is ignored, not duplicated. This
    function opens no alert and calls no safety rule."""
    now = now or dt.datetime.now(dt.timezone.utc)
    for p in periods:
        check_period(p, now=now)
    rows = [dict(r) for r in await conn.fetch(
        "SELECT kind, granted, recorded_at, revoked_at FROM care_consents WHERE person_id = $1", device["person_id"])]
    if not sharing_granted(rows):
        return {"accepted": len(periods), "stored": 0, "sharing": False}
    stored = 0
    for p in periods:
        tag = await conn.execute(
            """
            INSERT INTO care_activity_periods
                (person_id, device_id, period_start, period_end, source, model_version, model_sha256,
                 seconds, reasons, windows, mean_confidence)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10, $11)
            ON CONFLICT (device_id, period_start) WHERE device_id IS NOT NULL DO NOTHING
            """,
            device["person_id"], device["id"], p["period_start"], p["period_end"], p["source"],
            p["model_version"], p["model_sha256"], json.dumps(p["seconds"]), json.dumps(p.get("reasons") or {}),
            int(p["windows"]), p.get("mean_confidence"))
        stored += int(tag.split()[-1])
    return {"accepted": len(periods), "stored": stored, "sharing": True}


async def sharing_for_person(conn, person_id) -> bool:
    rows = [dict(r) for r in await conn.fetch(
        "SELECT kind, granted, recorded_at, revoked_at FROM care_consents WHERE person_id = $1", person_id)]
    return sharing_granted(rows)


# ── Caregiver summaries (pure) ───────────────────────────────────────────────

def _group_of(label: str) -> str:
    for g, members in GROUPS.items():
        if label in members:
            return g
    return "unknown_or_unavailable"


def _empty() -> dict:
    return {"active": 0.0, "still": 0.0, "other_movement": 0.0, "unknown": 0.0, "unavailable": 0.0, "not_recorded": 0.0}


def _minutes(d: dict) -> dict:
    out = {k: int(round(v / 60)) for k, v in d.items()}
    out["unknown_or_unavailable"] = out["unknown"] + out["unavailable"] + out["not_recorded"]
    return out


def summarise(rows: Iterable[Mapping], *, tz: ZoneInfo, today: dt.date, now: dt.datetime, days: int = 7,
              weeks: int = 4) -> dict:
    """Minutes per day (last ``days`` days, today included) and per week (last ``weeks`` blocks
    of seven days ending today), in the person's own timezone. An hour counts on the local day it
    starts. Time the watch sent nothing for is ``not_recorded``, never filled in."""
    span_days = max(days, 7 * weeks)
    first = today - dt.timedelta(days=span_days - 1)
    per_day: dict[dt.date, dict] = {first + dt.timedelta(days=i): _empty() for i in range(span_days)}
    sources: set[str] = set()
    for r in rows:
        day = r["period_start"].astimezone(tz).date()
        if day not in per_day:
            continue
        sources.add(r["source"])
        seconds = r["seconds"] if isinstance(r["seconds"], dict) else json.loads(r["seconds"])
        acc = per_day[day]
        for label, s in seconds.items():
            if label == UNKNOWN:
                acc["unknown"] += s
            elif label == UNAVAILABLE:
                acc["unavailable"] += s
            else:
                acc[_group_of(label)] += s
    for day, acc in per_day.items():
        start = dt.datetime.combine(day, dt.time(0), tzinfo=tz)
        end = min(start + dt.timedelta(days=1), now.astimezone(tz)) if day == today else start + dt.timedelta(days=1)
        expected = max(0.0, (end - start).total_seconds())
        recorded = sum(v for k, v in acc.items() if k != "not_recorded")
        acc["not_recorded"] = max(0.0, expected - recorded)
    day_list = [{"date": d.isoformat(), "minutes": _minutes(per_day[d])}
                for d in sorted(per_day) if d > today - dt.timedelta(days=days)]
    week_list = []
    for w in range(weeks):
        end_day = today - dt.timedelta(days=7 * w)
        start_day = end_day - dt.timedelta(days=6)
        tot = _empty()
        for i in range(7):
            for k, v in per_day[start_day + dt.timedelta(days=i)].items():
                tot[k] += v
        week_list.append({"start": start_day.isoformat(), "end": end_day.isoformat(), "minutes": _minutes(tot)})
    return {"days": day_list, "weeks": list(reversed(week_list)), "sources": sorted(sources)}


async def summary_for(conn, person: Mapping, *, days: int = 7, weeks: int = 4,
                      now: Optional[dt.datetime] = None) -> dict:
    """What a carer sees: the trend when the consent is granted, and only the fact that it is not
    otherwise (no data is read)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        tz = ZoneInfo(person["timezone"])
    except Exception:  # noqa: BLE001 - an unknown zone name falls back to UTC, as reports do
        tz = ZoneInfo("UTC")
    version, sha, _ = known_model()
    out = {"label": LABEL, "sharing": False, "timezone": str(tz.key), "groups": dict(GROUP_WORDS),
           "model": {"version": version, "sha256": sha}, "days": [], "weeks": [], "sources": []}
    if not await sharing_for_person(conn, person["id"]):
        return out
    today = now.astimezone(tz).date()
    first = today - dt.timedelta(days=max(days, 7 * weeks))
    since = dt.datetime.combine(first, dt.time(0), tzinfo=tz)
    rows = [dict(r) for r in await conn.fetch(
        "SELECT period_start, source, seconds FROM care_activity_periods "
        "WHERE person_id = $1 AND period_start >= $2 ORDER BY period_start", person["id"], since)]
    out.update(summarise(rows, tz=tz, today=today, now=now, days=days, weeks=weeks))
    out["sharing"] = True
    return out


def device_settings(granted: bool) -> dict:
    """What GET /devices/me tells the watch."""
    return {"sharing": bool(granted)}
