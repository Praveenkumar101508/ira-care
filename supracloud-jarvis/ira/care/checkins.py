"""The check-in cycle: when to ask, what to ask, and what an answer meant.

Pure functions, so every rule here is testable without a database or a model:

* `in_quiet_hours`      is this local time inside the person's quiet hours?
* `next_checkin_at`     the next moment to ask: 15 to 20 minutes on (randomised,
                        per the person's settings), pushed past quiet hours
* `choose_question`     which kind of question comes next, from the person's
                        routines, medication times and what was asked recently
* `question_text`       the words: short, warm, one question, by name
* `summarise_answer`    a one-line summary and plain signals ("said yes",
                        "mentioned pain"), never a diagnosis

Wording is deliberately simple English for an older listener: short sentences,
no jargon, the person's preferred name, one question at a time. A language
model can rephrase a question (`care/checkin_cycle.py`, behind a setting that
is off by default); the templates here are what runs when it is off or
unavailable, and what the tests pin.
"""
from __future__ import annotations

import datetime as dt
import random
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

QUESTION_KINDS = ("meal", "medication", "mood", "pain", "plans", "wellbeing")

#: How close (minutes) a routine or medication time has to be for it to be
#: the thing we ask about.
NEAR_MINUTES = 60


def local_now(now: dt.datetime, timezone: str) -> dt.datetime:
    try:
        tz = ZoneInfo(timezone)
    except Exception:  # noqa: BLE001 - an unknown zone must not stop a check-in
        tz = ZoneInfo("UTC")
    return now.astimezone(tz)


def in_quiet_hours(local_time: dt.time, quiet_start: dt.time, quiet_end: dt.time) -> bool:
    """True inside [quiet_start, quiet_end). Handles a window across midnight."""
    if quiet_start == quiet_end:
        return False
    if quiet_start < quiet_end:
        return quiet_start <= local_time < quiet_end
    return local_time >= quiet_start or local_time < quiet_end


def quiet_hours_end(now_local: dt.datetime, quiet_start: dt.time, quiet_end: dt.time) -> dt.datetime:
    """The next moment quiet hours end, given `now_local` is inside them."""
    candidate = now_local.replace(hour=quiet_end.hour, minute=quiet_end.minute,
                                  second=0, microsecond=0)
    if candidate <= now_local:
        candidate += dt.timedelta(days=1)
    return candidate


def next_checkin_at(
    now: dt.datetime,
    settings: Mapping,
    timezone: str,
    rng: Optional[random.Random] = None,
) -> dt.datetime:
    """When to ask next: a random point `checkin_min_minutes` to
    `checkin_max_minutes` from now, moved to just after quiet hours if it would
    land inside them. Returns an aware UTC datetime."""
    rng = rng or random.Random()
    lo = int(settings["checkin_min_minutes"])
    hi = int(settings["checkin_max_minutes"])
    if hi < lo:
        hi = lo
    minutes = rng.uniform(lo, hi)
    when = now + dt.timedelta(minutes=minutes)
    when_local = local_now(when, timezone)
    if in_quiet_hours(when_local.time(), settings["quiet_start"], settings["quiet_end"]):
        after = quiet_hours_end(when_local, settings["quiet_start"], settings["quiet_end"])
        # Not on the dot: a few minutes in, so the first question of the day
        # does not arrive the second the quiet window closes.
        after += dt.timedelta(minutes=rng.uniform(1, lo))
        when = after.astimezone(dt.timezone.utc)
    return when.astimezone(dt.timezone.utc)


@dataclass(frozen=True)
class QuestionChoice:
    kind: str
    #: Why this kind: "medication_due", "meal_time", "rotation", "follow_up"
    reason: str
    #: The routine or medication this is about, if any
    subject: Optional[str] = None
    subject_id: Optional[str] = None
    #: Something recent worth following up on
    follow_up: Optional[str] = None


def _minutes_from(local_time: dt.time, other: dt.time) -> float:
    a = local_time.hour * 60 + local_time.minute
    b = other.hour * 60 + other.minute
    diff = abs(a - b)
    return min(diff, 1440 - diff)


def choose_question(
    now_local: dt.datetime,
    *,
    routines: Sequence[Mapping] = (),
    medications: Sequence[Mapping] = (),
    medication_events: Sequence[Mapping] = (),
    recent: Sequence[Mapping] = (),
    rng: Optional[random.Random] = None,
) -> QuestionChoice:
    """Pick the next question.

    Priority: a medication due within the hour that has no recorded dose today,
    then a meal routine due within the hour, then a follow-up on a recent answer
    that mentioned pain or feeling unwell, then a rotation through the kinds
    least recently asked.
    """
    rng = rng or random.Random()
    weekday = now_local.isoweekday()
    today = now_local.date()

    recorded_today = {
        (str(e["medication_id"]), e["due_at"].astimezone(now_local.tzinfo).time().replace(second=0, microsecond=0))
        for e in medication_events
        if e["due_at"].astimezone(now_local.tzinfo).date() == today
    }
    for med in medications:
        if not med.get("active", True):
            continue
        for t in med.get("times") or ():
            if _minutes_from(now_local.time(), t) <= NEAR_MINUTES and \
                    (str(med["id"]), t.replace(second=0, microsecond=0)) not in recorded_today:
                return QuestionChoice("medication", "medication_due", subject=med["name"],
                                      subject_id=str(med["id"]))

    for r in routines:
        if not r.get("active", True) or r.get("kind") != "meal" or not r.get("time_of_day"):
            continue
        if weekday in (r.get("days_of_week") or ()) and \
                _minutes_from(now_local.time(), r["time_of_day"]) <= NEAR_MINUTES:
            if not any(c["question_kind"] == "meal" and _asked_today(c, today, now_local.tzinfo)
                       and _minutes_from(c["scheduled_at"].astimezone(now_local.tzinfo).time(),
                                         r["time_of_day"]) <= NEAR_MINUTES
                       for c in recent):
                return QuestionChoice("meal", "meal_time", subject=r["name"], subject_id=str(r["id"]))

    for c in recent:
        summary = (c.get("answer_summary") or "").lower()
        if "pain" in summary or "unwell" in summary or "not good" in summary:
            if _asked_today(c, today, now_local.tzinfo) and c["question_kind"] != "pain":
                return QuestionChoice("pain", "follow_up", follow_up=c.get("answer_summary"))
            break

    asked = [c["question_kind"] for c in recent if c["question_kind"] in QUESTION_KINDS]
    rotation = [k for k in ("mood", "wellbeing", "plans", "pain", "meal") if k not in asked[:3]]
    if not rotation:
        rotation = ["mood", "wellbeing"]
    return QuestionChoice(rng.choice(rotation), "rotation")


def _asked_today(checkin: Mapping, today: dt.date, tz) -> bool:
    when = checkin.get("scheduled_at")
    return bool(when) and when.astimezone(tz).date() == today


_GREETINGS = ("Hello {name}.", "Hi {name}.", "{name}, it's me.")

_TEMPLATES = {
    "meal": ("Have you had something to eat?", "Did you have your {subject} yet?"),
    "medication": ("Have you taken your {subject} yet?",),
    "mood": ("How are you feeling right now?", "How is your morning going?"),
    "pain": ("Any aches or pains today, {name}?", "How is the {follow_up_short} now?"),
    "plans": ("What are you up to this afternoon?", "Anything planned for today?"),
    "wellbeing": ("How are you doing?", "Everything all right with you?"),
    "custom": ("{custom}",),
}


def question_text(choice: QuestionChoice, preferred_name: str, *, time_of_day: Optional[dt.time] = None,
                  rng: Optional[random.Random] = None, custom: str = "") -> str:
    """One greeting, one question. Short, warm, by name."""
    rng = rng or random.Random()
    name = (preferred_name or "").strip() or "there"
    greeting = rng.choice(_GREETINGS).format(name=name)
    options = _TEMPLATES.get(choice.kind, _TEMPLATES["wellbeing"])
    if choice.kind == "meal":
        q = options[1] if choice.subject else options[0]
    elif choice.kind == "medication":
        q = options[0]
    elif choice.kind == "pain":
        q = options[1] if choice.follow_up else options[0]
    elif choice.kind == "mood" and time_of_day is not None:
        q = options[1] if time_of_day.hour < 12 else options[0]
    elif choice.kind == "plans" and time_of_day is not None:
        q = options[0] if 12 <= time_of_day.hour < 17 else options[1]
    else:
        q = options[0]
    q = q.format(subject=(choice.subject or "").lower(), follow_up_short=_short(choice.follow_up),
                 custom=custom or "How are you?", name=name)
    return f"{greeting} {q}".strip()


def _short(text: Optional[str]) -> str:
    if not text:
        return "pain"
    text = text.lower()
    for word in ("back", "knee", "hip", "head", "chest", "leg", "arm", "stomach"):
        if word in text:
            return f"{word} pain"
    return "pain"


# ── Understanding the answer ─────────────────────────────────────────────────

_YES = re.compile(r"\b(yes|yeah|yep|i have|i did|already|taken|took|done|all done|just now)\b")
_NO = re.compile(r"\b(no|not yet|haven'?t|havent|forgot|didn'?t|didnt|later|nope)\b")
_PAIN = re.compile(r"\b(pain|hurts?|hurting|ache|aches|aching|sore|dizzy|breathless|fell|fall|fallen)\b")
_LOW = re.compile(r"\b(sad|lonely|tired|not good|not great|unwell|poorly|rough|awful|terrible|worried|anxious|down)\b")
_GOOD = re.compile(r"\b(good|fine|well|great|ok|okay|lovely|happy|better|not bad|alright|all right)\b")
_HELP = re.compile(r"\b(help|emergency|ambulance|can'?t get up|cant get up)\b")


@dataclass(frozen=True)
class AnswerReading:
    summary: str
    #: "yes", "no" or "unclear" for yes/no questions; "" otherwise
    said: str = ""
    #: Plain observations, never a judgement: "mentioned pain", "sounded low"
    signals: tuple[str, ...] = field(default_factory=tuple)
    #: The person asked for help in words
    asked_for_help: bool = False


def summarise_answer(kind: str, text: str, *, subject: Optional[str] = None) -> AnswerReading:
    raw = (text or "").strip()
    lowered = raw.lower()
    signals: list[str] = []
    if _PAIN.search(lowered):
        signals.append("mentioned pain or discomfort")
    if _LOW.search(lowered):
        signals.append("sounded low")
    if _HELP.search(lowered):
        signals.append("asked for help")
    said = ""
    if kind in ("meal", "medication"):
        yes, no = bool(_YES.search(lowered)), bool(_NO.search(lowered))
        said = "yes" if yes and not no else "no" if no and not yes else "unclear"
    if not raw:
        return AnswerReading(summary="No answer.", said=said or "", signals=tuple(signals))
    what = subject or ("a meal" if kind == "meal" else "medication")
    if kind == "medication":
        base = {"yes": f"Said they had taken {what}.", "no": f"Said they had not taken {what} yet.",
                "unclear": f"Answer about {what} was unclear."}[said]
    elif kind == "meal":
        base = {"yes": "Said they had eaten.", "no": "Said they had not eaten yet.",
                "unclear": "Answer about eating was unclear."}[said]
    elif _GOOD.search(lowered) and not signals:
        base = "Said they were doing well."
    else:
        # Never the person's own words: a summary is what family see without
        # consent and what travels in reports. The words stay in the transcript.
        base = {"mood": "Answered about how they feel.", "pain": "Answered about aches and pain.",
                "plans": "Answered about their plans.", "wellbeing": "Answered about how they are doing."
                }.get(kind, "Gave an answer.")
    summary = base if not signals else f"{base} ({'; '.join(signals)})"
    return AnswerReading(summary=summary[:400], said=said, signals=tuple(signals),
                         asked_for_help=bool(_HELP.search(lowered)))


def missed_in_a_row(recent: Iterable[Mapping]) -> int:
    """Consecutive missed check-ins, newest first, stopping at the first that
    was answered. Quiet-hour skips and cancellations do not break the run."""
    n = 0
    for c in recent:
        status = c["status"]
        if status in ("missed", "escalated"):
            n += 1
        elif status in ("skipped_quiet", "cancelled", "scheduled", "asked", "repeated"):
            continue
        else:
            break
    return n
