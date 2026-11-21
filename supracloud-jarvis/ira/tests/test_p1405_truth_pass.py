"""P14-05 — the adversarial truth pass: check the claims this phase made.

Phase 12's truth pass found **four of eight** claims false the first time
anybody checked them, including two live cross-tenant disclosures. Phase 13's
caught its own SLO probe shipping as a fail-open blacklist, and its acceptance
harness muting the logger it was testing for. The rate is not an accident, and
the shape is always the same: **a claim true at one layer and asserted at
another.**

So this file does not re-run Phase 14's unit tests. It attacks the sentences the
batch documents wrote, at the layer they wrote them about.

── FINDING AF, found here ──────────────────────────────────────────────────

P14-02's central claim is that an operator can tell a *fresh* failure from a
*long* one. The unit tests proved that of the detector; the batch document
claimed it of `/readyz?verbose=true`.

In production, `/readyz` suppresses verbose output — correctly, so an
unauthenticated caller cannot map internal topology and timing — and the
detection section rode on that flag. So the detector ran, observed, declared
FAILED, and **its view was invisible in the only environment where the claim
matters.** `/health/detail` is no help: it is unauthenticated and reduced in
production too, so there was no operator surface at all.

Phase 12's shape exactly. The suppression stays; production now gets a reduced
view — `health` only, and only for dependencies already named in the public
`failed` list, so no topology or timing is added — which is precisely the
distinction the batch exists to draw.
"""
from __future__ import annotations

import asyncio
import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")


def _check(name, ok):
    from core.runtime.readiness import CheckResult, CheckStatus

    return CheckResult(name, CheckStatus.OK if ok else CheckStatus.FAILED,
                       1, True, "")


def _all_probes_but_postgres(monkeypatch):
    from core.runtime import readiness

    async def down(**_kw):
        return _check("postgres", False)

    monkeypatch.setattr(readiness, "check_postgres", down)
    for name in ("redis", "migrations", "database_role", "key_provider",
                 "model_artefacts", "model_licences", "model_gateway"):
        async def up(_n=name, **_kw):
            return _check(_n, True)
        monkeypatch.setattr(readiness, f"check_{name}", up)


async def _readyz(environment: str, monkeypatch, failures: int):
    from httpx import ASGITransport, AsyncClient

    from app.factory import create_app
    from core.runtime import readiness
    from core.runtime.failure_detector import reset_detector

    monkeypatch.setenv("IRA_ENVIRONMENT", environment)
    _all_probes_but_postgres(monkeypatch)
    reset_detector()
    readiness.reset_readiness_cache()
    try:
        for _ in range(failures):
            await readiness.evaluate_readiness(use_cache=False)
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://p1405.invalid") as client:
            response = await client.get("/readyz?verbose=true")
        return response
    finally:
        reset_detector()
        readiness.reset_readiness_cache()


# ── FINDING AF ──────────────────────────────────────────────────────────────


async def test_production_can_still_tell_a_blip_from_an_outage(monkeypatch):
    """FINDING AF. The claim, checked in the environment it is made about.

    Before the fix this body was `{"status": "not_ready", "failed": ["postgres"]}`
    in production whether postgres had been failing for one probe or for four
    hours — which is exactly the question P14-02 was built to answer.
    """
    response = await _readyz("production", monkeypatch, failures=1)
    rows = {r["name"]: r for r in response.json().get("detection", [])}
    assert rows, (
        f"no detection section in production: {response.json()}. The detector "
        f"is running and nothing can see it.")
    assert rows["postgres"]["health"] == "suspected", (
        "a single failing probe is not distinguishable from an outage")

    response = await _readyz("production", monkeypatch, failures=3)
    rows = {r["name"]: r for r in response.json().get("detection", [])}
    assert rows["postgres"]["health"] == "failed"


async def test_production_still_discloses_no_timing_or_topology(monkeypatch):
    """The reason the suppression exists, re-asserted after weakening it.

    A fix that solved AF by turning verbose back on in production would trade a
    reporting gap for an unauthenticated topology-and-timing disclosure. The
    reduced body must carry `name` and `health` and nothing else, and it must
    name only dependencies `/readyz` already discloses publicly.
    """
    response = await _readyz("production", monkeypatch, failures=3)
    body = response.json()

    assert "checks" not in body, (
        "the full verbose body is being served in production — this is the "
        "topology disclosure the suppression exists to prevent")
    extra = {key for row in body["detection"] for key in row} - {"name", "health"}
    assert extra == set(), (
        f"production is disclosing {sorted(extra)}; timing fields let an "
        f"unauthenticated caller measure internal behaviour")

    named = set(body.get("failed", []))
    reported = {row["name"] for row in body["detection"]}
    assert reported <= named, (
        f"production is naming dependencies not already in `failed`: "
        f"{sorted(reported - named)} — that is new topology")


async def test_a_healthy_production_instance_says_nothing_at_all(monkeypatch):
    """No `detection` key when nothing has failed.

    `detection: []` on a healthy instance would read as "checked, all fine",
    which is the distinction P13-03 drew between `down` and `unknown` and the
    one this whole phase is about.
    """
    from httpx import ASGITransport, AsyncClient

    from app.factory import create_app
    from core.runtime import readiness
    from core.runtime.failure_detector import reset_detector

    monkeypatch.setenv("IRA_ENVIRONMENT", "production")
    from core.runtime.readiness import CheckStatus  # noqa: F401

    async def up(_n=None, **_kw):
        return _check("x", True)

    for name in ("postgres", "redis", "migrations", "database_role",
                 "key_provider", "model_artefacts", "model_licences",
                 "model_gateway"):
        async def _u(_n=name, **_kw):
            return _check(_n, True)
        monkeypatch.setattr(readiness, f"check_{name}", _u)

    reset_detector()
    readiness.reset_readiness_cache()
    try:
        await readiness.evaluate_readiness(use_cache=False)
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://p1405.invalid") as client:
            body = (await client.get("/readyz?verbose=true")).json()
    finally:
        reset_detector()
        readiness.reset_readiness_cache()

    assert body["status"] == "ready"
    assert "detection" not in body, (
        "a healthy production instance is reporting a detection section, which "
        "reads as 'checked' from a surface that should say nothing")


async def test_development_still_gets_the_full_view(monkeypatch):
    """The control. Reducing everywhere would satisfy the tests above and would
    remove the timings an operator debugging locally actually wants."""
    response = await _readyz("development", monkeypatch, failures=3)
    body = response.json()
    rows = {r["name"]: r for r in body.get("detection", [])}

    assert "checks" in body, "verbose output is suppressed in development"
    assert rows["postgres"]["health"] == "failed"
    assert "threshold_detection_ms" in rows["postgres"], (
        "the measurement is gone from the one environment allowed to see it")
    assert "redis" in rows, (
        "development is only reporting failed dependencies; the full view is "
        "the point of verbose")


# ── The other claims, attacked at the layer they were made about ────────────


def test_the_ssh_offset_fix_is_not_dead_code(monkeypatch):
    """FINDING AE's second half — attacked rather than assumed.

    `_check_ssh_failures` prefers journald and falls back to the file reader.
    If journald is present on every real host, the file path — and therefore
    half of finding AE's fix — would never execute, and the batch's claim to
    have fixed *two* checkpoints would be true of one.

    The honest answer is in the code: the journald path is tried and the file
    path is the fallback, so on a host without journald (containers, most
    non-systemd images) the fixed path is the live one. Asserted so the claim
    is not read as stronger than it is.
    """
    import inspect
    import sys
    import types

    if "psutil" not in sys.modules:
        monkeypatch.setitem(sys.modules, "psutil", types.ModuleType("psutil"))
    from worker import security_monitor

    source = inspect.getsource(security_monitor._check_ssh_failures)
    assert "_check_ssh_failures_journald" in source
    assert "_check_ssh_failures_file" in source, (
        "the file reader is no longer reachable from the dispatcher, so "
        "finding AE's SSH half is dead code")


def test_no_phase_14_suite_skips_anything():
    """Every P14 batch claims "no skips". A skip is invisible in a green
    summary, which is the failure this phase exists to find — so the claim is
    executed rather than trusted."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent
    suites = sorted(str(p) for p in root.glob("test_p140*.py")
                    if p.name != Path(__file__).name)
    assert len(suites) >= 4, f"expected the P14 suites, found {suites}"

    result = subprocess.run(
        [sys.executable, "-m", "pytest", *suites, "-q", "-p", "no:cacheprovider",
         "--no-header", "-ra"],
        capture_output=True, text=True, cwd=str(root.parent))
    tail = result.stdout.strip().splitlines()[-1] if result.stdout else ""
    assert result.returncode == 0, f"a P14 suite failed: {tail}"
    assert "skipped" not in tail, (
        f"a P14 suite skipped a test, and a skip is a green run that asserted "
        f"nothing: {tail}")


def test_the_phase_still_refuses_to_claim_a_recovery_time():
    """§5, re-asserted at closure — the claim most likely to drift upward.

    Written when detection existed and redirection did not: the temptation was
    to add detection's figure to the drill's promotion time and call the sum an
    RTO, and two things forbade it — the figure was not a time-to-detect, and
    client redirection did not exist.

    **Both of those have changed.** P15-04 built redirection and P14-R2
    composed all three into one sequence, so there is now a real end-to-end
    number AND a real time-to-detect. That makes this test more useful rather
    than less: the row carries a measured figure that LOOKS like a recovery
    time and is not one, and a reader who finds it must still be sent to the
    drill for the interval that is.
    """
    from core.runtime.slo import register

    row = [r for r in register()
           if r["slo"] == "dependency_failure_time_to_detect"][0]
    # INVERTED by P14-R2. The refusal was correct while nothing composed the
    # three mechanisms; the composition exists now and the number was taken
    # against a SIGKILL the drill itself sent. Asserting the absence would be
    # asserting something false — the failure this whole truth pass is about.
    assert row["baseline"] is not None, (
        "the time-to-detect baseline is gone again")
    caveats = " ".join(row["caveats"])
    # The refusal that SURVIVES, and the reason this test still exists: the
    # temptation was always to add detection to promotion and call the sum an
    # RTO. There is now a real end-to-end figure, which makes the temptation
    # stronger rather than weaker — a reader who finds a measured number here
    # must still be told it is not the recovery time.
    assert "still not be a recovery" in caveats, (
        "the row stopped refusing to be read as a recovery time, now that it "
        "carries a number that looks like one")
    assert "failover_e2e_drill.py" in caveats or \
           "failover_e2e_drill.py" in row["baseline"], (
        "nothing points at the drill that measured the whole interval, so a "
        "reader is left to add the parts up — which is what this test exists "
        "to prevent")


