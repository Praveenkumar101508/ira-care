"""Which model answered a chat turn, as the provider reported it (finding P1-05).

The chat response's `model_used` and the stream's done-frame `model` used to be
fixed tier names (`qwen3-fast`, `qwen3-deep`), written by the agent or the route
before or without looking at what answered. On the install of 31 October 2026
the readout said `qwen3-deep` while Ollama served `qwen3:4b`. The Agent activity
panel shows that field to the owner, and constitution rule D4 says a displayed
state reflects what the backend reported.

The model's name is known in exactly one place: the provider's response, which
echoes the model that ran. `utils/llm.py` returns only text to its forty-odd
callers, so the name is carried beside the call instead of through it. A chat
route opens a recording around the turn; each completion that reaches the
caller notes its model there; the route reads the last one.

── Why the last call ───────────────────────────────────────────────────────

A turn can make more than one model call: the executor asks for a command
before it answers. The answer is written by the last call, so that is the one
reported. The supervisor's routing call is excluded (`excluded()`), because it
decides who answers and writes none of the answer: a turn the gate then
refuses has had no model answer at all.

── What is never reported ──────────────────────────────────────────────────

A refusal from the gateway, which no model wrote; a call outside a recording;
and a call on the direct path `utils/llm.py` takes only when no gateway can be
built. When nothing was recorded, the caller says `NOT_REPORTED`; it does not
substitute the tier it asked for.

── A mutable recorder in a ContextVar ──────────────────────────────────────

LangGraph runs each node in a task with a copy of the caller's context. A value
SET inside a node would not reach the route; an object the route created and a
node APPENDS to does, because the copy refers to the same object.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Iterator, Optional

#: What the readout says when no provider reported a model for the turn.
NOT_REPORTED = "not reported"


@dataclass(frozen=True, slots=True)
class Served:
    #: The model as the provider's response named it, e.g. `qwen3:4b`.
    model: str
    #: The tier IRA asked for (`fast`, `deep`, `reasoning`), when known.
    tier: Optional[str] = None


class ServedModels:
    """The completions that reached the caller during one recording, in order."""

    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls: list[Served] = []

    def note(self, model: str, tier: Optional[str] = None) -> None:
        self.calls.append(Served(model=model, tier=tier))

    @property
    def last(self) -> Optional[Served]:
        return self.calls[-1] if self.calls else None


_recorder: ContextVar[Optional[ServedModels]] = ContextVar("ira_served_models", default=None)


def begin() -> tuple[ServedModels, Token]:
    """Start recording in this context. Pair with `end(token)`.

    For an async generator, where a `with` block would have to enclose every
    `yield`; everywhere else, use `recording()`.
    """
    served = ServedModels()
    return served, _recorder.set(served)


def end(token: Token) -> None:
    """Stop the recording `begin` started.

    A streamed response that the client abandons is closed from another
    context, where the token cannot be reset; there is then nothing to restore
    in this one either.
    """
    try:
        _recorder.reset(token)
    except ValueError:
        pass


@contextmanager
def recording() -> Iterator[ServedModels]:
    """Record every completion that reaches the caller inside the block."""
    served, token = begin()
    try:
        yield served
    finally:
        end(token)


@contextmanager
def excluded() -> Iterator[None]:
    """Calls inside the block are not recorded: they write none of the answer."""
    token = _recorder.set(None)
    try:
        yield
    finally:
        _recorder.reset(token)


def note_served(model: Optional[str], tier: Optional[str] = None) -> None:
    """Record that `model` answered, if a recording is open and a model was named."""
    served = _recorder.get()
    if served is not None and model:
        served.note(str(model), tier)
