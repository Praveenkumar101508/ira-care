"""M2 — the external execution gate.

IRA CARE (D-056): there is no hosted model API. These tests prove that no
approval, no environment variable and no registered executor can make a model
call leave the machine. Formerly they proved the gate needed (a) an approved
external decision, (b) the IRA_ALLOW_EXTERNAL_API master switch, and (c) an
explicitly registered executor. Local decisions always run locally.
"""
import pytest
from dataclasses import replace

from reasoning.model_availability import ModelAvailability
from reasoning.model_profiles import reload_config
from reasoning.model_router import (
    ExternalExecutorNotConfigured,
    apply_consent,
    clear_external_executor,
    register_external_executor,
    route,
    run_decision,
)

UNKNOWN = ModelAvailability(reachable=False, models=frozenset(), source="test")
VERY_HARD = "do a very deep architecture and security refactor of the entire platform"


@pytest.fixture(autouse=True)
def _fresh():
    reload_config()
    clear_external_executor()
    yield
    clear_external_executor()
    reload_config()


def _local_runner(decision):
    return f"local:{decision.selected_model}"


def test_local_decision_runs_locally():
    d = route("hi", env={}, availability=UNKNOWN)
    out = run_decision(d, local_runner=_local_runner, env={})
    assert out.startswith("local:")


def test_an_approved_decision_stays_local_and_an_external_one_cannot_run():
    """D-056: there is no hosted model. Approval changes nothing; a hand-built
    external decision is refused at execution time whatever the environment says."""
    env = {"IRA_ALLOW_EXTERNAL_API": "true"}
    d = route(VERY_HARD, env=env, availability=UNKNOWN)
    final = apply_consent(d, approved=True, env=env)
    assert final.provider == "local" and "no hosted model" in final.reason
    forged = replace(final, provider="external")
    with pytest.raises(ExternalExecutorNotConfigured):
        run_decision(forged, local_runner=_local_runner, env=env)


def test_registering_an_external_executor_is_refused():
    with pytest.raises(ExternalExecutorNotConfigured):
        register_external_executor(lambda d, s, p: "EXTERNAL CALLED")
    clear_external_executor()   # harmless, kept for older callers


def test_no_environment_turns_an_external_model_on():
    for env in ({}, {"IRA_ALLOW_EXTERNAL_API": "true"},
                {"IRA_ALLOW_EXTERNAL_API": "true", "IRA_EXTERNAL_API_MODEL": "acme-frontier", "IRA_EXTERNAL_API_PROVIDER": "acme"}):
        d = route(VERY_HARD, env=env, availability=UNKNOWN)
        final = apply_consent(d, approved=True, env=env)
        assert final.provider == "local"
        assert final.selected_model == d.selected_model, "the local model is the answer, not a safety net"
        assert run_decision(final, local_runner=_local_runner, env=env).startswith("local:")


def test_declined_decision_runs_locally():
    env = {"IRA_ALLOW_EXTERNAL_API": "true"}
    d = route(VERY_HARD, env=env, availability=UNKNOWN)
    final = apply_consent(d, approved=False, env=env)  # user said "Local only"
    out = run_decision(final, local_runner=_local_runner, env=env)
    assert out.startswith("local:")