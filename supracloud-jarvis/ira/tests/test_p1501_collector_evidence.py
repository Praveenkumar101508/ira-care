"""P15-01 — FINDING AG: two SLOs were alertable on the strength of a string.

Phase 15's first batch, taken first because it needs no infrastructure and it
decides part of what the rest of the phase is for.

── The finding ─────────────────────────────────────────────────────────────

`register()` reported, on the merged trunk at `7ebdc19`:

    readiness_availability   collected_externally   alertable=True
    http_5xx_rate            collected_externally   alertable=True

and the derivation was:

    present = bool(self.probe())
    if not present:
        return Collection.NONE
    return Collection.EXTERNAL if self.recorded_by else Collection.IN_PROCESS

So `probe()` decided only NONE-vs-not-NONE. Whether a signal counted as
externally collected — and therefore **alertable** — rested on whether
`recorded_by` was a **non-empty string literal**.

Neither recorder exists:

* `readiness_state` names *"the orchestrator's own probe history"*. **Nothing
  polls `/readyz` anywhere in this repository.** The only Compose healthchecks
  target the model services' `/health` on ports 8003/8004, and a Docker
  healthcheck keeps a last state and a retry count — not a history.
* `http_error_rate` names *"the reverse proxy access log"*. **No Compose file
  defines a proxy service** — the services are `ira-api`, four vLLM containers,
  `frontend`, `ollama`, `postgres`, `redis`. The only `nginx.conf` in the tree
  is under `future-scale/`, which by its own name is not the current deployment.

The module forbids this reasoning in its own words, in the comment above the
probes:

    Each answers one question by execution. None of them is allowed to be a
    constant: a probe returning a literal is a declaration wearing a probe's
    name.

P13-05 applied that rule to the `probe` half and caught `_process_records_metrics`
shipping as a fail-open blacklist. **The `recorded_by` half was never subjected
to it**, and it is the half that decides `alertable`.

── The asymmetry, kept rather than flattened ───────────────────────────────

The two are not equally weak, and saying so matters:

* `http_error_rate`'s probe checks a **configured** `nginx_log_path`, and
  `worker/security_monitor.py` really does read that file. There is some
  evidence — of a configured path, not of a proxy writing to it.
* `readiness_state`'s probe checks **routing only**. There is no evidence about
  the recorder at all.

── The fix, and the direction it fails in ──────────────────────────────────

`EXTERNAL` now requires a `recorder_probe`: positive evidence that the named
system is present. Absent or unrecognised → `NOT_COLLECTED`, which is the
direction that can only **under**-claim — the same repair
`_process_records_metrics` received. Each probe becomes True on its own the day
the thing it looks for is deployed, which is the whole design of this register.
"""
from __future__ import annotations

import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")


# ── FINDING AG: the values, pinned ──────────────────────────────────────────


def test_no_objective_is_alertable_on_a_recorder_that_does_not_exist():
    """The finding itself, as values rather than as reasoning.

    Both were `alertable=True` before this batch. If either goes back to True,
    either somebody deployed the recorder — in which case this test should be
    updated with the evidence — or the string-literal derivation is back.
    """
    from core.runtime.slo import register

    rows = {r["slo"]: r for r in register()}

    readiness = rows["readiness_availability"]
    assert readiness["alertable"] is False, (
        "readiness_availability is alertable again. Nothing in this repository "
        "polls /readyz; if that changed, say where and update this test.")
    assert readiness["collection"] == "not_collected"

    errors = rows["http_5xx_rate"]
    assert errors["alertable"] is False, (
        "http_5xx_rate is alertable again. No Compose file defines a reverse "
        "proxy; if one was added, update this test with the service name.")
    assert errors["collection"] == "not_collected"


def test_the_register_still_names_what_it_cannot_alert_on():
    """The deliverable of P13-05 is *saying so*, and the list just grew.

    Written as an exact set. The version this replaces compared `unalertable()`
    against `register()` — **a tautology**: it asserted the two functions agree
    with each other, and would have passed just as happily with everything
    alertable or nothing. A test that cannot fail for the property it names is
    the failure this programme keeps finding, and it was sitting inside the
    module written to prevent it.
    """
    from core.runtime.slo import register, unalertable

    assert set(unalertable()) == {
        "readiness_availability",
        "http_5xx_rate",
        "http_request_latency_p95",
    }
    # …and still consistent with the register, which is the weaker property the
    # old test asserted. Kept, because it is worth having as well.
    assert set(unalertable()) == {r["slo"] for r in register()
                                  if not r["alertable"]}


# ── The mechanism ───────────────────────────────────────────────────────────


def _signal(**kw):
    from core.runtime.slo import Signal

    base = dict(name="s", what="w", recorded_by="", probe=lambda: True)
    base.update(kw)
    return Signal(**base)


def test_a_named_recorder_with_no_probe_is_not_collected():
    """The exact defect, at the unit that produced it.

    Under the old derivation this returned `EXTERNAL`, and it was the ONLY case
    that could — every EXTERNAL signal in the register reached that state this
    way.
    """
    from core.runtime.slo import Collection

    signal = _signal(recorded_by="a system nobody has checked for")
    assert signal.collection() is Collection.NONE, (
        "a recorder named in a string is being counted as collection")


def test_a_recorder_probe_that_finds_nothing_is_not_collected():
    from core.runtime.slo import Collection

    signal = _signal(recorded_by="a proxy", recorder_probe=lambda: False)
    assert signal.collection() is Collection.NONE


def test_a_recorder_probe_that_finds_something_IS_external():
    """The control. Returning NONE always would satisfy every test above and
    would make the EXTERNAL state unreachable — which is not the finding."""
    from core.runtime.slo import Collection

    signal = _signal(recorded_by="a proxy", recorder_probe=lambda: True)
    assert signal.collection() is Collection.EXTERNAL


def test_a_recorder_probe_that_raises_is_not_collected():
    """Same rule as the signal probe: not proved is not collected.

    A recorder probe that raises has established nothing, and failing the other
    way is how a register starts claiming signals nobody has.
    """
    from core.runtime.slo import Collection

    def boom():
        raise RuntimeError("cannot tell")

    assert _signal(recorded_by="x", recorder_probe=boom).collection() is Collection.NONE


def test_an_in_process_signal_is_unaffected():
    """`recorded_by=""` means this process records it, and that path must not
    have been disturbed — two objectives depend on it."""
    from core.runtime.slo import Collection

    assert _signal(recorded_by="").collection() is Collection.IN_PROCESS


# ── The recorder probes are probes, not constants ───────────────────────────


def test_the_readiness_recorder_probe_reads_the_deployment(tmp_path,
                                                            monkeypatch):
    """It must flip to True when a poller is actually configured.

    A probe that returns False unconditionally would pass every assertion above
    while being exactly the constant this module forbids. Driven by writing a
    Compose file with a `/readyz` healthcheck and pointing the probe at it.
    """
    import yaml

    from core.runtime import slo

    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump({
        "services": {"ira-api": {"image": "ira", "healthcheck": {
            "test": ["CMD-SHELL", "curl -sf http://localhost:8000/readyz || exit 1"]}}}
    }), encoding="utf-8")

    real = slo._compose_documents
    monkeypatch.setattr(slo, "_compose_documents",
                        lambda: [yaml.safe_load((tmp_path / "docker-compose.yml")
                                                .read_text(encoding="utf-8"))])
    assert slo._something_polls_readiness() is True, (
        "a configured /readyz healthcheck was not recognised, so this probe is "
        "a constant wearing a probe's name")

    monkeypatch.setattr(slo, "_compose_documents", real)
    assert slo._something_polls_readiness() is False, (
        "something now polls /readyz in this repository — find it, and replace "
        "this assertion with one naming it")


def test_the_proxy_recorder_probe_reads_the_deployment(monkeypatch):
    """Same, for the reverse proxy. An allow-list of images, not a blacklist.

    `_process_records_metrics` shipped as a blacklist and read an unrecognised
    type as *collected*. An unrecognised image here must read as **no proxy**.
    """
    from core.runtime import slo

    monkeypatch.setattr(slo, "_compose_documents",
                        lambda: [{"services": {"web": {"image": "nginx:1.27"}}}])
    assert slo._a_reverse_proxy_is_deployed() is True

    monkeypatch.setattr(slo, "_compose_documents",
                        lambda: [{"services": {"web": {"image": "some-unknown-image"}}}])
    assert slo._a_reverse_proxy_is_deployed() is False, (
        "an unrecognised image is being read as a reverse proxy — that is the "
        "fail-open blacklist P13-05 already fixed once")


def test_no_reverse_proxy_is_deployed_today():
    """The measured fact the finding rests on, asserted so it stays checked."""
    from core.runtime.slo import _a_reverse_proxy_is_deployed

    assert _a_reverse_proxy_is_deployed() is False, (
        "a proxy service now exists in a Compose file — http_5xx_rate may "
        "legitimately be alertable, so update the finding and the tests above")


# ── What this batch does NOT claim ──────────────────────────────────────────


def test_finding_a_poller_would_still_not_prove_it_keeps_history():
    """The limit, written into the probe rather than assumed away.

    `readiness_availability` is a **30-day rolling** objective. A Docker
    healthcheck keeps a last state and a retry count; finding one proves
    something polls, not that anything retains a series. The probe's docstring
    must say so, or a future deployment with a healthcheck would flip this SLO
    to alertable on evidence that does not support a 30-day window.

    Whitespace is normalised before matching. The first version asserted on the
    raw docstring and failed against text that plainly contained the phrase —
    "a retry\\ncount" is not "retry count". An assertion coupled to where a line
    happens to wrap is testing the formatter, not the claim.
    """
    import re

    from core.runtime.slo import _something_polls_readiness

    doc = re.sub(r"\s+", " ", _something_polls_readiness.__doc__ or "")
    assert "not a series" in doc, (
        "the readiness recorder probe no longer states that polling is not "
        "history")
    assert "retry count" in doc
    assert "never be read as" in doc, (
        "the probe no longer says what a True from it must NOT be taken to mean")
