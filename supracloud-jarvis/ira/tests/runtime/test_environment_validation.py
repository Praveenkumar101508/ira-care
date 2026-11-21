"""Production configuration must fail closed, not warn.

The existing `_validate_config` in `app/lifespan.py` logs warnings and starts
anyway. These tests lock in the second behaviour that Phase 1 adds: in staging
and production, unsafe configuration *raises* and the process does not start.

The distinction these tests protect is the whole point of the module. A warning
about a default admin password is read by a developer on a laptop and by nobody
at all on an instance that autoscaled up at 3am.
"""
from __future__ import annotations

import pytest

from core.runtime.environment import (
    ConfigurationError,
    Environment,
    enforce,
    get_environment,
    is_placeholder,
    validate_for_environment,
)


class FakeSettings:
    """A settings object that is safe by default; tests break one thing at a time."""

    def __init__(self, **overrides):
        self.dev_mode = False
        self.ira_secret_key = "a" * 64
        self.ira_admin_password = "a-properly-long-password"
        self.postgres_password = "a-real-postgres-password"
        self.redis_password = "a-real-redis-password"
        self.ira_domain = "ira.example.com"
        self.is_local_domain = False
        self.ollama_base_url = "http://ollama:11434/v1"
        self.vllm_fast_url = "http://vllm-fast:8001/v1"
        self.vllm_deep_url = "http://vllm-deep:8002/v1"
        for k, v in overrides.items():
            setattr(self, k, v)


# ── Environment resolution ───────────────────────────────────────────────────

def test_unset_environment_defaults_to_development():
    assert get_environment("") is Environment.DEVELOPMENT
    assert get_environment(None if False else "") is Environment.DEVELOPMENT


@pytest.mark.parametrize("raw,expected", [
    ("development", Environment.DEVELOPMENT),
    ("test", Environment.TEST),
    ("staging", Environment.STAGING),
    ("production", Environment.PRODUCTION),
    ("  PRODUCTION  ", Environment.PRODUCTION),
])
def test_known_environments_resolve(raw, expected):
    assert get_environment(raw) is expected


def test_a_typo_raises_rather_than_defaulting_to_development():
    """The failure mode this prevents: IRA_ENVIRONMENT=produciton silently
    disabling every production rule on the one deployment that needs them."""
    with pytest.raises(ConfigurationError) as exc:
        get_environment("produciton")
    assert "produciton" in str(exc.value)


def test_staging_is_production_like():
    assert Environment.STAGING.is_production_like
    assert Environment.PRODUCTION.is_production_like
    assert not Environment.DEVELOPMENT.is_production_like
    assert not Environment.TEST.is_production_like


# ── Development and test are unaffected ──────────────────────────────────────

@pytest.mark.parametrize("env", [Environment.DEVELOPMENT, Environment.TEST])
def test_development_and_test_allow_everything(env):
    """Phase 1 must not change behaviour for an existing private install."""
    unsafe = FakeSettings(
        dev_mode=True,
        ira_secret_key="CHANGE_ME_ira_secret_key_here",
        ira_admin_password="admin",
        postgres_password="",
        ira_domain="localhost",
        is_local_domain=True,
    )
    assert validate_for_environment(unsafe, env) == []
    assert enforce(unsafe, env) is env


# ── Production rules ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _kek_provider_chosen(monkeypatch):
    """A production-safe configuration now has to name its key provider.

    Phase 4 batch 04 added a rule: production refuses to boot on the DEFAULT
    key-encryption-key provider, because the default is the development one and
    ADR-0012's stated danger is "production uses it without anybody noticing".
    An explicit `IRA_KEK_PROVIDER=environment` is an operator writing down that
    they accept the exposure until Phase 15 binds a real KMS.

    So `FakeSettings` is no longer a complete production configuration on its
    own, and this fixture supplies the missing half rather than the rule being
    weakened to suit the fixture. `tests/tenancy/test_p4_key_provider.py` is
    where the rule itself is tested, including that the UNSET case is refused.
    """
    from core.crypto import key_provider

    monkeypatch.setenv(key_provider.PROVIDER_ENV, key_provider.ENVIRONMENT)


def test_a_safe_configuration_passes_production():
    assert validate_for_environment(FakeSettings(), Environment.PRODUCTION) == []


def test_dev_mode_is_refused_in_production():
    problems = validate_for_environment(FakeSettings(dev_mode=True), Environment.PRODUCTION)
    assert any("DEV_MODE" in p for p in problems)


@pytest.mark.parametrize("value", [
    "", "CHANGE_ME_ira_secret_key_here", "changeme", "secret", "your-key-here",
    "<your-secret>", "ci-test-secret-key",
])
def test_placeholder_secret_keys_are_refused(value):
    problems = validate_for_environment(
        FakeSettings(ira_secret_key=value), Environment.PRODUCTION)
    assert any("IRA_SECRET_KEY" in p for p in problems)


def test_short_secret_key_is_refused():
    problems = validate_for_environment(
        FakeSettings(ira_secret_key="short-but-not-a-placeholder"), Environment.PRODUCTION)
    assert any("IRA_SECRET_KEY" in p and "shorter" in p for p in problems)


@pytest.mark.parametrize("value", ["admin", "password", "", "CHANGE_ME_strong_password_here"])
def test_default_admin_passwords_are_refused(value):
    problems = validate_for_environment(
        FakeSettings(ira_admin_password=value), Environment.PRODUCTION)
    assert any("IRA_ADMIN_PASSWORD" in p for p in problems)


def test_short_admin_password_is_refused():
    problems = validate_for_environment(
        FakeSettings(ira_admin_password="tooshort"), Environment.PRODUCTION)
    assert any("IRA_ADMIN_PASSWORD" in p for p in problems)


@pytest.mark.parametrize("field", ["postgres_password", "redis_password"])
def test_missing_infrastructure_credentials_are_refused(field):
    problems = validate_for_environment(
        FakeSettings(**{field: ""}), Environment.PRODUCTION)
    assert any(field.upper() in p for p in problems)


def test_local_domain_in_production_is_refused_because_cors_depends_on_it():
    problems = validate_for_environment(
        FakeSettings(ira_domain="localhost", is_local_domain=True), Environment.PRODUCTION)
    assert any("IRA_DOMAIN" in p and "CORS" in p for p in problems)


def test_missing_domain_is_refused():
    problems = validate_for_environment(FakeSettings(ira_domain=""), Environment.PRODUCTION)
    assert any("IRA_DOMAIN" in p for p in problems)


# ── Model runtime exposure ───────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://localhost:11434/v1",
    "http://127.0.0.1:11434/v1",
    "http://10.0.1.5:11434/v1",
    "http://192.168.1.10:11434/v1",
    "http://172.16.0.4:11434/v1",
    "http://ollama:11434/v1",          # Docker service name
    "http://vllm-fast:8001/v1",        # hyphenated service name
    "http://ollama.internal:11434/v1",
])
def test_private_model_endpoints_are_accepted(url):
    assert validate_for_environment(
        FakeSettings(ollama_base_url=url), Environment.PRODUCTION) == []


@pytest.mark.parametrize("url", [
    "http://models.example.com:11434/v1",
    "https://ollama.public-host.net/v1",
    "http://8.8.8.8:11434/v1",
])
def test_public_model_endpoints_are_refused(url):
    problems = validate_for_environment(
        FakeSettings(ollama_base_url=url), Environment.PRODUCTION)
    assert any("OLLAMA_BASE_URL" in p for p in problems)


# ── enforce() raises, and the message is safe ────────────────────────────────

def test_enforce_raises_in_production_and_lists_every_problem():
    cfg = FakeSettings(dev_mode=True, ira_secret_key="", ira_admin_password="admin")
    with pytest.raises(ConfigurationError) as exc:
        enforce(cfg, Environment.PRODUCTION)
    assert len(exc.value.problems) >= 3


def test_the_error_message_never_contains_a_secret_value():
    """The message is designed to be logged and pasted into an incident channel,
    so it names settings and never their values."""
    secret = "hunter2-this-is-the-actual-secret-value"
    cfg = FakeSettings(ira_secret_key=secret[:10], ira_admin_password=secret)
    with pytest.raises(ConfigurationError) as exc:
        enforce(cfg, Environment.PRODUCTION)
    message = str(exc.value)
    assert secret not in message
    assert secret[:10] not in message
    assert "IRA_SECRET_KEY" in message


def test_enforce_is_a_noop_in_development():
    cfg = FakeSettings(dev_mode=True, ira_secret_key="")
    assert enforce(cfg, Environment.DEVELOPMENT) is Environment.DEVELOPMENT


# ── Placeholder detection ────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    None, "", "   ", "CHANGE_ME", "change-me", "CHANGE_ME_your_name",
    "your_password", "placeholder", "example", "xxx", "TODO", "<secret>",
    "admin", "password", "root",
])
def test_placeholders_are_detected(value):
    assert is_placeholder(value)


@pytest.mark.parametrize("value", [
    "a" * 64, "correct-horse-battery-staple", "Kx9#mQ2$vL8@nR4!", "ira.example.com",
])
def test_real_values_are_not_placeholders(value):
    assert not is_placeholder(value)
