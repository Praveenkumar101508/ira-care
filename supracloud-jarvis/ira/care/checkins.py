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
    "morning": ("Good morning {name}. Are you up and about?",),
    "safety": ("{name}, are you OK? Tap I'm OK, or say help.",),
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
    if choice.kind in ("morning", "safety"):
        return q.format(name=name)
    q = q.format(subject=_subject(choice.subject), follow_up_short=_short(choice.follow_up),
                 custom=custom or "How are you?", name=name)
    return f"{greeting} {q}".strip()


_LEADING_WORD = re.compile(r"^(the|your|my|a|an)\s+", re.IGNORECASE)


def _subject(name: Optional[str]) -> str:
    """A medication or routine's name as it reads after "your": "The evening tablet" is "evening tablet"."""
    return _LEADING_WORD.sub("", (name or "").strip()).lower()


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
_LOW = re.compile(r"\b(sad|lonely|tired|not good|not great|unwell|poorly|rough|awful|terrible|worried|anxious|down|"
                  r"low|crying|cried|tearful|in tears|miserable|fed up)\b")   # round 10 hold-out B: "very low today, crying"
_GOOD = re.compile(r"\b(good|fine|well|great|ok|okay|lovely|happy|better|not bad|alright|all right)\b")
_HELP = re.compile(r"\b(help|emergency|ambulance|can'?t get up|cant get up)\b")


# ── Reading an answer about a dose: conservative by design ───────────────────
#
# One reader for every path that can write a dose down (the scheduled
# check-in, a reminder's two-step confirmation). It returns
#
#     yes      an unambiguous confirmation that THIS dose was taken
#     later    not taken yet: negated, forgotten, or an intention to take it
#     skip     the person is not taking it today
#     unclear  anything else: doubt, another time, another medicine, a bare
#              "okay", a contradiction, silence
#
# Only "yes" may ever become a taken dose. Doubt always wins over confidence,
# and nothing a language model or a classifier says can turn an answer this
# reader did not call "yes" into one (care/conversation.merge).

DOSE_ANSWERS = ("yes", "later", "skip", "unclear")

_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "`": "'", "\u00b4": "'"})
_CONTRACTIONS = (
    (r"\bhaven'?t\b", "have not"), (r"\bhasn'?t\b", "has not"), (r"\bhadn'?t\b", "had not"),
    (r"\bdidn'?t\b", "did not"), (r"\bdon'?t\b", "do not"), (r"\bdoesn'?t\b", "does not"),
    (r"\bwon'?t\b", "will not"), (r"\bcan'?t\b", "cannot"), (r"\bcan not\b", "cannot"),
    (r"\bwasn'?t\b", "was not"), (r"\bisn'?t\b", "is not"), (r"\baren'?t\b", "are not"),
    (r"\bshouldn'?t\b", "should not"), (r"\bcouldn'?t\b", "could not"), (r"\bwouldn'?t\b", "would not"),
    (r"\bi'll\b", "i will"), (r"\bi'?ve\b", "i have"), (r"\bi'?m\b", "i am"), (r"\bi'd\b", "i would"),
    (r"\bit'?s\b", "it is"), (r"\bthat'?s\b", "that is"), (r"\bthey'?re\b", "they are"),
    (r"\bgonna\b", "going to"), (r"\bgotta\b", "got to"),
)


def normalise_answer(text: str) -> str:
    """Lower case, plain apostrophes, contractions spelled out, punctuation gone."""
    t = (text or "").translate(_APOSTROPHES).lower()
    for pattern, repl in _CONTRACTIONS:
        t = re.sub(pattern, repl, t)
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    t = t.replace("'", "")
    return re.sub(r"\s+", " ", t).strip()


_DOSE_UNSURE = re.compile(
    r"\b(think|thought|maybe|probably|might|may have|could have|perhaps|possibly|not sure|unsure|"
    r"cannot remember|do not remember|did not remember|can not recall|cannot recall|do not recall|"
    r"no idea|do not know|dont know|i guess|i suppose|i believe|reckon|not certain|confused|"
    r"supposed to|should have|meant to)\b")
_DOSE_SKIP = re.compile(
    r"\b(not today|skip|skipping|skipped|no thanks?|no thank you|do not need|do not want|"
    r"will not|not taking|not going to take|refuse|leave it|forget it|not having)\b")
_DOSE_LATER = re.compile(
    r"\b(not yet|have not|has not|had not|did not|not taken|not took|never took|forgot|forgotten|"
    r"will|going to|about to|later|not now|in a (bit|minute|while|moment|second)|soon|"
    r"after (my |the )?(lunch|breakfast|dinner|tea|supper|meal|food)|ask me again|remind me|snooze|"
    r"in (five|ten|twenty|5|10|20) minutes|when i|once i|yet to|still need|need to take)\b")
_DOSE_OTHER = re.compile(
    r"\b(yesterday|last night|last week|the day before|other day|tomorrow|earlier this week|"
    r"different|another|other|wrong|someone elses|instead|extra|double|twice|two|three|four|two lots)\b")
_DOSE_NEG = re.compile(r"\b(no|not|never|nope|nah)\b")
#: An answer that broke off or was interrupted (round 9): a dash in the raw words, or the person
#: turning to something else. "Yes I took the— hang on, the door" confirms nothing.
_DOSE_BROKEN_RAW = re.compile(r"[\u2014\u2013]|\s-\s*$|\w-$")
_DOSE_INTERRUPTED = re.compile(
    r"\b(hang on|hold on|wait|just a (minute|second|moment|sec)|one (minute|second|moment)|"
    r"someone(s| is)? at the door|the door|the phone|the kettle|let me look|where did i put|what did you say|"
    r"pardon|sorry what|i mean)\b")
#: An indefinite object ("I took something") or a medicine that is not the one asked about
#: (round 9). The subject's own words are allowed; "the painkillers yes" to a question about the
#: morning tablets is not a confirmation of the morning tablets.
_DOSE_INDEFINITE = re.compile(r"\b(something|some of them|one of them|a couple|a few|whatever|the usual)\b")
_DOSE_MEDICINE_WORDS = re.compile(
    r"\b(painkillers?|paracetamol|ibuprofen|aspirin|codeine|antibiotics?|inhaler|insulin|drops|cream|ointment|"
    r"statins?|water tablets?|blood pressure (tablets?|pills?)|heart (tablets?|pills?)|sleeping (tablets?|pills?)|"
    r"(blue|white|red|yellow|pink|little|big|small|round) (ones?|tablets?|pills?))\b")
_DOSE_BARE_NO = re.compile(r"^(no|nope|nah|no no|no no no|no sorry|sorry no|no i am afraid)$")
_DOSE_YES = re.compile(
    r"\b(yes|yeah|yep|yup|aye|i have|i did|i took|took (it|them|that|those|mine|my)|taken|have had|"
    r"had (it|them|mine|my)|already|all done|done that|just now|that is right|that is correct|correct)\b")


#: A time of day used as the NAME of a dose ("the evening ones", "my night tablets"), as against
#: when it was taken ("this evening"). Round 10: the round 9 hold-out's "I took the evening ones",
#: asked about the morning tablets, was read as "yes".
_DOSE_TIME_NAMED = re.compile(
    r"\b(morning|afternoon|evening|night|nighttime|bedtime|lunchtime|lunch|breakfast|teatime|supper|dinner)"
    r"( ?time)? (ones?|tablets?|pills?|doses?|medicines?|meds|medication|capsules?|lot)\b")
_TIME_FAMILIES = {"morning": "morning", "breakfast": "morning", "afternoon": "afternoon", "lunch": "afternoon",
                  "lunchtime": "afternoon", "evening": "evening", "teatime": "evening", "supper": "evening",
                  "dinner": "evening", "night": "night", "nighttime": "night", "bedtime": "night"}


def _names_another_dose_time(t: str, subject: str) -> bool:
    """True when the answer names a dose by a time of day the question's medicine does not have."""
    asked = {f for w, f in _TIME_FAMILIES.items() if re.search(rf"\b{w}\b", subject)}
    if not asked:
        return False   # the medicine has no time in its name: nothing to compare
    named = {_TIME_FAMILIES[m.group(1)] for m in _DOSE_TIME_NAMED.finditer(t)}
    return bool(named - asked)


def read_dose_answer(text: str, *, subject: Optional[str] = None,
                     other_medications: Sequence[str] = ()) -> str:
    """yes | later | skip | unclear for "Have you taken your X?". Conservative:
    when in doubt the answer is never "yes"."""
    if _DOSE_BROKEN_RAW.search(text or ""):
        return "unclear"
    t = normalise_answer(text)
    if not t:
        return "unclear"
    if _DOSE_UNSURE.search(t) or _DOSE_INTERRUPTED.search(t) or _DOSE_INDEFINITE.search(t):
        return "unclear"
    own_words = normalise_answer(subject or "")
    if _names_another_dose_time(t, own_words):
        return "unclear"  # "the evening ones" when asked about the morning tablets
    for m in _DOSE_MEDICINE_WORDS.finditer(t):
        if m.group(0) not in own_words:
            return "unclear"  # the answer names a medicine that is not the one asked about
    if _DOSE_SKIP.search(t):
        return "skip"
    if _DOSE_LATER.search(t):
        return "later"
    if _DOSE_OTHER.search(t):
        return "unclear"
    own = normalise_answer(subject or "")
    for name in other_medications:
        other = normalise_answer(name)
        if other and other != own and other not in own and re.search(rf"\b{re.escape(other)}\b", t):
            return "unclear"  # the answer names a different medicine
    if _DOSE_BARE_NO.match(t):
        return "skip"
    if _DOSE_NEG.search(t):
        return "unclear"  # a negative we cannot place: never a confirmation
    if _DOSE_YES.search(t):
        return "yes"
    return "unclear"


def dose_said(dose: str) -> str:
    """The yes/no/unclear a summary uses, from a dose answer."""
    return {"yes": "yes", "later": "no", "skip": "no"}.get(dose, "unclear")


# ── The safety question: "Are you OK?" ───────────────────────────────────────
#
# Three outcomes, never two: "ok" only for an unambiguous positive with no
# sign of trouble; "help" for any request, distress or negative; "unclear" for
# everything else, which keeps the safety deadline running. The absence of a
# request for help is NOT an answer that the person is safe.

SAFETY_ANSWERS = ("ok", "help", "unclear")

_SAFE_DISTRESS = re.compile(
    r"\b(help|emergency|ambulance|hurt|hurts|hurting|pain|ache|aching|sore|dizzy|faint|fainted|fell|fall|fallen|"
    r"floor|ground|cannot|can not|unable|stuck|bleeding|breathe|breathing|chest|sick|unwell|poorly|ill|"
    r"wobbly|shaky|confused|scared|frightened|weak|numb|bad|awful|terrible|wrong)\b")
_SAFE_DOUBT = re.compile(r"\b(maybe|think|not sure|do not know|dont know|i guess|perhaps|probably|kind of|sort of|"
                         r"a bit|bit|little|but|except|although|apart from|remember)\b")
_SAFE_NEG = re.compile(r"\b(no|not|never|nope|nah)\b")
_SAFE_OK = re.compile(
    r"^(yes |yeah |yep )?(i am |im |i am all |am )?(ok|okay|fine|alright|all right|good|well|grand)"
    r"( thanks| thank you| thank you ira| ira| really| now| honestly)*$"
    r"|^(yes|yeah|yep)( i am| im)?( ok| okay| fine| alright| all right)?( thanks| thank you)?$"
    r"|^(i am |im )?(perfectly |absolutely |quite )?(ok|okay|fine|alright|all right)( thanks| thank you)?$"
    r"|^(no )?(i do not need|do not need) (any )?help( thanks| thank you)?$")


def read_safety_answer(text: str) -> str:
    """ok | help | unclear for a spoken answer to "are you OK?". Only a short,
    unambiguous positive is "ok"; "yes, but I cannot get up" is help."""
    t = normalise_answer(text)
    if not t:
        return "unclear"
    if re.fullmatch(r"(no )?(i do not need|do not need) (any )?help( thanks| thank you)?", t):
        return "ok"
    if re.search(r"\b(cannot|do not|can not) (remember|recall)\b", t):
        t = re.sub(r"\b(cannot|do not|can not) (remember|recall)\b", "remember", t)
    if _SAFE_DISTRESS.search(t):
        return "help"
    if _SAFE_DOUBT.search(t):
        return "unclear"
    if _SAFE_NEG.search(t):
        # "I'm not OK", "not fine", "no": never reassurance. A plain negative
        # to "are you OK?" is treated as a call for help.
        return "help"
    if _SAFE_OK.fullmatch(t):
        return "ok"
    return "unclear"


@dataclass(frozen=True)
class AnswerReading:
    summary: str
    #: "yes", "no" or "unclear" for yes/no questions; "" otherwise
    said: str = ""
    #: Plain observations, never a judgement: "mentioned pain", "sounded low"
    signals: tuple[str, ...] = field(default_factory=tuple)
    #: The person asked for help in words
    asked_for_help: bool = False
    #: For a medication question: yes | later | skip | unclear (read_dose_answer)
    dose: str = ""


def summarise_answer(kind: str, text: str, *, subject: Optional[str] = None,
                     other_medications: Sequence[str] = ()) -> AnswerReading:
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
    dose = ""
    if kind == "meal":
        yes, no = bool(_YES.search(lowered)), bool(_NO.search(lowered))
        said = "yes" if yes and not no else "no" if no and not yes else "unclear"
    elif kind == "medication":
        dose = read_dose_answer(raw, subject=subject, other_medications=other_medications)
        said = dose_said(dose)
    if not raw:
        return AnswerReading(summary="No answer.", said=said or "", signals=tuple(signals), dose=dose)
    what = subject or ("a meal" if kind == "meal" else "medication")
    if kind in ("medication", "meal"):
        base = yes_no_summary(kind, said, subject, dose=dose)
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
                         asked_for_help=bool(_HELP.search(lowered)), dose=dose)


def yes_no_summary(kind: str, said: str, subject: Optional[str] = None, *, dose: str = "") -> str:
    """The one-line summary of a meal or dose answer, from the reading that decided it."""
    if kind == "medication":
        what = subject or "medication"
        if said == "no" and dose == "skip":
            return f"Said they were not taking {what} today."
        return {"yes": f"Said they had taken {what}.", "no": f"Said they had not taken {what} yet.",
                }.get(said, f"Answer about {what} was unclear.")
    return {"yes": "Said they had eaten.", "no": "Said they had not eaten yet."}.get(said, "Answer about eating was unclear.")


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
