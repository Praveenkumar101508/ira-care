"""P14-02 — failure detection: the gap the drill names in its own docstring.

`scripts/failover-drill.sh` builds a standby, SIGKILLs a primary, promotes and
reports RTO and RPO, and then says of its own numbers:

    The RTO here is PostgreSQL's promotion time only. A real recovery time also
    includes DETECTING THE FAILURE and POINTING CLIENTS AT THE NEW PRIMARY, and
    neither exists in this project yet.

P14-02 built the first. The second was still absent afterwards, and the last
section of this file asserted that rather than leaving it to be inferred — a
phase that closes half a gap and lets a status document imply the whole one is
recurring failure class 17 with extra steps.

**P15-04 built the second**, so that section now asserts the redirection EXISTS
and is driven by a CI job. Note what did not change: closing both halves still
does not produce an RTO. Detection, promotion and redirection are three separate
mechanisms and nothing composes them into one measured sequence, which is why
the drill's caveat survives P15-04 in a narrowed form rather than being deleted.

── The three properties everything else follows from ───────────────────────

1. **No observations is `NOT_MEASURED`, never `HEALTHY`.** The default cannot be
   the good answer. This is the way this module would most easily reintroduce
   the exact failure class Phase 13 spent a batch removing, so it is asserted
   first and from several directions.
2. **One failing probe is not a failure.** A detector that fires on a blip is an
   outage amplifier — `readiness.py`'s own docstring describes the mechanism.
3. **Old evidence is not evidence.** A HEALTHY observation nobody has refreshed
   decays back to `NOT_MEASURED`, because "it was fine half an hour ago" is not
   an answer to "is it fine".
"""
from __future__ import annotations

import asyncio
import logging
import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")


@pytest.fixture
def fd():
    """A detector of this test's own, never the process-wide one.

    Time is injected throughout rather than slept: every property here is about
    time, and a staleness test that sleeps is a test that goes flaky on a loaded
    CI runner and then gets deleted.
    """
    from core.runtime.failure_detector import FailureDetector

    return FailureDetector(failure_threshold=3, recovery_threshold=2,
                           staleness=30.0)


# ── 1. Nothing known is not the same as nothing wrong ───────────────────────


def test_a_detector_with_no_observations_reports_not_measured(fd):
    """The single most important assertion in this file.

    A detector whose default is HEALTHY reports every dependency in the system
    as fine before it has looked at any of them, which is worse than having no
    detector: it is a component that answers "yes" to a question nobody asked it
    to check.
    """
    from core.runtime.failure_detector import DependencyHealth

    status = fd.status("postgres")
    assert status.health is DependencyHealth.NOT_MEASURED
    assert status.health is not DependencyHealth.HEALTHY
    assert status.actionable is False, (
        "an unmeasured dependency is being treated as a failure, so every cold "
        "start now looks like an outage")
    assert fd.report() == [], (
        "the detector invented a row for a dependency nobody probed")
    assert fd.failing() == []


def test_a_healthy_observation_goes_stale_rather_than_persisting(fd):
    """"It was fine half an hour ago" is not an answer to "is it fine".

    The last state is deliberately not returned once the newest observation is
    older than the staleness window — a detector reporting HEALTHY off an
    observation nobody has refreshed is the Phase 13 failure class arriving
    inside the component built to close it.
    """
    from core.runtime.failure_detector import DependencyHealth

    fd.observe("redis", True, now=1_000.0)
    assert fd.status("redis", now=1_000.0).health is DependencyHealth.HEALTHY
    assert fd.status("redis", now=1_029.0).health is DependencyHealth.HEALTHY

    stale = fd.status("redis", now=1_031.0)
    assert stale.health is DependencyHealth.NOT_MEASURED, (
        "a 31-second-old success is still being reported as healthy")
    assert stale.last_observed_ms_ago == 31_000, (
        "the caller is told the answer is unknown and not told how stale the "
        "evidence is, which is the one case where it most needs to know")


def test_a_failed_state_also_goes_stale(fd):
    """The mirror, and the one that is easy to forget.

    Decaying only the good state would be a detector that latches onto failure:
    a dependency that failed once and was never probed again would read FAILED
    forever, and anything acting on that would refuse traffic to a system nobody
    has checked in an hour.
    """
    from core.runtime.failure_detector import DependencyHealth

    for i in range(3):
        fd.observe("postgres", False, now=1_000.0 + i)
    assert fd.status("postgres", now=1_002.0).health is DependencyHealth.FAILED

    late = fd.status("postgres", now=1_100.0)
    assert late.health is DependencyHealth.NOT_MEASURED, (
        "the detector latched on FAILED and never re-examined the claim")
    assert late.actionable is False


# ── 2. One blip is not an outage ────────────────────────────────────────────


def test_one_failure_does_not_declare_a_failure(fd):
    """The outage amplifier this module exists to avoid.

    `readiness.py`'s docstring describes the mechanism: a brief Redis wobble
    trips a probe, the orchestrator restarts every replica, the restarts hammer
    the recovering dependency, and twenty seconds becomes twenty minutes.
    """
    from core.runtime.failure_detector import DependencyHealth

    fd.observe("redis", False, now=1_000.0)
    first = fd.status("redis", now=1_000.0)
    assert first.health is DependencyHealth.SUSPECTED
    assert first.actionable is False, (
        "one failing probe is being treated as an outage")

    fd.observe("redis", False, now=1_001.0)
    assert fd.status("redis", now=1_001.0).health is DependencyHealth.SUSPECTED

    fd.observe("redis", False, now=1_002.0)
    declared = fd.status("redis", now=1_002.0)
    assert declared.health is DependencyHealth.FAILED, (
        "three consecutive failures did not cross the threshold, so the "
        "detector never fires and this whole batch is decorative")
    assert declared.actionable is True


def test_a_success_resets_the_run(fd):
    """Two failures, a success, two failures must NOT be a failure.

    Consecutive is the whole contract. An implementation counting total failures
    would pass every test above and declare an outage for a dependency that has
    been fine all day with occasional blips.
    """
    from core.runtime.failure_detector import DependencyHealth

    fd.observe("redis", False, now=1_000.0)
    fd.observe("redis", False, now=1_001.0)
    fd.observe("redis", True, now=1_002.0)
    assert fd.status("redis", now=1_002.0).health is DependencyHealth.HEALTHY

    fd.observe("redis", False, now=1_003.0)
    fd.observe("redis", False, now=1_004.0)
    assert fd.status("redis", now=1_004.0).health is DependencyHealth.SUSPECTED, (
        "the failure counter did not reset on success, so intermittent blips "
        "accumulate into a declared outage")


def test_recovery_needs_more_than_one_success(fd):
    """A flapping dependency is not a recovered one.

    Putting traffic back after a single success is how a half-recovered database
    gets knocked over again, so `RECOVERING` is a state a caller must not read
    as healthy.
    """
    from core.runtime.failure_detector import DependencyHealth

    for i in range(3):
        fd.observe("postgres", False, now=1_000.0 + i)
    assert fd.status("postgres", now=1_002.0).health is DependencyHealth.FAILED

    fd.observe("postgres", True, now=1_003.0)
    half = fd.status("postgres", now=1_003.0)
    assert half.health is DependencyHealth.RECOVERING, (
        "one success moved a failed dependency straight back to healthy")
    assert half.actionable is False, (
        "a recovering dependency is still being reported as an active failure")

    fd.observe("postgres", True, now=1_004.0)
    assert fd.status("postgres", now=1_004.0).health is DependencyHealth.HEALTHY


def test_a_flapping_dependency_never_reads_healthy(fd):
    """The composed case, driven rather than reasoned about.

    Alternating failure and success after an outage keeps resetting the recovery
    run, so the state stays RECOVERING — which is the behaviour that matters and
    is not obvious from the two tests above.
    """
    from core.runtime.failure_detector import DependencyHealth

    for i in range(3):
        fd.observe("postgres", False, now=1_000.0 + i)

    t = 1_003.0
    for _ in range(6):
        fd.observe("postgres", True, now=t)
        t += 1
        fd.observe("postgres", False, now=t)
        t += 1
        assert fd.status("postgres", now=t).health is not DependencyHealth.HEALTHY, (
            "a dependency flapping every second is being reported as healthy")


# ── The measurement, and what it is not ─────────────────────────────────────


def test_the_detection_figure_measures_the_threshold_not_the_outage(fd):
    """P14-02's measurement half, asserted with its caveat attached.

    The number is the interval between the FIRST FAILING OBSERVATION and the
    declaration. It is not a time-to-detect: everything between the dependency
    actually dying and the first failing probe is invisible here. The field is
    named `threshold_detection_ms` on the wire for that reason, and this test
    pins the arithmetic so the name cannot quietly start meaning something else.
    """
    fd.observe("postgres", False, now=1_000.0)
    fd.observe("postgres", False, now=1_002.5)
    fd.observe("postgres", False, now=1_005.0)

    status = fd.status("postgres", now=1_005.0)
    assert status.detection_ms == 5_000, (
        f"detection_ms is {status.detection_ms}, not the 5000 ms between the "
        f"first failing observation and the declaration")
    assert status.failing_for_ms == 0

    assert fd.status("postgres", now=1_009.0).failing_for_ms == 4_000, (
        "the ongoing outage duration is not being measured from the "
        "declaration")


def test_a_completed_outage_keeps_its_measured_length(fd):
    """The measurement must survive recovery, or nobody can ever report on it.

    A duration that exists only while the outage is happening is a duration
    nobody reads: the operator arrives afterwards.
    """
    for i in range(3):
        fd.observe("postgres", False, now=1_000.0 + i)
    fd.observe("postgres", True, now=1_010.0)
    fd.observe("postgres", True, now=1_012.0)

    status = fd.status("postgres", now=1_012.0)
    assert status.last_outage_ms == 10_000, (
        f"the outage lasted 10 s from declaration to recovery and the detector "
        f"recorded {status.last_outage_ms}")
    assert status.failing_for_ms is None
    assert status.detection_ms is None, (
        "a recovered dependency is still reporting a live detection figure")


def test_the_detector_is_memory_bounded(fd):
    """A monitoring component is not exempt from being a memory hole.

    `observe` takes a string. The readiness set is eight and fixed, but an
    unbounded map keyed by a name is the same defect as P14-01b's degraded
    rate limiter, and it would be introduced by the component whose job is
    noticing problems.
    """
    from core.runtime.failure_detector import DependencyHealth, FailureDetector

    small = FailureDetector(max_tracked=4)
    for i in range(10):
        small.observe(f"dep-{i}", True, now=1_000.0)

    assert len(small.report()) == 4
    assert small.status("dep-9").health is DependencyHealth.NOT_MEASURED, (
        "a dropped observation is being reported as healthy rather than as "
        "unknown — the drop must fail toward 'nothing is known'")


# ── The real caller: readiness feeds it ─────────────────────────────────────


def test_the_readiness_path_feeds_the_detector(monkeypatch):
    """§2 of the owner's standard, and the lesson of FINDING AB.

    P14-01 found `get_lockdown_state` alarming into a void — a control with no
    caller, counted as protection. A detector nothing feeds would be the same
    defect committed knowingly, one batch later, so the wiring is driven rather
    than described.
    """
    from core.runtime import readiness
    from core.runtime.failure_detector import (DependencyHealth, detector,
                                               reset_detector)

    async def failing_probe(**_kw):
        return readiness.CheckResult("postgres", readiness.CheckStatus.FAILED,
                                     1, True, "probe_error")

    async def ok_probe(name):
        async def _probe(**_kw):
            return readiness.CheckResult(name, readiness.CheckStatus.OK, 1, True)
        return _probe

    reset_detector()
    readiness.reset_readiness_cache()
    monkeypatch.setattr(readiness, "check_postgres", failing_probe)
    for name in ("redis", "migrations", "database_role", "key_provider",
                 "model_artefacts", "model_licences", "model_gateway"):
        monkeypatch.setattr(readiness, f"check_{name}",
                            asyncio.run(ok_probe(name)))

    try:
        for _ in range(3):
            report = asyncio.run(readiness.evaluate_readiness(use_cache=False))
            assert report.ready is False

        status = detector().status("postgres")
        assert status.health is DependencyHealth.FAILED, (
            f"three real readiness evaluations with postgres down left the "
            f"detector at {status.health!r} — nothing is feeding it")
        assert detector().status("redis").health is DependencyHealth.HEALTHY
    finally:
        reset_detector()
        readiness.reset_readiness_cache()


def test_a_cache_hit_is_not_a_fresh_observation(monkeypatch):
    """The subtle one, and the reason the observe call sits where it does.

    `evaluate_readiness` caches and single-flights. Observing there rather than
    in `_evaluate_uncached` would count cache hits and single-flight waiters as
    fresh probes, so ONE failing evaluation read by three concurrent callers
    would cross a three-failure threshold. Detection would fire on one failure
    while appearing to require three, which is worse than firing on one openly.
    """
    from core.runtime import readiness
    from core.runtime.failure_detector import (DependencyHealth, detector,
                                               reset_detector)

    async def failing_probe(**_kw):
        return readiness.CheckResult("postgres", readiness.CheckStatus.FAILED,
                                     1, True, "probe_error")

    async def skip(name):
        async def _probe(**_kw):
            return readiness.CheckResult(name, readiness.CheckStatus.SKIPPED,
                                         0, False)
        return _probe

    reset_detector()
    readiness.reset_readiness_cache()
    monkeypatch.setattr(readiness, "check_postgres", failing_probe)
    for name in ("redis", "migrations", "database_role", "key_provider",
                 "model_artefacts", "model_licences", "model_gateway"):
        monkeypatch.setattr(readiness, f"check_{name}", asyncio.run(skip(name)))

    async def three_concurrent_readers():
        readiness.reset_readiness_cache()
        await asyncio.gather(*(readiness.evaluate_readiness() for _ in range(3)))

    try:
        asyncio.run(three_concurrent_readers())
        status = detector().status("postgres")
        assert status.health is DependencyHealth.SUSPECTED, (
            f"one evaluation read by three callers produced {status.health!r} — "
            f"cache hits or single-flight waiters are being counted as probes")
        assert status.actionable is False

        # …and a SKIPPED check contributed nothing at all, in either direction.
        assert detector().status("redis").health is DependencyHealth.NOT_MEASURED, (
            "a skipped check was observed, so 'not run' is being recorded as "
            "evidence")
    finally:
        reset_detector()
        readiness.reset_readiness_cache()


async def test_readyz_verbose_exposes_the_detector(monkeypatch):
    """The HTTP surface, driven through the real router.

    `httpx.ASGITransport`, never `TestClient`, for the reason this suite has
    recorded several times: the latter drives the app on its own portal thread
    with its own event loop.
    """
    from httpx import ASGITransport, AsyncClient

    from app.factory import create_app
    from core.runtime import readiness
    from core.runtime.failure_detector import detector, reset_detector

    async def failing_probe(**_kw):
        return readiness.CheckResult("postgres", readiness.CheckStatus.FAILED,
                                     1, True, "probe_error")

    async def ok(name):
        async def _probe(**_kw):
            return readiness.CheckResult(name, readiness.CheckStatus.OK, 1, True)
        return _probe

    reset_detector()
    readiness.reset_readiness_cache()
    monkeypatch.setattr(readiness, "check_postgres", failing_probe)
    for name in ("redis", "migrations", "database_role", "key_provider",
                 "model_artefacts", "model_licences", "model_gateway"):
        monkeypatch.setattr(readiness, f"check_{name}", await ok(name))

    app = create_app()
    try:
        for _ in range(3):
            await readiness.evaluate_readiness(use_cache=False)
        assert detector().failing() == ["postgres"]

        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://p1402.invalid") as client:
            response = await client.get("/readyz?verbose=true")

        assert response.status_code == 503
        body = response.json()
        rows = {row["name"]: row for row in body.get("detection", [])}
        assert rows, f"the detector's view never reached the surface: {body}"
        assert rows["postgres"]["health"] == "failed"
        assert "threshold_detection_ms" in rows["postgres"], (
            "the measurement is not exposed, so nobody can read it")
        assert "detection_ms" not in rows["postgres"], (
            "the field is named in a way that reads as time-to-detect, which "
            "is the caveat this batch must not lose")
        assert rows["redis"]["health"] == "healthy"
    finally:
        reset_detector()
        readiness.reset_readiness_cache()


async def test_readyz_says_nothing_about_detection_when_nothing_is_known():
    """Absent, not present-and-empty.

    A `detection: []` on a process that has observed nothing reads as "checked,
    no failures". That is precisely the claim a process with no observations
    cannot make, and it is the same distinction P13-03 drew between `down` and
    `unknown` on the health surface.
    """
    from httpx import ASGITransport, AsyncClient

    from app.factory import create_app
    from core.runtime import readiness
    from core.runtime.failure_detector import reset_detector

    reset_detector()
    readiness.reset_readiness_cache()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://p1402.invalid") as client:
        body = (await client.get("/readyz?verbose=true")).json()

    if "detection" in body:
        # Probes may have genuinely run against this process; the assertion that
        # matters is that the key is never present-and-empty.
        assert body["detection"], (
            "an empty detection list is being reported, which reads as "
            "'checked, all fine' from a process that has observed nothing")
    reset_detector()
    readiness.reset_readiness_cache()


# ── The gap this batch left open, and what closed it ───────────────────────
#
# This section asserted an ABSENCE until P15-04. See the first test for why the
# absence assertion had to go rather than be updated.



def test_time_to_detect_is_measured_and_is_still_not_a_recovery_time():
    """INVERTED by P14-R2, and the second half is the half that matters.

    This asserted `baseline is None` on the grounds that nothing composed the
    three mechanisms. P14-R2 composed them and measured this against a SIGKILL
    it sent, so the absence is no longer true and asserting it would be an
    assertion of a false absence — green while the thing it denies exists.

    What survives is the distinction the row was protecting: a met objective
    HERE is still not a recovery time. Detection is ~86% of the end-to-end
    figure, not all of it, and the end-to-end figure belongs to the drill.
    """
    from core.runtime.slo import register

    row = [r for r in register()
           if r["slo"] == "dependency_failure_time_to_detect"][0]
    assert row["baseline"] is not None, (
        "the time-to-detect baseline is gone. P14-R2 measured it — 1.21 s from "
        "a SIGKILL to the declaration — and a row that goes back to None is "
        "claiming nothing was measured when something was.")
    assert "SIGKILL" in row["baseline"], (
        f"the baseline no longer says what it was measured against: "
        f"{row['baseline']!r}. A number without a provenance is the thing this "
        f"register exists to refuse.")
    caveats = " ".join(row["caveats"])
    assert "still not be a recovery" in caveats, (
        "the row stopped saying that meeting this objective is not a recovery "
        "time. Detection is one part of the interval, and the whole is "
        "measured elsewhere.")


def test_the_drill_states_three_parts_and_refuses_to_add_them_up():
    """Documentation matching reality, in the direction that is easy to miss.

    The drill's docstring originally said BOTH halves were missing. P14-02 built
    detection; **P15-04 built redirection**. A document that disagrees with the
    tree is the same defect whichever way it leans — P14-00 recorded that when it
    corrected two status rows that UNDERSTATED Phase 13 — so the sentence had to
    be rewritten rather than left alone.

    The rewrite is where the risk moved. "Both halves now exist" is true and
    reads as "failover works", which is the overstatement this test exists to
    block. So the drill must say all three parts exist AND that nothing composes
    them, and must still refuse the RTO. Naming the parts is not the claim; the
    refusal is.
    """
    import pathlib
    import re

    drill = (pathlib.Path(__file__).resolve().parents[2] / "scripts"
             / "failover-drill.sh").read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", drill)

    assert "core/runtime/failure_detector.py" in drill, (
        "the drill no longer names detection; P14-02 built it, and a document "
        "understating the tree is as wrong as one overstating it")
    assert "infra/terraform/redirection/" in drill, (
        "the drill no longer names redirection; P15-04 built it")

    assert "still promotion time alone" in flat, (
        "the drill stopped saying its number is promotion time only — which is "
        "what it becomes the moment somebody reads three existing parts as one "
        "working mechanism")
    # UPDATED by P14-R2. This used to require "never been run end to end", and
    # that sentence became false the day the composition was built: an
    # assertion of a false absence goes green while the thing it denies exists.
    # What replaces it is the pointer, so a reader of THIS script is sent to
    # the number that is a recovery time rather than left with the one that is
    # not.
    assert "failover_e2e_drill.py" in flat, (
        "the drill no longer points at the end-to-end drill that composes the "
        "three mechanisms, so a reader is left with promotion time and no way "
        "to find the figure that is actually an RTO")


def test_the_slo_register_claims_the_measurement_and_its_provenance():
    """The register is where this project states what it can and cannot measure.

    P13-05 built it precisely so a claim of coverage has to be earned. This row
    said "the mechanism is connected and NOTHING HAS BEEN MEASURED against the
    objective — nobody has killed a dependency and timed the declaration
    against the moment it died", and asserted `baseline is None` so a number
    could not appear without the work behind it.

    P14-R2 did the work: `scripts/failover_e2e_drill.py` sends the SIGKILL, so
    the moment of death is known exactly rather than inferred from a probe.

    The assertion therefore flips from "no number" to "a number WITH its
    provenance" — which is the property the original was protecting. A bare
    figure would still fail this test.
    """
    from core.runtime.slo import register

    row = [r for r in register()
           if r["slo"] == "dependency_failure_time_to_detect"][0]

    # INVERTED by P14-R2, exactly as this test's own failure message asked:
    # "if it was genuinely measured, keep it and update this test with the
    # provenance". It was, so the provenance is what is asserted now.
    assert row["baseline"] is not None, (
        "the time-to-detect baseline went back to None. P14-R2 measured it "
        "against a SIGKILL it sent; a row claiming nothing was measured is now "
        "the false statement.")
    assert "failover_e2e_drill.py" in row["baseline"], (
        f"the baseline does not name what took it ({row['baseline']!r}). A "
        f"number without a provenance is indistinguishable from one inferred "
        f"from the threshold, which is what this test was written to catch.")
    assert "runs)" in row["baseline"], (
        "the baseline does not say how many runs it is over. One run is an "
        "anecdote.")
    assert row["collection"] != "not_collected", (
        "the signal reads as uncollected, so the readiness path is not feeding "
        "the detector in this build")

    caveats = " ".join(row["caveats"])
    assert "still not be a recovery" in caveats, (
        "the row does not say that detection is half a recovery time, so a "
        "met objective here would read as resilience")


def test_the_slo_probe_is_a_probe_and_not_a_declaration():
    """P13-05's own rule, applied to the signal P14-02 adds.

    Its `_process_records_metrics` probe shipped a version that returned True
    for anything it did not recognise — a blacklist, so an unknown type read as
    *collected*. This asserts the new probe is derived by execution: break the
    detector's contract and the probe must go False rather than keep reporting
    the signal as present.
    """
    from core.runtime import slo
    from core.runtime.failure_detector import DependencyHealth

    assert slo._failure_detection_is_wired() is True

    class _Deaf:
        """A detector that accepts observations and learns nothing."""

        def observe(self, *a, **k):
            pass

        def status(self, name, **k):
            return type("S", (), {"health": DependencyHealth.NOT_MEASURED})()

    import core.runtime.failure_detector as fdmod
    real = fdmod.FailureDetector
    fdmod.FailureDetector = _Deaf
    try:
        assert slo._failure_detection_is_wired() is False, (
            "the probe reports the signal as collected against a detector that "
            "observes nothing — it is reading a declaration, not executing")
    finally:
        fdmod.FailureDetector = real
