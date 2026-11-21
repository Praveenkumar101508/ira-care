"""P17-02 — the system benchmark reports what it measured, not what it drove.

These are units over the harness's judgement, not a second copy of the run. The
run itself needs a real uvicorn, a real socket and a migrated database; it
happens in CI (`system-benchmark` step) and its output is checked there.

What is testable everywhere, and is the part that can silently go wrong, is the
classification: **whether a latency figure is allowed to be called a measurement
of the path it was collected from.**
"""
from __future__ import annotations

import importlib
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[4]
HARNESS = REPO / "scripts" / "staging" / "p17_system_benchmark.py"
METHODOLOGY = REPO / "benchmarks" / "SYSTEM_BENCHMARK_METHODOLOGY.md"

if not HARNESS.is_file():
    pytest.skip(f"{HARNESS} not found", allow_module_level=True)


def _module():
    sys.path.insert(0, str(REPO / "scripts" / "staging"))
    return importlib.import_module("p17_system_benchmark")


# ── FINDING AS: a figure is only a measurement if the path did its work ─────


def test_all_2xx_is_a_measurement_of_the_path():
    assert _module()._what_it_measures({200: 60}) == "the path doing its work"


def test_a_rate_limited_path_is_not_an_authentication_measurement():
    """FINDING AS, as it actually occurred.

    `POST /auth/token` at concurrency 8: median 33 ms, p95 11.2 s, and **every
    one of 60 responses was 429**. As a row in a latency table that reads as
    "authentication is slow". It is a rate-limiter measurement, and the rate
    limiter is correct.

    The danger is not that the number is wrong. It is that the number is
    COMPARABLE with a real one, and nothing downstream can tell.
    """
    verdict = _module()._what_it_measures({429: 60})
    assert "rate limiter" in verdict
    assert "NOT the path" in verdict
    assert "0/60" in verdict


@pytest.mark.parametrize(("statuses", "expected"), [
    ({401: 40}, "authentication refusal"),
    ({403: 40}, "authorisation refusal"),
    ({404: 40}, "routing miss"),
    ({500: 40}, "whatever answers 500"),
])
def test_every_non_success_class_is_named_rather_than_lumped(statuses, expected):
    """Named, because "not 2xx" would hide which substitution occurred.

    A 401 and a 500 on the same path mean different things about the run — one
    says the principal was wrong, the other says the path broke — and a reader
    who has to open the status counts to find out will not.
    """
    assert expected in _module()._what_it_measures(statuses)


def test_a_transport_failure_disqualifies_the_figure_whatever_the_share():
    """FINDING BV — a percentage is the wrong instrument for a non-response.

    CI recorded `/readyz` as `{0: 15, 200: 185}`: **92.5% 2xx**, comfortably
    above the share threshold, so the verdict read "the path doing its work" —
    and the p95 was **30,033 ms**, which is not a latency. It is the client
    timeout, fifteen times over, and each one contributes exactly the timeout
    value to every percentile it appears in.

    A 429 or a 500 is the server ANSWERING, quickly, with the wrong thing, and a
    share threshold is right for those. A 0 is the harness giving up after
    waiting. **One is enough to poison the distribution**, so the share must not
    get a vote.
    """
    verdict = _module()._what_it_measures({0: 15, 200: 185})
    assert "NOT a latency" in verdict
    assert "15 of 200" in verdict
    # And the share is named explicitly as irrelevant, so a later reader does
    # not "fix" this by tuning the threshold.
    assert "irrelevant" in verdict


def test_a_single_no_response_is_enough_to_disqualify():
    """199 of 200 answered, and the figure is still not a latency.

    Deliberately separate from the case above: the rule is not "a lot of
    timeouts", it is "any timeout at all", and a test that only exercised 15 of
    them would let somebody reintroduce a threshold and still pass.
    """
    assert "NOT a latency" in _module()._what_it_measures({0: 1, 200: 199})


def test_the_threshold_is_a_share_and_a_stray_error_does_not_disqualify_a_run():
    """A run is not discarded for one flake, and IS discarded for a pattern.

    Both directions asserted. A classifier that only ever says "not the path" is
    a classifier nobody has watched accept anything.
    """
    module = _module()
    assert module.SUCCESS_SHARE_REQUIRED == 0.9
    assert module._what_it_measures({200: 95, 500: 5}) == "the path doing its work"
    assert module._what_it_measures({200: 80, 500: 20}) != "the path doing its work"


def test_no_responses_at_all_is_not_silently_a_success():
    """Zero measured is UNKNOWN, never clean — Phase 13's rule, item S."""
    assert _module()._what_it_measures({}) == "nothing — no response was recorded"


# ── The reporting rules the methodology commits to ──────────────────────────


def test_p95_is_nearest_rank_and_not_interpolated():
    """Stated in the methodology, so it is pinned here.

    p95 of 20 samples is the 19th value. A reader who assumes interpolation
    mis-reads every small run, and a silent change of estimator would make two
    recorded numbers incomparable without either of them changing.
    """
    module = _module()
    values = [float(n) for n in range(1, 21)]
    assert module._percentile(values, 95) == 19.0
    assert module._percentile(values, 50) == 10.0
    assert module._percentile([], 95) != module._percentile([], 95)   # NaN


def test_the_driven_set_is_wider_than_p13_and_reaches_an_authenticated_path():
    """The whole point of P17-02 over P13-04.

    P13-04 covered two dependency-free probes and skipped `/auth/token` for want
    of a migrated database and a seeded owner. If this set ever shrinks back to
    probes only, the phase's claim has quietly reverted.
    """
    driven = _module().DRIVEN
    paths = {path for _method, path, _auth, _why in driven}
    assert {"/livez", "/readyz"} <= paths, "the P13-04 baseline paths are gone"
    assert "/auth/token" in paths, "the skip P17-02 exists to retire is back"
    authenticated = [p for _m, p, needs_auth, _w in driven if needs_auth]
    assert authenticated, (
        "no authenticated path is driven, so this benchmark measures the same "
        "unauthenticated surface P13-04 did and the phase claims more than it "
        "does")


def test_every_driven_path_says_what_it_exercises():
    for method, path, _auth, why in _module().DRIVEN:
        assert why and len(why) > 40, (
            f"{method} {path} is driven with no stated reason. A path in a "
            f"benchmark with no reason is a path nobody can argue with.")


def test_every_skip_carries_a_reason():
    """A skip asserts nothing. An unexplained one asserts nothing and hides it."""
    for path, reason in _module().SKIPPED:
        assert reason and len(reason) > 30, f"{path} is skipped without a reason"


def test_the_methodology_states_what_is_not_measured_before_the_numbers():
    """A caveat that lives only in a docstring is a caveat that gets dropped."""
    text = METHODOLOGY.read_text(encoding="utf-8")
    assert "does not measure a deployment" in text
    assert "scalable" in text
    assert "FINDING AS" in text


def test_the_document_carries_its_own_caveat_rather_than_citing_one():
    """The disclaimer is written onto the results file, not only the method doc.

    Read from the harness's source constant rather than from a produced file,
    so this holds in an environment that cannot do a run.
    """
    source = HARNESS.read_text(encoding="utf-8")
    assert '"what_this_is_not"' in source
    # The sentence is a multi-line implicit concatenation, so it does not exist
    # as a contiguous string in the SOURCE — a source grep would fail against
    # correct code. Assert against the module's own docstring instead, which is
    # the text a reader of the harness actually meets.
    assert "does not measure a deployment" in (_module().__doc__ or "")


def test_a_recorded_run_is_shaped_the_way_the_methodology_promises():
    """If a results.json is present, it must carry conditions and coverage.

    Skips loudly when no run has been recorded — this environment often has no
    database — rather than passing vacuously on an absent file.
    """
    results = REPO / "artifacts" / "system-benchmark" / "results.json"
    if not results.is_file():
        pytest.skip(
            "no artifacts/system-benchmark/results.json in this checkout. The "
            "run happens in the CI benchmark step; this asserts its SHAPE when "
            "one is present and asserts nothing when it is not.")
    document = json.loads(results.read_text(encoding="utf-8"))
    for key in ("conditions", "results", "skipped", "paths_driven",
                "paths_measuring_the_path", "what_this_is_not"):
        assert key in document, f"the results document has no {key!r}"
    for field in ("host", "cpu_count", "concurrency", "server", "transport",
                  "database", "python"):
        assert document["conditions"].get(field) is not None, (
            f"conditions.{field} is unrecorded, so this run is not comparable "
            f"with any other")
    for row in document["results"]:
        assert "measures" in row, f"{row.get('path')} does not say what it measured"


def test_a_throttled_path_gets_a_budget_it_can_answer():
    """FINDING AZ — a burst against a rate-limited endpoint measures the limiter.

    `/auth/token` was driven at the full budget, 200 requests at concurrency 16,
    and the server died partway through. Two hundred logins at once is not a
    thing to point at a service one is trying to time — and by construction the
    figure could only ever have described the limiter, however long the run.
    """
    module = _module()
    assert "/auth/token" in module.THROTTLED, (
        "the login path is back on the full budget. A burst there measures the "
        "rate limiter and destabilises the service being timed.")
    requests, concurrency = module.THROTTLED["/auth/token"]
    assert concurrency == 1, "a throttled path driven concurrently is still a burst"
    assert requests <= 5, "more than the limiter allows is still the limiter"


def test_the_verdict_names_the_failing_status_not_the_successful_one():
    """FINDING BA — printed by CI's first green run, about itself.

    With {0: 1, 200: 3} the 2xx share is 75%, below the threshold — but the
    dominant status OVERALL is 200, so the first version reported "whatever
    answers 200, NOT the path". That names the successful responses as the
    culprit and says nothing about the one that failed.

    A diagnosis that points at the wrong half is worse than none: it is
    confidently misleading, and it only appears when failures are a MINORITY,
    which is the common case.

    ── Updated by FINDING BV, and deliberately retargeted, not deleted ──

    BA's original case was `{0: 1, 200: 3}`. BV made ANY no-response disqualify
    the figure outright, so that input now takes the earlier branch and never
    reaches the dominant-non-2xx logic BA exists to protect.

    The property — **the verdict must never name the successful responses as
    the culprit** — is unchanged and still needs a test. `{429: 1, 200: 3}` is
    the same minority-failure shape without a transport failure in it.
    """
    verdict = _module()._what_it_measures({429: 1, 200: 3})
    assert "rate limiter" in verdict, verdict
    assert "answers 200" not in verdict, (
        "the verdict blamed the successful responses")
    assert "3/4" in verdict

    # The case BA was originally written against still may not blame the
    # successes — it is simply refused earlier now, and harder.
    zero = _module()._what_it_measures({0: 1, 200: 3})
    assert "answers 200" not in zero, "the verdict blamed the successful responses"
    assert "NOT a latency" in zero
