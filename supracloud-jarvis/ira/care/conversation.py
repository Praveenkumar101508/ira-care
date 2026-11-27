"""IRA talks: the care conversation service.

One place that composes check-in questions, reads answers into a fixed
structure, and holds a free conversation ("Talk to IRA"), all through a
`Model` adapter. The default (`IRA_CARE_MODEL=ollama`, or unset) is a chain of
two free open-weight models served by Ollama on the server itself
(`IRA_CARE_MODEL_PRIMARY`, then `IRA_CARE_MODEL_SECONDARY`; docs/MODEL_CHOICE.md),
with the set questions as the last resort. `fake` is the deterministic model
CI and the tests use; `off` means templates only; `local` is IRA's older model
layer. No hosted model API exists anywhere in this path: every word stays on
the operator's own machine.

Every model reply passes `guard()`: no diagnosis words, no advice, bounded
length, one question at most for a check-in. A reply that fails gets a safe
fallback line. The template path takes over whenever the model fails or its
first sentence is slower than `IRA_CARE_MODEL_FIRST_SENTENCE_S` (default 4).

Safety signals are never the model's alone: the keyword rules in
`care/checkins.py` run on every answer and the stricter reading wins.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Mapping, Optional, Protocol, Sequence

from pydantic import BaseModel, Field, ValidationError, field_validator

from . import checkins as rules
from .monitoring import no_diagnosis_words

logger = logging.getLogger("ira.care.conversation")

DEFAULT_LANGUAGE = "en-GB"
FIRST_SENTENCE_S = 12.0


# ── The model adapter ────────────────────────────────────────────────────────

class Model(Protocol):
    name: str

    def stream(self, messages: list[dict], *, max_tokens: int, temperature: float) -> AsyncIterator[str]: ...

    async def complete_json(self, messages: list[dict], *, max_tokens: int) -> str: ...


class FakeModel:
    """Deterministic, instant, and shaped like a small model would be. CI and
    the tests use it; `slow_s` and `broken` simulate a slow or failing model."""
    name = "fake"

    def __init__(self, *, slow_s: float = 0.0, broken: bool = False, reply: Optional[str] = None,
                 json_reply: Optional[dict] = None):
        self.slow_s, self.broken, self.reply, self.json_reply = slow_s, broken, reply, json_reply
        self.calls: list[list[dict]] = []

    async def stream(self, messages, *, max_tokens, temperature):
        self.calls.append(messages)
        if self.broken:
            raise RuntimeError("fake model is broken")
        if self.slow_s:
            await asyncio.sleep(self.slow_s)
        text = self.reply if self.reply is not None else self._compose(messages)
        for word in text.split(" "):
            yield word + " "

    async def complete_json(self, messages, *, max_tokens):
        self.calls.append(messages)
        if self.broken:
            raise RuntimeError("fake model is broken")
        if self.json_reply is not None:
            return json.dumps(self.json_reply)
        user = messages[-1]["content"]
        answer = user.rsplit("Answer:", 1)[-1].strip().lower() if "Answer:" in user else ""
        return json.dumps({
            "wellbeing": "poor" if re.search(r"\b(awful|terrible|unwell|poorly|bad)\b", answer) else
                         "good" if re.search(r"\b(fine|good|well|great|ok|okay)\b", answer) else "unknown",
            "pain_mentioned": bool(re.search(r"\b(pain|hurt|ache|sore)\b", answer)),
            "medication_taken": "yes" if re.search(r"\b(yes|taken|took|did)\b", answer) else
                                "no" if re.search(r"\b(no|not yet|forgot|haven't)\b", answer) else "unknown",
            "needs_help": bool(re.search(r"\b(help|can't get up|cant get up)\b", answer)),
            "mood_words": [w for w in ("cheerful", "tired", "lonely", "worried", "content") if w in answer],
            "summary": "Answered the question.",
        })

    @staticmethod
    def _compose(messages) -> str:
        user = messages[-1]["content"]
        everything = "\n".join(m.get("content", "") for m in messages)
        name = re.search(r"Name: ([^\n.]+)", everything)
        n = name.group(1).strip() if name else "there"
        if "Talk:" in user:
            return f"I'm here, {n}. Tell me more about that."
        due = re.search(r"Medication due now: ([^\n;.]+)", everything)
        if due:
            return f"Hello {n}. Have you had {due.group(1).strip()} yet?"
        return f"Good to hear from you, {n}. How are you feeling today?"


class LayerModel:
    """IRA's own model layer: the fast tier, a small local model by default."""
    name = "layer"

    async def stream(self, messages, *, max_tokens, temperature):
        from utils.llm import stream_tokens
        async for tok in stream_tokens(messages, max_tokens=max_tokens, temperature=temperature):
            yield tok

    async def complete_json(self, messages, *, max_tokens):
        from utils.llm import chat_complete
        out = await chat_complete(messages, max_tokens=max_tokens, temperature=0.0,
                                  response_format={"type": "json_object"})
        return out if isinstance(out, str) else ""


class OllamaModel:
    """One open-weight model served by Ollama on this machine. Streams tokens
    from `/api/chat`; asks for JSON with `format: json` when a structure is wanted."""

    def __init__(self, name: str, base_url: Optional[str] = None, *, timeout_s: float = 60.0, transport=None) -> None:
        self.name = name
        self.base_url = (base_url or os.getenv("IRA_CARE_OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
        self.timeout_s = timeout_s
        self._transport = transport   # tests hand in an httpx.MockTransport

    @staticmethod
    def _load_options() -> dict:
        """The options that decide how the model is loaded. Every request must send the same
        ones: a request with a different context size makes Ollama load the model again
        ("runner options changed"), which takes longer than a turn may wait (D-073)."""
        opts = {"num_ctx": context_window()}
        threads = _int_env("IRA_CARE_MODEL_THREADS")
        if threads:
            opts["num_thread"] = threads
        return opts

    def _options(self, max_tokens: int, temperature: float) -> dict:
        return {"num_predict": max_tokens, "temperature": temperature, **self._load_options()}

    def _body(self, messages, *, stream: bool, max_tokens: int, temperature: float) -> dict:
        # keep_alive: the model stays loaded between turns, so no turn pays the
        # load (about 10 to 15 s on four cores) again (round 4, D-069).
        return {"model": self.name, "messages": messages, "stream": stream, "think": False,
                "keep_alive": keep_alive(), "options": self._options(max_tokens, temperature)}

    async def stream(self, messages, *, max_tokens, temperature):
        import httpx
        body = self._body(messages, stream=True, max_tokens=max_tokens, temperature=temperature)
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s, connect=5.0), transport=self._transport) as client:
            async with client.stream("POST", f"{self.base_url}/api/chat", json=body) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    tok = (obj.get("message") or {}).get("content") or ""
                    if tok:
                        yield tok
                    if obj.get("done"):
                        break

    async def complete_json(self, messages, *, max_tokens):
        import httpx
        body = {**self._body(messages, stream=False, max_tokens=max_tokens, temperature=0.0), "format": "json"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s, connect=5.0), transport=self._transport) as client:
            r = await client.post(f"{self.base_url}/api/chat", json=body)
            r.raise_for_status()
            return ((r.json().get("message") or {}).get("content") or "").strip()


    async def warm(self) -> float:
        """Load the model (or refresh its keep-alive) with an empty request.
        Returns the seconds it took: about 0.05 s when it was already loaded."""
        import httpx
        t0 = time.monotonic()
        body = {"model": self.name, "keep_alive": keep_alive(), "options": self._load_options()}
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s, connect=5.0), transport=self._transport) as client:
            r = await client.post(f"{self.base_url}/api/generate", json=body)
            r.raise_for_status()
        return time.monotonic() - t0

    async def loaded(self) -> Optional[bool]:
        """Is this model in memory now? Asks Ollama's `/api/ps` (a few milliseconds);
        None when Ollama cannot say, so the caller tries the model as before."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=1.0), transport=self._transport) as client:
                r = await client.get(f"{self.base_url}/api/ps")
                r.raise_for_status()
                names = {m.get("name") or m.get("model") for m in (r.json().get("models") or [])}
        except Exception:  # noqa: BLE001 - unknown is not the same as cold
            return None
        return self.name in names


class ChainModel:
    """The chosen model, then the second model, then (by the callers) the set
    questions. A model that fails, or that has not produced its first token
    within `first_token_s`, hands over to the next. `name` is the model that
    answered last, so transcripts and the timeline say which one spoke."""

    def __init__(self, models: Sequence[Model], *, first_token_s: Optional[float] = None) -> None:
        if not models:
            raise ValueError("a chain needs at least one model")
        self.models = list(models)
        self.first_token_s = first_token_s if first_token_s is not None else first_token_budget_s()
        self.name = self.models[0].name
        self.last_error: str = ""
        self._warming: Optional[asyncio.Task] = None

    async def warm(self) -> float:
        """Load the primary only: on a 16 GB machine the second model would
        push the first out (OLLAMA_MAX_LOADED_MODELS is 1 in the deployment)."""
        first = self.models[0]
        warm = getattr(first, "warm", None)
        return await warm() if warm else 0.0

    async def _ready(self, model) -> bool:
        """Only a model already in memory is asked (D-073). Starting a cold one
        inside a turn's few seconds aborts its load when the turn gives up, and
        the next model's load pushes the first out again: neither ever loads.
        A cold chain answers from the set lines at once and loads its primary
        in the background, whole, for the next turn."""
        loaded = getattr(model, "loaded", None)
        if loaded is None:
            return True
        state = await loaded()
        if state is not False:
            return True
        self.last_error = f"{model.name}: not loaded"
        self._warm_in_background()
        return False

    def _warm_in_background(self) -> None:
        if self._warming is not None and not self._warming.done():
            return

        async def warm() -> None:
            try:
                await self.warm()
            except Exception as exc:  # noqa: BLE001 - the next turn will try again
                logger.info("care conversation: background load failed (%s)", type(exc).__name__)
        self._warming = asyncio.create_task(warm())

    @property
    def first_sentence_budget(self) -> float:
        """Enough for every model in the chain to be given its chance."""
        return self.first_token_s * len(self.models) + first_sentence_budget_s()

    async def stream(self, messages, *, max_tokens, temperature):
        last: Optional[BaseException] = None
        for model in self.models:
            if not await self._ready(model):
                continue
            it = model.stream(messages, max_tokens=max_tokens, temperature=temperature).__aiter__()
            try:
                first = await asyncio.wait_for(it.__anext__(), timeout=self.first_token_s)
            except StopAsyncIteration:
                last = RuntimeError(f"{model.name}: empty reply")
                continue
            except Exception as exc:  # noqa: BLE001 - this model is skipped, the next tried
                last = exc
                self.last_error = f"{model.name}: {type(exc).__name__}"
                logger.info("care conversation: %s did not answer in time (%s); trying the next model", model.name, type(exc).__name__)
                continue
            self.name = model.name
            yield first
            async for tok in it:
                yield tok
            return
        raise RuntimeError(f"no model in the chain answered ({self.last_error or last})")

    async def complete_json(self, messages, *, max_tokens):
        last: Optional[BaseException] = None
        for model in self.models:
            if not await self._ready(model):
                continue
            try:
                out = await asyncio.wait_for(model.complete_json(messages, max_tokens=max_tokens),
                                             timeout=self.first_token_s * 3)
            except Exception as exc:  # noqa: BLE001
                last = exc
                self.last_error = f"{model.name}: {type(exc).__name__}"
                continue
            if out:
                self.name = model.name
                return out
        raise RuntimeError(f"no model in the chain answered ({self.last_error or last})")


#: The free open-weight models the chain uses by default (docs/MODEL_CHOICE.md, round 4).
DEFAULT_PRIMARY = "gemma4:e4b"
DEFAULT_SECONDARY = "gemma4:e2b"
#: How long the primary is given for its first token before the chain moves on. The
#: instant acknowledgement (D-070) is already spoken, so waiting for the model's own words is
#: better than the set line: a whole talk prompt (about 270 tokens) is read again on every turn
#: (no prompt cache with Gemma 4's sliding window) and takes 3 to 6 s on four cores (D-073).
FIRST_TOKEN_S = 8.0
#: How long Ollama keeps a model loaded after a turn, and the context window a turn is given.
DEFAULT_KEEP_ALIVE = "30m"
DEFAULT_NUM_CTX = 1024

_override: Optional[Model] = None


def _int_env(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def keep_alive() -> str:
    return (os.getenv("IRA_CARE_MODEL_KEEP_ALIVE") or DEFAULT_KEEP_ALIVE).strip()


def context_window() -> int:
    return _int_env("IRA_CARE_MODEL_NUM_CTX", DEFAULT_NUM_CTX) or DEFAULT_NUM_CTX


async def warm_configured_model() -> Optional[float]:
    """Load the configured model so the first person to speak does not wait for
    it. Called at server start and by the worker every few minutes; harmless
    with the fake model or templates only (returns None)."""
    model = configured_model()
    warm = getattr(model, "warm", None)
    if model is None or warm is None:
        return None
    try:
        took = await warm()
    except Exception as exc:  # noqa: BLE001 - a cold model is slow, not broken
        logger.info("care conversation: could not warm the model (%s)", type(exc).__name__)
        return None
    logger.info("care conversation: model %s warm in %.2f s", getattr(model, "name", "?"), took)
    return took


def first_token_budget_s() -> float:
    try:
        return float(os.getenv("IRA_CARE_MODEL_FIRST_TOKEN_S") or FIRST_TOKEN_S)
    except ValueError:
        return FIRST_TOKEN_S


def chain_from_env() -> ChainModel:
    primary = (os.getenv("IRA_CARE_MODEL_PRIMARY") or DEFAULT_PRIMARY).strip()
    secondary = (os.getenv("IRA_CARE_MODEL_SECONDARY") or DEFAULT_SECONDARY).strip()
    models: list[Model] = [OllamaModel(primary)]
    if secondary and secondary.lower() not in ("off", "none", "") and secondary != primary:
        models.append(OllamaModel(secondary))
    return ChainModel(models)


def configured_model() -> Optional[Model]:
    """The model to use, or None for templates only. Tests inject one with `use_model`."""
    if _override is not None:
        return _override
    choice = (os.getenv("IRA_CARE_MODEL") or "ollama").strip().lower()
    if choice in ("off", "none", "0", "false"):
        return None
    if choice == "fake":
        return FakeModel()
    if choice == "local":
        return LayerModel()
    return chain_from_env()


def use_model(model: Optional[Model]) -> None:
    """Test hook: pin the model for the process."""
    global _override
    _override = model


def first_sentence_budget_s() -> float:
    try:
        return float(os.getenv("IRA_CARE_MODEL_FIRST_SENTENCE_S") or FIRST_SENTENCE_S)
    except ValueError:
        return FIRST_SENTENCE_S


# ── Guardrails ───────────────────────────────────────────────────────────────

_ADVICE = re.compile(
    r"\b(you should (take|stop|increase|reduce|double)|take (one|two|three|another|an extra|more)\b.*\b(tablet|pill|dose)|"
    r"increase your|reduce your|double your|stop taking|it is safe to|it's safe to|you need to see|see a doctor|"
    r"call (999|911|112)|dial (999|911|112)|i (would )?recommend|i advise|symptoms? of)", re.I)

# Telling the person what they have. Until round 7 any "you have" was refused,
# which also refused "I hope you have a peaceful evening" and the product's own
# "Did you have your lunch yet?". The rule now refuses any "you have" it cannot
# place as plainly social (`_tells_them_what_they_have`):
#
# 1. "you have" / "you've got" with a word about health anywhere after it in
#    the sentence is refused, whatever comes before it ("I hope you have a
#    fever" too).
# 2. A hedged or inferred "you have" ("you may have", "you probably have",
#    "sounds like you have", "I think you've got") is refused unless what
#    follows is plainly social ("you must have been so proud", "sounds like
#    you've had a lovely rest"). "You may have missed the bus" is refused: the
#    guard cannot tell it from "you may have broken your hip".
# 3. A plain "you have" passes only after a wish or a question ("I hope you
#    have", "Did you have"), or before something plainly social ("you have a
#    beautiful garden", "you have family visiting", "you have done so well").
# A health word missing from `_HEALTH` is still refused by 2 and 3 unless the
# sentence is social in shape; the list only has to cover social-shaped
# sentences that mention health.
_HAVE = re.compile(r"\byou( have( got)?|'ve got|'ve)\b", re.I)
_HEDGE = re.compile(
    r"\byou (probably|possibly|likely|maybe|may|might|could|must) (have( got)?|'ve got)\b|"
    r"\byou'?ve (probably|possibly|likely) got\b|"
    r"\b(sounds?|looks?|seems?|appears?) (like|as if|as though) you"
    r"( (probably|possibly|likely|may|might|could|must))?( have( got)?|'ve( got)?)\b|"
    r"\b(i think|i suspect|i believe|i expect|i'm afraid|i am afraid|i fear|i'm sure|i am sure|i bet|"
    r"perhaps|maybe|probably|likely|clearly|obviously|definitely|certainly) you( have( got)?|'ve( got)?)\b", re.I)
_HEALTH = re.compile(
    r"\b(pain\w*|ache\w*|aching|sore\w*|headache\w*|migraine\w*|fever\w*|temperature|chills?|infect\w*|virus\w*|viral|"
    r"bacteri\w*|flu|influenza|covid\w*|coronavirus|cold|colds|cough\w*|chest|lungs?|pneumonia|bronchitis|asthma|copd|"
    r"diabet\w*|sugar|glucose|insulin|blood|pressure|heart\w*|pulse|cancer\w*|tumou?rs?|lumps?|disease\w*|condition\w*|"
    r"illness\w*|ill|unwell|sick\w*|poorly|syndrome\w*|disorder\w*|deficien\w*|an(a)?emi\w*|dehydrat\w*|uti|urin\w*|"
    r"bladder|kidney\w*|liver|arthritis|gout|osteopor\w*|fractur\w*|broken|break|sprain\w*|injur\w*|wounds?|bruis\w*|"
    r"bleed\w*|cuts?|rash\w*|eczema|psoriasis|shingles|allerg\w*|alzheimer\w*|confus\w*|delirium|memory|depress\w*|"
    r"anxiety|clots?|thromb\w*|dvt|embol\w*|swell\w*|swollen|inflam\w*|nause\w*|vomit\w*|diarrh\w*|constipat\w*|"
    r"dizz\w*|vertigo|faint\w*|falls?|fallen|seizures?|fits?|shock|symptom\w*|bug|germs?|tablets?|pills?|"
    r"medicine\w*|medication\w*|dose\w*|(a |the )?(problem|issue|trouble) with your)\b", re.I)
_SOCIAL_WORD = (
    r"(lovely|beautiful|wonderful|nice|good|great|fine|kind|warm|gentle|peaceful|restful|relaxing|pleasant|happy|"
    r"merry|cosy|cozy|busy|quiet|special|splendid|marvellous|fantastic|brilliant|delightful|proud|pleased|glad|"
    r"delighted|thrilled|excited|lucky|brave|clever|generous|"
    r"family|friends?|visitors?|company|guests?|visits?|grandchild\w*|children|neighbours?|daughter|son|"
    r"granddaughter|grandson|sister|brother|niece|nephew|husband|wife|partner|birthday|party|plans?|memories|"
    r"talent|eye for|way with|green fingers|sense of humour)\b")
_DET = r"(such |so |very |really |real |quite |so many |so much |lots of |plenty of |many |some |a |an |your |the |(a )?bit of )*"
# What may follow a hedged "you have" or any "you have": something social, or
# "been" / "had" before something social ("been so kind", "had a lovely day").
_SOCIAL_AFTER = re.compile(r"^( got| been| had)? " + _DET + _SOCIAL_WORD, re.I)
# What may also follow a plain "you have": a finished, ordinary activity.
_ACTIVITY_AFTER = re.compile(
    r"^ (done|made|grown|planted|baked|cooked|written|read|achieved|earned|managed|raised|taught|helped|shared|"
    r"visited|enjoyed|finished|eaten|walked|remembered|worked|told|said|heard|spoken)\b", re.I)
_SOCIAL_BEFORE = re.compile(
    r"\b(hope|hoping|wish|wishing|trust|enjoy)\b(\s+[\w']+){0,3}\s+you have$|"
    r"\b(did|do|does|will|would|can|could|shall|should|have|had|might|may) you have$", re.I)
_SENTENCE = re.compile(r"[^.!?]+[.!?]*")


def _tells_them_what_they_have(text: str) -> bool:
    """Does the reply tell the person what they have (an illness, a symptom)?
    Anything it cannot place as social is treated as if it does."""
    t = text.replace("’", "'").replace("‘", "'")
    for sentence in _SENTENCE.findall(t):
        for m in _HEDGE.finditer(sentence):
            after = sentence[m.end():]
            if _HEALTH.search(after) or not _SOCIAL_AFTER.search(after):
                return True
        for m in _HAVE.finditer(sentence):
            after = sentence[m.end():]
            if _HEALTH.search(after):
                return True
            if m.group(0).lower().startswith("you've"):
                continue            # "you've" alone was never refused; only with a health word, or hedged (above)
            if _SOCIAL_BEFORE.search(sentence[:m.end()]):
                continue
            if _SOCIAL_AFTER.search(after) or _ACTIVITY_AFTER.search(after):
                continue
            return True
    return False


_ADVICE_ASK = re.compile(
    r"\b(should i (take|stop|have|skip|go|see|call|ring|double|miss)|is it (safe|normal|ok|okay) to|can i (take|skip|miss|stop|have)|"
    r"how (much|many) (\w+ )?(should|can|could|may) i|what('s| is) wrong with me|do i (have|need)|do you think i('ve| have| am| might)|"
    r"what should i do about|is (this|it|that) .{0,30}(normal|serious|dangerous)|dose|dosage|"
    r"what (tablet|pill|medicine|medication) (should|can)|(go|get) to (the )?hospital|see (a|the|my) (doctor|gp|nurse))\b", re.I)
_URL = re.compile(r"https?://|www\.", re.I)

FALLBACK_LINE = "I'm here with you, {name}. Shall I let {carer} know you'd like a word?"
ADVICE_LINE = "I can't advise on that, {name}. Shall I let {carer} know?"


@dataclass
class GuardResult:
    ok: bool
    text: str
    reason: str = ""


def guard(text: str, *, max_chars: int = 400, one_question: bool = False) -> GuardResult:
    """Is this reply fit to be spoken to the person? Describes, never diagnoses
    or advises; short; plain text."""
    t = re.sub(r"\s+", " ", (text or "")).strip().strip('"').strip()
    t = re.sub(r"<[^>]{1,40}>", "", t).strip()        # a stray tag or "thinking" marker
    if not t:
        return GuardResult(False, "", "empty")
    if len(t) > max_chars:
        return GuardResult(False, "", "too long")
    if not no_diagnosis_words(t):
        return GuardResult(False, "", "diagnosis word")
    if _ADVICE.search(t) or _tells_them_what_they_have(t):
        return GuardResult(False, "", "advice")
    if _URL.search(t):
        return GuardResult(False, "", "link")
    if one_question and t.count("?") > 1:
        return GuardResult(False, "", "more than one question")
    return GuardResult(True, t)


def asks_for_advice(text: str) -> bool:
    return bool(_ADVICE_ASK.search(text or ""))


# ── Sentences, for speaking as they arrive ───────────────────────────────────

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


class SentenceSplitter:
    """Feed tokens; get whole sentences back as soon as they are complete."""

    def __init__(self) -> None:
        self.buffer = ""

    def feed(self, token: str) -> list[str]:
        self.buffer += token
        parts = _SENTENCE_END.split(self.buffer)
        if len(parts) <= 1:
            return []
        self.buffer = parts[-1]
        return [p.strip() for p in parts[:-1] if p.strip()]

    def flush(self) -> list[str]:
        out = [self.buffer.strip()] if self.buffer.strip() else []
        self.buffer = ""
        return out


async def stream_sentences(model: Model, messages: list[dict], *, max_tokens: int, temperature: float,
                           first_sentence_s: Optional[float] = None) -> AsyncIterator[str]:
    """Sentences from the model as they complete. Raises TimeoutError if the
    first sentence is not ready within the budget, so the caller can fall back."""
    if first_sentence_s is not None:
        budget = first_sentence_s
    else:
        budget = getattr(model, "first_sentence_budget", None) or first_sentence_budget_s()
    splitter = SentenceSplitter()
    it = model.stream(messages, max_tokens=max_tokens, temperature=temperature).__aiter__()
    started = time.monotonic()
    first = True
    while True:
        try:
            if first:
                remaining = budget - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("first sentence too slow")
                token = await asyncio.wait_for(it.__anext__(), timeout=remaining)
            else:
                token = await it.__anext__()
        except StopAsyncIteration:
            for s in splitter.flush():
                yield s
            return
        except asyncio.TimeoutError as exc:
            raise TimeoutError("first sentence too slow") from exc
        for s in splitter.feed(token):
            first = False
            yield s


# ── Composing a check-in ─────────────────────────────────────────────────────

@dataclass
class Context:
    name: str
    language: str = DEFAULT_LANGUAGE
    time_of_day: str = "day"
    routines: Sequence[str] = ()
    medication_due: Sequence[str] = ()
    earlier_today: Sequence[str] = ()
    summary: str = ""
    carer_name: str = "your carer"


SYSTEM = (
    "You are IRA, a kind companion speaking aloud to an older person living alone. "
    "Short, plain and warm. No medical advice, no condition names, no doses. "
    "Never say you are a program. Speak {language}."
)

#: Every prompt token costs about 10 ms on four CPU cores and Gemma's sliding-window
#: attention means the prompt is processed afresh on every turn (round 4, D-069), so
#: the context is kept to what the question needs: at most this many items per line
#: and this many characters of memory.
CONTEXT_ITEMS = 3
CONTEXT_MEMORY_CHARS = 240

_LANGUAGE_NAMES = {"en-gb": "British English", "en": "English", "en-us": "English", "cy": "Welsh", "pl": "Polish",
                   "ur": "Urdu", "pa": "Punjabi", "bn": "Bengali", "gu": "Gujarati", "hi": "Hindi", "ta": "Tamil"}


def language_name(code: str) -> str:
    return _LANGUAGE_NAMES.get((code or "").lower(), code or "English")


def _context_lines(ctx: Context) -> str:
    lines = [f"Name: {ctx.name}. It is {ctx.time_of_day}."]
    if ctx.routines:
        lines.append("Routines: " + "; ".join(list(ctx.routines)[:CONTEXT_ITEMS]) + ".")
    if ctx.medication_due:
        lines.append("Medication due now: " + "; ".join(list(ctx.medication_due)[:CONTEXT_ITEMS]) + ".")
    if ctx.earlier_today:
        lines.append("Earlier today: " + "; ".join(list(ctx.earlier_today)[:CONTEXT_ITEMS]) + ".")
    if ctx.summary:
        memory = ctx.summary if len(ctx.summary) <= CONTEXT_MEMORY_CHARS else ctx.summary[:CONTEXT_MEMORY_CHARS].rsplit(" ", 1)[0] + "…"
        lines.append("Memory: " + memory)
    return "\n".join(lines)


def checkin_messages(ctx: Context, kind: str, subject: Optional[str] = None) -> list[dict]:
    about = f" It must mention {subject} by that name." if subject else ""
    return [
        {"role": "system", "content": SYSTEM.format(language=language_name(ctx.language))},
        {"role": "user", "content": (
            f"Ask the next check-in question about {kind}.{about} One warm question, at most two short sentences, "
            f"using the name. Words only.\n{_context_lines(ctx)}")},
    ]


async def compose_checkin(ctx: Context, kind: str, template: str, *, model: Optional[Model] = None,
                          subject: Optional[str] = None) -> tuple[str, str]:
    """The question to ask, and where it came from ("model" or "template").
    `subject` is the medication or routine the question is about: a composed
    question that does not name it is not used, because the answer is read
    against that name."""
    model = model if model is not None else configured_model()
    if model is None:
        return template, "template"
    try:
        parts: list[str] = []
        async for sentence in stream_sentences(model, checkin_messages(ctx, kind, subject), max_tokens=80, temperature=0.5):
            parts.append(sentence)
            if len(parts) >= 2:
                break
        verdict = guard(" ".join(parts), max_chars=220, one_question=True)
        names_subject = not subject or subject.lower() in verdict.text.lower()
        if verdict.ok and "?" in verdict.text and 5 <= len(verdict.text) and names_subject:
            return verdict.text, "model"
        logger.info("care conversation: model question rejected (%s); template used", verdict.reason or "no question")
    except Exception as exc:  # noqa: BLE001 - the template is the fallback, always
        logger.info("care conversation: model unavailable (%s); template used", type(exc).__name__)
    return template, "template"


# ── Reading an answer into a fixed structure ─────────────────────────────────

class StructuredAnswer(BaseModel):
    wellbeing: str = Field("unknown", pattern=r"^(good|ok|poor|unknown)$")
    pain_mentioned: bool = False
    medication_taken: str = Field("unknown", pattern=r"^(yes|no|unknown)$")
    needs_help: bool = False
    mood_words: list[str] = Field(default_factory=list, max_length=6)
    summary: str = Field("", max_length=300)

    @field_validator("mood_words")
    @classmethod
    def _short_words(cls, v):
        return [w.strip().lower()[:24] for w in v if isinstance(w, str) and w.strip()][:6]


#: Phrases that go straight to "ask first" and then the escalation chain.
RED_FLAGS = re.compile(
    r"\b(i (have )?fell|i('ve| have)? fallen|i fall|(had|having) a fall|fell (down|over|off)|"
    r"can'?t breathe|cannot breathe|chest pains?|pains? in my chest|(my )?chest (hurts|is tight)|"
    r"can'?t get up|cannot get up|i'?m bleeding|bleeding (a lot|badly)|can'?t move|cannot move|"
    r"can'?t feel my|(arm|leg|face|hand) is numb|i'?m stuck|passed out|blacked out|collapsed|"
    r"heart attack|stroke|"
    # Round 9: help in an older person's idiom (tests/care/eval/round9_*.jsonl). Each needs the
    # person, not a thing, as its subject, so "the TV's gone all funny" and "the cat is on the
    # floor" stay ordinary.
    r"can'?t stand up|cannot stand up|can'?t get (myself |my ?self )?(up|off the floor)|"
    r"(taken|took|had|having) a tumble|(the )?room is spinning|knocked myself out|"
    r"my legs? (have|has) gone|i'?m (lying |laid |down )?on the floor|lying on the floor|"
    r"(i'?ve |i have |i )?(come|came) over (all )?(funny|faint|dizzy|queer)|i'?ve gone all (funny|faint|dizzy)|"
    r"can'?t catch my breath|cannot catch my breath|face.{0,12}drooping|went down (like a sack|with a bang)|"
    r"blood everywhere|words (are )?coming out wrong|my (arm|leg) won'?t work|gone over in the|"
    r"i'?m still down|fallen out of bed|"
    # Round 10 (the round 9 hold-out's misses): a head injury is a red flag even when the person
    # says they are fine. "went down" alone is not added: "I went down to the shop" is ordinary.
    r"(hit|banged|bumped|knocked|cracked|smacked) (my|me) head|head injury|hurt my head|"
    # Round 10 hold-out (r10-r10-5, recorded as a miss before this line): chest tightness in other words.
    r"chest (has )?(gone|going|feels?|felt|is getting) (all )?(tight|heavy)|tight(ness)? (in|across) my chest)\b", re.I)


def red_flag(text: str) -> bool:
    return bool(RED_FLAGS.search(text or ""))


def answer_messages(kind: str, question: str, text: str, language: str = DEFAULT_LANGUAGE) -> list[dict]:
    return [
        {"role": "system", "content": "Read an older person's spoken answer to a companion's question. Reply with JSON only: "
                                      '{"wellbeing": good|ok|poor|unknown, "pain_mentioned": bool, "medication_taken": yes|no|unknown, '
                                      '"needs_help": bool, "mood_words": [up to 6 words], "summary": "one short sentence in your '
                                      'own words"}. "ok" is so-so or tired; "poor" is low, unwell or in pain. Never diagnose.'},
        {"role": "user", "content": f"Question kind: {kind}\nQuestion: {question}\nAnswer: {text}"},
    ]


def _quotes_the_person(summary: str, text: str) -> bool:
    """A summary must not carry the person's own words (DECISIONS D-020)."""
    words = re.findall(r"[a-z']+", (text or "").lower())
    s = (summary or "").lower()
    for i in range(0, max(0, len(words) - 3)):
        if " ".join(words[i:i + 4]) in s:
            return True
    return False


@dataclass
class Reading:
    """The merged reading: the keyword rules' AnswerReading plus the structure."""
    reading: rules.AnswerReading
    structured: StructuredAnswer
    source: str  # "model+rules" or "rules"
    red_flag: bool = False


# The classifier may only add a signal when it is sure: an "asked for help"
# it gets wrong is a false alarm to the whole circle. Chosen on the 150-turn
# eval (docs/evidence/training/classifier_eval.json), not on its training data.
CLASSIFIER_MIN_CONFIDENCE = {"needs_help": 0.9, "pain": 0.8, "wellbeing": 0.7, "said": 0.8}


def merge(keyword: rules.AnswerReading, structured: Optional[StructuredAnswer], text: str, kind: str,
          classified=None, *, subject: Optional[str] = None) -> Reading:
    """The stricter reading always wins: neither the model nor the classifier
    can downgrade a safety signal, and a red-flag phrase overrides everything.
    `classified` is the small classifier's reading (care/classifier.py), when it runs.

    For a dose the rules decide alone (`checkins.read_dose_answer`): the model
    and the classifier may only make the answer MORE cautious, never turn an
    unclear or negative answer into "taken". The merged `said`, the structured
    `medication_taken`, the summary and the `dose` always agree."""
    flag = red_flag(text)
    s = structured or StructuredAnswer()
    c_help = bool(classified and classified.needs_help and classified.confidence.get("needs_help", 0) >= CLASSIFIER_MIN_CONFIDENCE["needs_help"])
    c_pain = bool(classified and classified.pain and classified.confidence.get("pain", 0) >= CLASSIFIER_MIN_CONFIDENCE["pain"])
    c_poor = bool(classified and classified.wellbeing == "poor" and classified.confidence.get("wellbeing", 0) >= CLASSIFIER_MIN_CONFIDENCE["wellbeing"])
    needs_help = keyword.asked_for_help or s.needs_help or flag or c_help
    pain_kw = any("pain" in sig for sig in keyword.signals)
    pain = pain_kw or s.pain_mentioned or flag or c_pain
    said = keyword.said
    dose = keyword.dose
    if kind in ("meal", "medication"):
        c_said = (classified.said if classified and classified.said in ("yes", "no")
                  and classified.confidence.get("said", 0) >= CLASSIFIER_MIN_CONFIDENCE["said"] else "unknown")
        if kind == "medication":
            # Only ever more cautious. A "yes" from the rules that the model or the
            # classifier doubts is no longer a yes; nothing upgrades to yes.
            if said == "yes" and "no" in (s.medication_taken, c_said):
                said, dose = "no", "unclear"
            if said not in ("yes", "no"):
                said, dose = "unclear", dose if dose in ("later", "skip") else "unclear"
        elif "no" in (keyword.said, c_said):
            said = "no"
        elif keyword.said == "yes" or c_said == "yes":
            said = "yes"
        else:
            said = keyword.said or "unclear"
    signals = list(keyword.signals)
    if pain and not pain_kw:
        signals.append("mentioned pain or discomfort")
    if needs_help and not keyword.asked_for_help:
        signals.append("asked for help")
    if flag:
        signals.append("said something that needs a check right now")
    if (s.wellbeing == "poor" or c_poor) and "sounded low" not in signals:
        signals.append("sounded low")
    summary = keyword.summary
    if kind in ("meal", "medication") and said != keyword.said:
        base = rules.yes_no_summary(kind, said, subject, dose=dose)
        summary = base if not signals else f"{base} ({'; '.join(signals)})"
    # The keyword summary already says yes or no about a meal or a dose; the
    # model's sentence is used for the open questions, when it adds something.
    informative = len(s.summary.split()) >= 3 and s.summary.strip().lower().rstrip(".") not in ("answered the question", "gave an answer")
    if (structured is not None and s.summary and kind not in ("meal", "medication") and informative
            and not _quotes_the_person(s.summary, text) and no_diagnosis_words(s.summary)):
        base = s.summary.strip().rstrip(".") + "."
        summary = base if not signals else f"{base} ({'; '.join(signals)})"
    merged = rules.AnswerReading(summary=summary[:400], said=said, signals=tuple(dict.fromkeys(signals)),
                                 asked_for_help=needs_help, dose=dose if kind == "medication" else keyword.dose)
    wellbeing = "poor" if (s.wellbeing == "poor" or c_poor) else s.wellbeing
    if wellbeing == "unknown" and classified and classified.wellbeing in ("good", "ok"):
        wellbeing = classified.wellbeing
    return Reading(merged, StructuredAnswer(wellbeing=wellbeing, pain_mentioned=pain,
                                            medication_taken=("yes" if said == "yes" else "no" if said == "no" else "unknown")
                                            if kind == "medication" else "unknown",
                                            needs_help=needs_help, mood_words=s.mood_words, summary=merged.summary),
                   "model+rules" if structured is not None else "rules", red_flag=flag)


async def read_answer(kind: str, question: str, text: str, *, subject: Optional[str] = None,
                      model: Optional[Model] = None, language: str = DEFAULT_LANGUAGE,
                      other_medications: tuple = ()) -> Reading:
    keyword = rules.summarise_answer(kind, text, subject=subject, other_medications=other_medications)
    model = model if model is not None else configured_model()
    structured: Optional[StructuredAnswer] = None
    classified = None
    if (text or "").strip():
        from . import classifier as _classifier
        clf = _classifier.configured()
        if clf is not None:
            try:
                classified = clf.read(text)
            except Exception as exc:  # noqa: BLE001 - the rules and the model still decide
                logger.info("care classifier failed on an answer (%s)", type(exc).__name__)
    if model is not None and (text or "").strip():
        try:
            raw = await asyncio.wait_for(model.complete_json(answer_messages(kind, question, text, language), max_tokens=200),
                                         timeout=first_sentence_budget_s() * 2)
            structured = StructuredAnswer.model_validate(json.loads(raw))
        except (ValidationError, ValueError, TypeError) as exc:
            logger.info("care conversation: model answer did not fit the schema (%s); keyword reading used", type(exc).__name__)
        except Exception as exc:  # noqa: BLE001
            logger.info("care conversation: model unavailable for the answer (%s)", type(exc).__name__)
    return merge(keyword, structured, text, kind, classified, subject=subject)


# ── Talk to IRA ──────────────────────────────────────────────────────────────

@dataclass
class Turn:
    """One reply: sentences to speak, and an optional action for the client.
    `acknowledgement` is the short line the rules speak at once while the
    model composes `sentences` (round 4, D-070); it is spoken before them and
    is not one of them."""
    sentences: list[str]
    action: Optional[dict] = None
    source: str = "model"
    red_flag: bool = False
    needs_help: bool = False
    acknowledgement: Optional[str] = None

    @property
    def spoken(self) -> list[str]:
        """Everything the person heard, in order."""
        return ([self.acknowledgement] if self.acknowledgement else []) + list(self.sentences)


# ── The instant acknowledgement ──────────────────────────────────────────────
#
# A person who has just spoken should hear something within a second or so, the
# way a listener says "mm" before answering. The model's first sentence takes
# two to four seconds on four CPU cores, so the rules choose one short line and
# it is spoken at once; the model's sentences follow. The lines say nothing
# about the content, so they can never be wrong, and they never carry a
# question. IRA_CARE_INSTANT_ACK=off turns them off.

_QUESTION_WORDS = re.compile(r"^(what|when|where|who|why|how|is|are|do|does|did|can|could|will|would|shall|should|have|has)\b", re.I)
_ACK_QUESTION = ("Let me think, {name}.", "Let me see, {name}.", "Good question, {name}.")
_ACK_STATEMENT = ("I see, {name}.", "I'm listening, {name}.", "Thank you for telling me, {name}.", "Mm, {name}.")
_ACK_WORRY = ("I'm here, {name}.", "I hear you, {name}.")


def instant_ack_on() -> bool:
    return (os.getenv("IRA_CARE_INSTANT_ACK") or "on").strip().lower() not in ("off", "0", "false", "no")


def acknowledgement(text: str, name: str, *, pick: Optional[Callable[[Sequence[str]], str]] = None) -> str:
    """The short line spoken while the model composes. `pick` chooses among the
    lines (random by default); the tests pass their own."""
    import random
    choose = pick or random.choice
    t = (text or "").strip()
    if _WORRY.search(t):
        lines = _ACK_WORRY
    elif t.endswith("?") or _QUESTION_WORDS.search(t):
        lines = _ACK_QUESTION
    else:
        lines = _ACK_STATEMENT
    return choose(lines).format(name=name)


_CALL = re.compile(r"\b(call|ring|phone|dial)\s+(my\s+)?([a-z][a-z' -]{1,40})$", re.I)
_TODAY = re.compile(r"\b(what('s| is) (on|happening) today|what (do i have|am i doing) today|my day|today'?s plan)", re.I)
_TABLETS = re.compile(r"\b(did i (take|have) my (tablets?|pills?|medication|medicine)|have i (taken|had) my "
                      r"(tablets?|pills?|medication|medicine))\b", re.I)
_REMIND = re.compile(r"\b(remind me|don'?t let me forget)\b(.*)", re.I)


@dataclass
class TalkContext(Context):
    contacts: Sequence[Mapping] = ()        # circle members: display_name, phone, relationship
    doses_today: Sequence[str] = ()         # "Metformin at 08:00: taken"
    # The communication profile (care/personal.py, 110). The topic lists are
    # empty unless the person's 'topics' consent is recorded.
    familiar_topics: Sequence[str] = ()
    topics_avoid: Sequence[str] = ()
    redirect_name: Optional[str] = None     # who IRA offers to contact when a worry repeats
    reminder_due_words: Optional[str] = None  # "at 15:00", set by the caller once the time is parsed


_WORRY = re.compile(r"\b(worr(y|ied|ies)|scared|afraid|frightened|anxious|lonely|alone|nobody|no one|what if|where is|"
                    r"where'?s|has (he|she|anyone) |is (he|she) (coming|dead|all right|alright)|can'?t stop thinking|"
                    r"keep thinking|again and again|still not|not come|hasn'?t come|forgot(ten)? me)\b", re.I)
_STOP = frozenset("i me my the a an is are was were to of and it its it's im i'm he she they do does did has have had "
                  "not no so just very really that this there here what where when why how about for with on in at be been".split())


def _worry_tokens(text: str) -> frozenset:
    return frozenset(w for w in re.findall(r"[a-z']+", (text or "").lower()) if w not in _STOP and len(w) > 2)


def worry_repeated(text: str, earlier: Sequence[str]) -> bool:
    """The same worry, again: worry words now, and at least two earlier turns
    today that share most of their words with this one."""
    if not _WORRY.search(text or ""):
        return False
    now_tokens = _worry_tokens(text)
    if len(now_tokens) < 2:
        return False
    alike = 0
    for e in earlier:
        t = _worry_tokens(e)
        if not t:
            continue
        overlap = len(now_tokens & t) / max(1, len(now_tokens | t))
        if overlap >= 0.5 or (now_tokens & t) == now_tokens:
            alike += 1
    return alike >= 2


def redirect_reply(ctx: "TalkContext") -> Turn:
    """Gentle: acknowledge, do not argue or diagnose, offer a familiar topic
    (with consent) and the named person."""
    who = ctx.redirect_name or ctx.carer_name
    first = f"I hear you, {ctx.name}. That has been on your mind today, and I'm here with you."
    if ctx.familiar_topics:
        topic = ctx.familiar_topics[0]
        second = f"Shall I let {who} know you'd like a word? Or tell me about {topic}."
    else:
        second = f"Shall I let {who} know you'd like a word?"
    return Turn([first, second], action={"kind": "offer_contact", "name": who}, source="rules")


def mentions_avoided_topic(sentence: str, topics: Sequence[str]) -> bool:
    low = (sentence or "").lower()
    return any(t and t.lower() in low for t in topics)


_HELP_BUTTON = re.compile(r"\b(press(ing|ed)?|push(ing|ed)?|touch(ing|ed)?) (the )?help\b|\bhelp button\b", re.I)
_QUESTION = re.compile(r"^\s*(what|what's|whats|how|who|which|where|when|why|does|do|is|can|could|would|will)\b|\?\s*$", re.I)


def asks_about_the_help_button(text: str) -> bool:
    """A question about the Help button itself ("what number do you ring when I press Help?"),
    with no sign of trouble. Anything with a distress word, an urgent word or a red flag is not
    one, so "how do I press Help, I've fallen" still escalates."""
    t = (text or "").strip()
    if not (_HELP_BUTTON.search(t) and _QUESTION.search(t)):
        return False
    low = rules.normalise_answer(t)
    return not (rules._SAFE_DISTRESS.search(low.replace("help", "")) or
                re.search(r"\b(now|quick|quickly|please help|help me|emergency|ambulance|urgent)\b", low))


def rule_reply(text: str, ctx: TalkContext) -> Optional[Turn]:
    """The things IRA answers from the record, before any model: a call, the
    day's plan, the tablets, a reminder, and advice requests."""
    t = (text or "").strip()
    if not red_flag(t) and asks_about_the_help_button(t):
        # Round 10: a question ABOUT the Help button is not a call for help (round 9 hold-out).
        who = ctx.carer_name or "your family"
        return Turn([f"When you press Help, I tell {who} and the people looking after you straight away, {ctx.name}.",
                     "If you ever need them, press it, or just tell me, and I'll get help."], source="rules")
    if red_flag(t) or rules._HELP.search(t.lower()):
        return Turn([f"I'm getting help for you now, {ctx.name}. Stay where you are."], source="rules",
                    red_flag=red_flag(t), needs_help=True)
    # A reminder first: "remind me to ring Sravani later" is a reminder, not a call.
    m = _REMIND.search(t)
    if m:
        from .personal import parse_reminder
        raw = m.group(2).strip().strip(".") or "that"
        what, due_local, how = parse_reminder(raw, dt.datetime.now())
        when = ctx.reminder_due_words or ("in an hour" if how.startswith("default") else how if how.startswith("in ")
                                          else f"at {due_local.strftime('%H:%M')}")
        return Turn([f"I'll remind you about {what} {when}, {ctx.name}."],
                    action={"kind": "reminder", "text": what[:200], "raw": raw[:200], "when": when}, source="rules")
    m = _CALL.search(t)
    if m:
        who = m.group(3).strip().lower().rstrip(".!?")
        for c in ctx.contacts:
            names = {str(c.get("display_name", "")).lower(), str(c.get("relationship", "")).lower()}
            first = str(c.get("display_name", "")).split(" ")[0].lower()
            if who in names or who == first or (c.get("relationship") and who in str(c["relationship"]).lower()):
                if c.get("phone"):
                    return Turn([f"Here is {c['display_name']}. Tap to call."],
                                action={"kind": "call", "name": c["display_name"], "phone": c["phone"]}, source="rules")
                return Turn([f"I have {c['display_name']} but no phone number for them. Shall I let {ctx.carer_name} know?"],
                            source="rules")
        return Turn([f"I don't have anyone called {who} in your circle. Shall I let {ctx.carer_name} know?"], source="rules")
    if _TODAY.search(t):
        if ctx.routines or ctx.medication_due:
            parts = list(ctx.routines) + [f"{m} is due" for m in ctx.medication_due]
            return Turn([f"Today, {ctx.name}: " + "; ".join(parts) + "."], source="rules")
        return Turn([f"Nothing is written down for today, {ctx.name}. A quiet day."], source="rules")
    if _TABLETS.search(t):
        if ctx.doses_today:
            return Turn(["Today's tablets: " + "; ".join(ctx.doses_today) + "."], source="rules")
        return Turn([f"I have no tablets written down for today, {ctx.name}."], source="rules")
    if asks_for_advice(t):
        return Turn([ADVICE_LINE.format(name=ctx.name, carer=ctx.carer_name)], source="rules")
    return None


def talk_messages(ctx: TalkContext, text: str, recent: Sequence[tuple[str, str]] = ()) -> list[dict]:
    personal = ""
    if ctx.topics_avoid:
        personal += " Never mention: " + ", ".join(list(ctx.topics_avoid)[:CONTEXT_ITEMS]) + "."
    if ctx.familiar_topics:
        personal += " Enjoys: " + ", ".join(list(ctx.familiar_topics)[:CONTEXT_ITEMS]) + "."
    who = ctx.redirect_name or ctx.carer_name
    msgs = [{"role": "system", "content": SYSTEM.format(language=language_name(ctx.language)) +
             f" Two or three short sentences. Asked for advice: you cannot advise, offer to tell {ctx.carer_name}. "
             f"Worried, low or lonely: offer to let {who} know." + personal + "\n" + _context_lines(ctx)}]
    for speaker, words in recent[-6:]:
        msgs.append({"role": "assistant" if speaker == "companion" else "user", "content": words})
    msgs.append({"role": "user", "content": f"Talk: {text}"})
    return msgs


async def talk(text: str, ctx: TalkContext, *, recent: Sequence[tuple[str, str]] = (), model: Optional[Model] = None,
               on_sentence: Optional[Callable[[str], None]] = None) -> Turn:
    """A reply to the person. Rules first, then the model behind the guard, then the fallback line."""
    ruled = rule_reply(text, ctx)
    if ruled is None and worry_repeated(text, [w for sp, w in recent if sp == "person"]):
        ruled = redirect_reply(ctx)
    if ruled is not None:
        for s in ruled.sentences:
            if on_sentence:
                on_sentence(s)
        return ruled
    model = model if model is not None else configured_model()
    fallback = FALLBACK_LINE.format(name=ctx.name, carer=ctx.carer_name)
    if model is None:
        if on_sentence:
            on_sentence(fallback)
        return Turn([fallback], source="template")
    ack = acknowledgement(text, ctx.name) if instant_ack_on() else None
    if ack and on_sentence:
        on_sentence(ack)
    spoken: list[str] = []
    try:
        async for sentence in stream_sentences(model, talk_messages(ctx, text, recent), max_tokens=120, temperature=0.6):
            verdict = guard(sentence, max_chars=240)
            if not verdict.ok:
                raise ValueError(f"reply failed the guard: {verdict.reason}")
            if mentions_avoided_topic(verdict.text, ctx.topics_avoid):
                continue  # the person asked IRA not to bring this up
            spoken.append(verdict.text)
            if on_sentence:
                on_sentence(verdict.text)
            if len(spoken) >= 3:
                break
    except Exception as exc:  # noqa: BLE001
        logger.info("care conversation: talk fell back (%s)", type(exc).__name__)
        if not spoken:
            if on_sentence:
                on_sentence(fallback)
            return Turn([fallback], source="template", acknowledgement=ack)
    return Turn(spoken, source=model.name, acknowledgement=ack)


# ── Memory: the rolling summary ──────────────────────────────────────────────

def roll_summary(previous: str, additions: Sequence[str], *, max_chars: int = 1200) -> str:
    """Append today's notes to the rolling summary, oldest dropped first. Rule
    based on purpose: the memory never depends on a model being available."""
    lines = [ln for ln in (previous or "").split("\n") if ln.strip()]
    for a in additions:
        a = re.sub(r"\s+", " ", a).strip()
        if a and a not in lines:
            lines.append(a)
    out = "\n".join(lines)
    while len(out) > max_chars and len(lines) > 1:
        lines.pop(0)
        out = "\n".join(lines)
    return out[:max_chars]

