"""Phase 4 batch 04 — production refuses to start on the development key.

CLOUD-ADR-0012 requires it in one sentence: *"Development uses a clearly
labelled development key provider; production refuses to start with it."*

Until this batch there was nothing to refuse. `core/connectors/secrets.py` reads
the KEK from `IRA_CONNECTOR_ENCRYPTION_KEY` and says so honestly — *"Still not a
KMS or an HSM. The KEK comes from the process environment."* — but nothing
NAMED that choice, so no rule could be written against it.

Two refusals are tested here rather than one, and the duplication is the point.
A startup check protects processes that ran it; a worker, a migration script or
a test harness that builds settings directly never does. So the provider lookup
refuses on its own as well, where nothing can reach a key without passing
through it.

What is NOT claimed: that any of this encrypts anything differently. It does
not. The only implemented provider is still the environment variable. What
changes is that using it in production now fails loudly instead of succeeding
quietly, which is the whole of ADR-0012's requirement.
"""
from __future__ import annotations

import secrets as _stdlib_secrets

import pytest

from core.crypto import key_provider
from core.runtime.environment import Environment


class _Cfg:
    """A settings object good enough for `validate_for_environment`.

    It reads with `getattr` only — deliberately, so a renamed setting is a
    reported problem rather than an AttributeError from inside a startup path —
    so a plain object with the right attributes is a faithful stand-in.
    """

    dev_mode = False
    # GENERATED, not written down — and the difference is a CI failure I
    # caused. The first version hardcoded a 64-character hex string, because
    # `_PLACEHOLDER_PATTERNS` rejects a repeated character and the fixture has
    # to look like a real secret to get past the validator. Gitleaks then
    # flagged it as `generic-api-key` at entropy 3.98, correctly: a literal
    # indistinguishable from a signing key IS one as far as a scanner is
    # concerned, and a test file is exactly where a real one eventually gets
    # pasted "temporarily".
    #
    # Generating it leaves nothing in the file to scan, and nothing to copy.
    ira_secret_key = _stdlib_secrets.token_hex(32)
    ira_admin_password = _stdlib_secrets.token_urlsafe(24)
    postgres_password = "a-real-password"
    redis_password = "a-real-password"
    ira_domain = "ira.example.com"
    is_local_domain = False
    ollama_base_url = "http://ollama:11434"
    vllm_fast_url = ""
    vllm_deep_url = ""


# ── The provider lookup ──────────────────────────────────────────────────────


def test_the_default_is_the_development_provider():
    """And that is correct. A default of `aws-kms` would make every fresh
    checkout fail with a credentials error instead of working; the production
    refusal is what makes a permissive default safe."""
    assert key_provider.configured(env={}) == key_provider.ENVIRONMENT
    assert key_provider.is_development_provider(key_provider.DEFAULT)


def test_development_is_permitted_outside_production():
    assert key_provider.resolve(
        environment=Environment.DEVELOPMENT, env={}) == key_provider.ENVIRONMENT


@pytest.mark.parametrize("environment", [Environment.PRODUCTION, Environment.STAGING])
def test_production_refuses_the_defaulted_development_provider(environment):
    """The requirement, in the environments it applies to.

    Staging is included because `Environment.is_production_like` includes it,
    and for the reason that property records: staging is production-shaped, not
    a second development environment.
    """
    with pytest.raises(key_provider.KeyProviderError) as caught:
        key_provider.resolve(environment=environment, env={})
    message = str(caught.value)
    assert "DEVELOPMENT provider" in message
    assert environment.value in message


@pytest.mark.parametrize("environment", [Environment.PRODUCTION, Environment.STAGING])
def test_production_permits_it_only_when_written_down(environment):
    """**Where the line is, and why it is not further left.**

    Refusing an EXPLICIT `IRA_KEK_PROVIDER=environment` would make production
    unstartable outright — the environment provider is the only one this build
    implements, and a real KMS is Phase 15. A rule that makes every deployment
    impossible does not get obeyed; it gets deleted, along with the reasoning
    that produced it.

    ADR-0012's danger is "production uses it without anybody noticing". An
    operator who writes it down has noticed, and it is in their configuration
    where a reviewer can see it.
    """
    assert key_provider.resolve(
        environment=environment,
        env={key_provider.PROVIDER_ENV: key_provider.ENVIRONMENT}
    ) == key_provider.ENVIRONMENT


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_an_empty_setting_is_not_a_choice(value):
    """**A plant walked past this.**

    `IRA_KEK_PROVIDER=` in an env file is set-but-empty, which is what a
    half-finished configuration looks like — a variable somebody added and never
    filled in. Treating its presence as deliberateness would let production boot
    on the development key on the strength of an empty string.

    The first version of `was_chosen` tested membership rather than content, and
    nothing caught it. This does.
    """
    assert not key_provider.was_chosen(env={key_provider.PROVIDER_ENV: value})
    with pytest.raises(key_provider.KeyProviderError):
        key_provider.resolve(environment=Environment.PRODUCTION,
                             env={key_provider.PROVIDER_ENV: value})


def test_the_refusal_explains_what_is_actually_at_risk():
    """A refusal an operator cannot act on gets overridden. This one names the
    consequence rather than the rule."""
    with pytest.raises(key_provider.KeyProviderError) as caught:
        key_provider.resolve(environment=Environment.PRODUCTION, env={})
    message = str(caught.value)
    assert "reads every data-encryption key" in message
    assert "CLOUD-ADR-0012" in message
    # And it says what to actually do, which a refusal with no exit does not.
    assert key_provider.PROVIDER_ENV in message and "Phase 15" in message


def test_a_planned_provider_is_refused_rather_than_silently_ignored():
    """**The failure mode that would be worst.**

    An operator who sets `aws-kms` has decided their keys live in a managed
    store. Falling back to the environment variable would use the development
    key while the configuration said otherwise — the deployment would look
    correctly configured and be exactly as exposed as before.
    """
    for name in sorted(key_provider.PLANNED):
        with pytest.raises(key_provider.KeyProviderError) as caught:
            key_provider.resolve(environment=Environment.PRODUCTION,
                                 env={key_provider.PROVIDER_ENV: name})
        assert "planned" in str(caught.value)
        assert "no fallback" in str(caught.value).lower()


def test_a_planned_provider_is_refused_in_development_too():
    """Not a production-only rule. Asking for a provider this build does not
    have is a broken configuration everywhere — and refusing only in production
    means the mistake is first discovered there.

    The example moved in P4-10: `aws-kms` used to be the planned provider and is
    now implemented, so this uses one that is still planned. Picking the name
    from `PLANNED` rather than hard-coding it means the next provider to be
    implemented cannot make this test silently assert nothing.
    """
    still_planned = sorted(key_provider.PLANNED)
    assert still_planned, (
        "every named provider is implemented, so there is nothing for this "
        "test to be about — delete it or add the next one deliberately")
    with pytest.raises(key_provider.KeyProviderError):
        key_provider.resolve(environment=Environment.DEVELOPMENT,
                             env={key_provider.PROVIDER_ENV: still_planned[0]})


def test_a_typo_is_distinguishable_from_an_unimplemented_provider():
    """Different operator mistakes with different fixes: one is a spelling
    error, the other is asking this build for something it cannot do."""
    with pytest.raises(key_provider.KeyProviderError) as caught:
        key_provider.resolve(environment=Environment.DEVELOPMENT,
                             env={key_provider.PROVIDER_ENV: "aws_kms_typo"})
    assert "is not a key provider this build knows" in str(caught.value)


def test_no_planned_provider_is_also_listed_as_implemented():
    """The two sets are what `resolve` branches on. An overlap would make a
    provider both usable and refused, and which branch won would depend on
    statement order."""
    assert not (key_provider.PLANNED & key_provider.IMPLEMENTED)
    assert key_provider.DEFAULT in key_provider.IMPLEMENTED


# ── The startup gate ─────────────────────────────────────────────────────────


def test_startup_validation_reports_the_defaulted_provider(monkeypatch):
    from core.runtime.environment import validate_for_environment

    monkeypatch.delenv(key_provider.PROVIDER_ENV, raising=False)
    problems = validate_for_environment(_Cfg(), Environment.PRODUCTION)
    assert any("development provider" in p for p in problems), problems


def test_startup_validation_accepts_it_once_written_down(monkeypatch):
    from core.runtime.environment import validate_for_environment

    monkeypatch.setenv(key_provider.PROVIDER_ENV, key_provider.ENVIRONMENT)
    assert validate_for_environment(_Cfg(), Environment.PRODUCTION) == []


def test_startup_validation_is_silent_outside_production(monkeypatch):
    from core.runtime.environment import validate_for_environment

    monkeypatch.delenv(key_provider.PROVIDER_ENV, raising=False)
    assert validate_for_environment(_Cfg(), Environment.DEVELOPMENT) == []


def test_startup_refuses_to_start_rather_than_only_reporting(monkeypatch):
    """`validate_for_environment` returns problems; `enforce` is what raises.
    A rule that only ever produced a list nobody read would be documentation."""
    from core.runtime.environment import ConfigurationError, enforce

    monkeypatch.delenv(key_provider.PROVIDER_ENV, raising=False)
    with pytest.raises(ConfigurationError):
        enforce(_Cfg(), Environment.PRODUCTION)


def test_a_provider_that_cannot_be_determined_is_a_problem_not_a_pass(monkeypatch):
    """A validator that cannot evaluate a rule has not satisfied it.

    Swallowing the failure would mean a deployment whose provider lookup is
    broken passes the check that exists to constrain it — the same
    "cannot see it, so it must be fine" confusion that made a status view report
    contract_ready on a table full of plaintext.
    """
    from core.runtime import environment as env_module

    def explode(*_a, **_k):
        raise RuntimeError("planted: the provider lookup is broken")

    monkeypatch.setattr(key_provider, "configured", explode)
    problems = env_module.validate_for_environment(_Cfg(), Environment.PRODUCTION)
    assert any("could not be determined" in p for p in problems), problems


def test_the_startup_gate_and_the_lookup_agree(monkeypatch):
    """Two refusals for one rule, and they must not disagree — a startup that
    permits what the lookup refuses is a process that starts and then cannot
    read a credential."""
    from core.runtime.environment import validate_for_environment

    monkeypatch.delenv(key_provider.PROVIDER_ENV, raising=False)
    for environment in (Environment.PRODUCTION, Environment.STAGING,
                        Environment.DEVELOPMENT):
        # Matched on the variable NAME, not on prose. The message is written
        # for an operator and will be reworded; the name is the contract.
        reported = any(key_provider.PROVIDER_ENV in p
                       for p in validate_for_environment(_Cfg(), environment))
        try:
            key_provider.resolve(environment=environment, env={})
            refused = False
        except key_provider.KeyProviderError:
            refused = True
        assert reported == refused, (
            f"{environment.value}: startup reported={reported} but the lookup "
            f"refused={refused}")


# ── What is not claimed ──────────────────────────────────────────────────────


def test_the_implemented_set_matches_what_exists():
    """P4-04 asserted `IMPLEMENTED == {environment}` and that `aws-kms` was
    merely planned. Both were true then. **P4-10 implemented AWS KMS**, so the
    assertion is inverted rather than deleted: a claim about which providers
    exist has to keep tracking the code, in whichever direction it moves.

    The module for each implemented provider must be importable, which is what
    stops a name being added to the set as an intention.
    """
    assert key_provider.ENVIRONMENT in key_provider.IMPLEMENTED
    assert key_provider.AWS_KMS in key_provider.IMPLEMENTED
    assert key_provider.AWS_KMS not in key_provider.PLANNED

    import importlib

    importlib.import_module("core.crypto.aws_kms")


def test_every_implemented_provider_is_reachable_from_the_kek_load_path():
    """A provider in the set that `secrets._load_key` does not route to is the
    "correct mechanism with no callers" shape — selectable, refused by nothing,
    and silently using the development key."""
    import pathlib as _pathlib

    from core.connectors import secrets as secrets_module

    source = _pathlib.Path(secrets_module.__file__).read_text(encoding="utf-8")
    for name in key_provider.IMPLEMENTED:
        if name == key_provider.ENVIRONMENT:
            continue  # the fallthrough path itself
        assert "key_provider" in source and "aws_kms" in source, (
            f"{name} is implemented but _load_key does not route to it")
