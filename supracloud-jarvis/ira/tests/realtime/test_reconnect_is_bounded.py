"""Reconnection must give up.

The bug this file exists to prevent is not a failed reconnect — it is a
successful one that never stops. A retry loop with no ceiling leaves a user
watching "reconnecting…" indefinitely while the client hammers a provider that
is already struggling, and does it in lockstep with every other client that
dropped at the same moment.

Every assertion here is about one of four bounds: attempts, per-delay,
total time, and synchronisation.
"""
from __future__ import annotations

import random

import pytest

from core.realtime.reconnect import (
    DEFAULT_POLICY,
    NO_RETRY,
    BackoffPolicy,
    exhausted,
    schedule,
    total_ms,
)

DETERMINISTIC = BackoffPolicy(jitter=0.0)


# ── Bounded ──────────────────────────────────────────────────────────────────


def test_the_schedule_is_finite():
    """The single assertion this module exists for."""
    assert len(schedule(DETERMINISTIC)) <= DETERMINISTIC.max_attempts


def test_the_default_policy_gives_up_inside_the_users_patience():
    """Roughly fifteen seconds, because that is about how long someone will
    wait mid-conversation before deciding it is broken and doing something
    else. A policy that keeps trying for two minutes is not being persistent on
    the user's behalf — it is refusing to tell them it failed."""
    assert total_ms(schedule(DETERMINISTIC)) <= DEFAULT_POLICY.max_total_ms
    assert DEFAULT_POLICY.max_total_ms <= 30_000


def test_no_single_delay_exceeds_the_cap():
    """Exponential growth without a cap reaches "tomorrow" in about a dozen
    steps."""
    policy = BackoffPolicy(max_attempts=20, max_total_ms=10**9, jitter=0.0)
    for delay in schedule(policy):
        assert delay <= policy.max_delay_ms


def test_the_total_budget_stops_the_schedule_before_max_attempts_does():
    """Two independent bounds, and the tighter one must win.

    `max_attempts` alone is not a time bound: five attempts at a five-second
    cap is twenty-five seconds of silence.
    """
    policy = BackoffPolicy(max_attempts=50, max_total_ms=1_000, jitter=0.0)
    delays = schedule(policy)
    assert total_ms(delays) <= 1_000
    assert len(delays) < policy.max_attempts


def test_a_no_retry_policy_produces_no_attempts():
    """What a session gets when the transport said the failure is permanent.
    Retrying "no provider is configured" is five more ways to be told the same
    thing."""
    assert schedule(NO_RETRY) == []
    assert total_ms(schedule(NO_RETRY)) == 0


def test_an_empty_schedule_means_do_not_retry_not_retry_immediately():
    """A caller reading `[]` as "no delay needed" would produce an unbounded
    tight loop — the exact opposite of what an empty schedule means."""
    assert schedule(NO_RETRY) == []
    assert exhausted(1, NO_RETRY)


# ── Backoff, not repetition ──────────────────────────────────────────────────


def test_delays_grow():
    """Retrying at a fixed interval is not backoff; it is a fixed-rate flood
    against a provider that is already struggling."""
    delays = schedule(DETERMINISTIC)
    assert len(delays) >= 2
    for earlier, later in zip(delays, delays[1:]):
        assert later > earlier, f"delays do not grow: {delays}"


def test_the_growth_is_the_configured_factor():
    delays = schedule(BackoffPolicy(base_ms=100, factor=2.0, max_delay_ms=10**6,
                                    max_attempts=5, max_total_ms=10**9, jitter=0.0))
    assert delays == [100, 200, 400, 800, 1600]


def test_the_first_delay_is_short_enough_to_be_worth_waiting_for():
    """A transient blip resolves in well under a second. A first retry two
    seconds out turns a recoverable hiccup into an audible gap."""
    assert schedule(DETERMINISTIC)[0] <= 500


# ── Jitter ───────────────────────────────────────────────────────────────────


def test_jitter_desynchronises_clients():
    """A thousand clients reconnecting on the same schedule after a provider
    blip is how a blip becomes an outage."""
    schedules = {tuple(schedule(DEFAULT_POLICY, rng=random.Random(seed)))
                 for seed in range(20)}
    assert len(schedules) > 1, "every client would retry at the same instants"


def test_jitter_is_bounded_by_its_fraction():
    """Jitter must perturb the schedule, not replace it. Unbounded jitter is
    just a random delay with extra steps."""
    policy = BackoffPolicy(base_ms=1000, factor=1.0, max_attempts=200,
                           max_delay_ms=1000, max_total_ms=10**9, jitter=0.2)
    delays = schedule(policy, rng=random.Random(7))
    assert delays, "no delays produced"
    for delay in delays:
        assert 800 - 1 <= delay <= 1200 + 1, f"{delay} is outside ±20% of 1000ms"


def test_zero_jitter_is_reproducible():
    """The tests above depend on this; so does anyone debugging a retry storm."""
    assert schedule(DETERMINISTIC) == schedule(DETERMINISTIC)


def test_jittered_schedules_still_respect_every_bound():
    """The bounds must hold for the randomised path too — that is the path that
    actually runs."""
    for seed in range(50):
        delays = schedule(DEFAULT_POLICY, rng=random.Random(seed))
        assert len(delays) <= DEFAULT_POLICY.max_attempts
        assert total_ms(delays) <= DEFAULT_POLICY.max_total_ms
        assert all(1 <= d <= DEFAULT_POLICY.max_delay_ms * 1.25 for d in delays)


# ── Exhaustion ───────────────────────────────────────────────────────────────


def test_exhausted_is_one_based_and_agrees_with_the_schedule():
    """An off-by-one here is either a wasted retry or one fewer than
    configured — and it is invisible without an assertion."""
    policy = BackoffPolicy(max_attempts=3, max_total_ms=10**9, jitter=0.0)
    assert not exhausted(1, policy)
    assert not exhausted(3, policy)
    assert exhausted(4, policy)
    assert len(schedule(policy)) == 3


# ── Nonsense configuration is rejected at construction ───────────────────────


@pytest.mark.parametrize("kwargs,fragment", [
    ({"max_attempts": -1}, "negative"),
    ({"base_ms": 0}, "positive"),
    ({"factor": 0.5}, "shrink"),
    ({"jitter": 1.0}, "jitter"),
    ({"jitter": -0.1}, "jitter"),
])
def test_an_impossible_policy_raises_instead_of_being_used(kwargs, fragment):
    """A backoff that shrinks, or one with 100% jitter, is a retry storm that
    looks like a configuration value. Failing at construction means it fails at
    startup rather than during the first outage."""
    with pytest.raises(ValueError) as exc:
        BackoffPolicy(**kwargs)
    assert fragment in str(exc.value)
