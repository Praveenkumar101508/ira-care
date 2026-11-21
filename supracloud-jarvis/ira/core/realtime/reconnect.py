"""Bounded reconnection.

A dropped connection should be retried. It should not be retried forever, and
the retries should not arrive in a burst — a thousand clients all reconnecting
on the same schedule after a provider blip is how a blip becomes an outage.

Every property here is about the word BOUNDED:

  * a maximum number of attempts, after which the session ends with
    `RECONNECT_EXHAUSTED` rather than spinning;
  * a maximum delay, so the backoff does not grow into "tomorrow";
  * a maximum TOTAL wall time, so the user is not left staring at
    "reconnecting" for longer than they would have tolerated;
  * jitter, so clients do not synchronise.

`schedule()` returns the full plan as a list up front, rather than yielding one
delay at a time. That is deliberate: a plan you can print is a plan you can
assert on, and "how long will this give up after?" is answerable by summing a
list instead of by reasoning about a loop.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Final, Optional, Sequence


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """How hard to try. Defaults chosen to give up inside ~15 seconds.

    Fifteen seconds because that is roughly how long someone will wait mid
    conversation before deciding it is broken and doing something else. A
    policy that keeps trying for two minutes is not being persistent on the
    user's behalf; it is refusing to tell them it failed.
    """

    max_attempts: int = 5
    base_ms: int = 250
    factor: float = 2.0
    max_delay_ms: int = 5_000
    #: The retry budget across all attempts. Reached => no further attempts,
    #: whatever `max_attempts` says.
    max_total_ms: int = 15_000
    #: Fraction of each delay that is randomised, ±. 0.0 makes the schedule
    #: fully deterministic, which the tests use.
    jitter: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 0:
            raise ValueError("max_attempts cannot be negative")
        if self.base_ms <= 0:
            raise ValueError("base_ms must be positive")
        if self.factor < 1.0:
            raise ValueError("factor below 1.0 would make the backoff shrink")
        if not 0.0 <= self.jitter < 1.0:
            raise ValueError("jitter must be in [0.0, 1.0)")


DEFAULT_POLICY: Final[BackoffPolicy] = BackoffPolicy()

#: A policy that never retries. What a session gets when the transport said the
#: failure is permanent — retrying a "provider not configured" is just five
#: more ways to be told the same thing.
NO_RETRY: Final[BackoffPolicy] = BackoffPolicy(max_attempts=0, max_total_ms=0)


def schedule(
    policy: BackoffPolicy = DEFAULT_POLICY,
    *,
    rng: Optional[random.Random] = None,
) -> list[int]:
    """The complete retry plan, in milliseconds before each attempt.

    An empty list means "do not retry", and the caller must treat that as the
    session ending — not as "retry immediately with no delay".
    """
    random_source = rng or random.Random()
    delays: list[int] = []
    total = 0
    delay = float(policy.base_ms)

    for _ in range(policy.max_attempts):
        capped = min(delay, float(policy.max_delay_ms))
        if policy.jitter:
            spread = capped * policy.jitter
            capped = random_source.uniform(capped - spread, capped + spread)
        step = max(1, int(round(capped)))

        if total + step > policy.max_total_ms:
            break                      # the retry budget is spent
        delays.append(step)
        total += step
        delay *= policy.factor

    return delays


def total_ms(delays: Sequence[int]) -> int:
    return sum(delays)


def exhausted(attempt: int, policy: BackoffPolicy = DEFAULT_POLICY) -> bool:
    """True once `attempt` (1-based) is past the policy's limit."""
    return attempt > policy.max_attempts


__all__ = [
    "DEFAULT_POLICY",
    "NO_RETRY",
    "BackoffPolicy",
    "exhausted",
    "schedule",
    "total_ms",
]
