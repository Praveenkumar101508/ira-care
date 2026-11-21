"""Phase 1 — edition resolution and edition-scoped defaults.

The acceptance criterion these guard: **the private edition's behaviour is
unchanged.** Every default that Phase 1 made edition-aware must still resolve to
exactly what it resolved to before, when the edition is private.
"""
from __future__ import annotations

import pytest

from core.editions import (
    DEFAULT_EDITION,
    EDITION_CLOUD,
    EDITION_PRIVATE,
    UnknownEditionError,
    current_edition,
    edition_default,
    edition_default_keys,
    is_cloud,
    is_private,
    normalise_edition,
)

_INFRA_ENV = {
    "IRA_SECRET_KEY": "ci-secret",
    "IRA_ADMIN_PASSWORD": "ci-admin",
    "POSTGRES_PASSWORD": "ci-db",
    "REDIS_PASSWORD": "ci-redis",
}


#: Flags that must be cleared before building Settings unless a test sets them.
#: The full suite runs with DEV_MODE=true in the environment (see
#: .github/workflows/test.yml), and the cloud edition refuses DEV_MODE — so
#: without this the cloud cases would fail for a reason unrelated to what they
#: are testing.
_MUST_BE_UNSET = ("DEV_MODE", "ARMORY_ENABLED", "BROWSER_AGENT_ENABLED", "IRA_PRIVACY_MODE")


def _settings(monkeypatch, **env):
    from config import get_settings

    for key in _MUST_BE_UNSET:
        if key not in env:
            monkeypatch.delenv(key, raising=False)
    for k, v in {**_INFRA_ENV, **env}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    from config import Settings

    return Settings()


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_default_edition_is_private():
    """An install that never sets IRA_EDITION keeps today's behaviour."""
    assert DEFAULT_EDITION == EDITION_PRIVATE
    assert normalise_edition(None) == EDITION_PRIVATE
    assert normalise_edition("") == EDITION_PRIVATE
    assert normalise_edition("   ") == EDITION_PRIVATE


@pytest.mark.parametrize("raw", ["private", "PRIVATE", " Private ", "cloud", "CLOUD"])
def test_known_editions_parse(raw):
    assert normalise_edition(raw) in (EDITION_PRIVATE, EDITION_CLOUD)


@pytest.mark.parametrize("raw", ["banana", "prod", "privat", "cloud-eu", "both"])
def test_unknown_edition_is_fatal(raw):
    """Not coerced to a default: guessing which posture the operator meant is
    exactly the silent assumption the edition split removes."""
    with pytest.raises(UnknownEditionError):
        normalise_edition(raw)


def test_current_edition_reads_the_environment(monkeypatch):
    monkeypatch.delenv("IRA_EDITION", raising=False)
    assert current_edition() == EDITION_PRIVATE
    assert is_private() and not is_cloud()

    monkeypatch.setenv("IRA_EDITION", "cloud")
    assert current_edition() == EDITION_CLOUD
    assert is_cloud() and not is_private()


# ── The defaults table ───────────────────────────────────────────────────────

def test_every_key_defines_both_editions():
    """A key that only answers for one edition silently returns None for the
    other, which reads as 'off' and hides the omission."""
    for key in edition_default_keys():
        assert edition_default(key, EDITION_PRIVATE) is not None, key
        assert edition_default(key, EDITION_CLOUD) is not None, key


def test_unknown_default_key_raises():
    with pytest.raises(KeyError):
        edition_default("no_such_default")


# ── Phase 0's deferred decisions, now resolved ───────────────────────────────

def test_browser_agent_private_unchanged_cloud_off():
    """Phase 0 deferred this rather than altering local behaviour. Private stays
    true (unchanged); cloud is false until browsing goes through the Permission
    Broker and produces an Action Receipt."""
    assert edition_default("browser_agent_enabled", EDITION_PRIVATE) is True
    assert edition_default("browser_agent_enabled", EDITION_CLOUD) is False


def test_privacy_mode_cloud_is_strictest_until_disclosure_exists():
    assert edition_default("privacy_mode", EDITION_PRIVATE) == "local_first"
    assert edition_default("privacy_mode", EDITION_CLOUD) == "local_only"


def test_owner_tooling_is_private_only():
    for key in ("armory_available", "forge_available", "local_filesystem_executors"):
        assert edition_default(key, EDITION_PRIVATE) is True, key
        assert edition_default(key, EDITION_CLOUD) is False, key


# ── Settings integration ─────────────────────────────────────────────────────

def test_private_settings_are_byte_for_byte_unchanged(monkeypatch):
    """The whole point of Phase 1: nothing about IRA Private moves."""
    monkeypatch.delenv("IRA_EDITION", raising=False)
    monkeypatch.delenv("BROWSER_AGENT_ENABLED", raising=False)
    monkeypatch.delenv("IRA_PRIVACY_MODE", raising=False)

    s = _settings(monkeypatch)

    assert s.ira_edition == EDITION_PRIVATE
    assert s.browser_agent_enabled is True     # as it was before Phase 1
    assert s.ira_privacy_mode == "local_first"  # as it was before Phase 1


def test_cloud_settings_tighten_unset_defaults(monkeypatch):
    monkeypatch.delenv("BROWSER_AGENT_ENABLED", raising=False)
    monkeypatch.delenv("IRA_PRIVACY_MODE", raising=False)

    s = _settings(monkeypatch, IRA_EDITION="cloud")

    assert s.ira_edition == EDITION_CLOUD
    assert s.browser_agent_enabled is False
    assert s.ira_privacy_mode == "local_only"


def test_explicit_value_beats_the_edition_default(monkeypatch):
    """Edition defaults fill gaps; they never override a deliberate choice."""
    s = _settings(monkeypatch, IRA_EDITION="cloud", BROWSER_AGENT_ENABLED="true")
    assert s.browser_agent_enabled is True


def test_cloud_refuses_armory(monkeypatch):
    with pytest.raises(Exception, match="ARMORY_ENABLED"):
        _settings(monkeypatch, IRA_EDITION="cloud", ARMORY_ENABLED="true")


def test_cloud_refuses_dev_mode(monkeypatch):
    """In a multi-tenant service, bypassing auth means bypassing tenant isolation."""
    with pytest.raises(Exception, match="DEV_MODE"):
        _settings(monkeypatch, IRA_EDITION="cloud", DEV_MODE="true", IRA_DOMAIN="localhost")


def test_unknown_edition_refuses_startup(monkeypatch):
    with pytest.raises(Exception):
        _settings(monkeypatch, IRA_EDITION="banana")


# ── Phase 8: the privacy-mode deferral ───────────────────────────────────────


def test_the_cloud_privacy_default_is_still_the_stricter_one():
    """Phase 8 cleared the blocker; it did not change the default.

    The Phase 0 deferral said cloud stays `local_only` until the model gateway
    can disclose which provider handled a request. That gateway now exists, so
    the CONDITION is met — but loosening a shipped privacy default is a product
    decision, and making it silently in the phase that cleared the blocker is
    exactly how a posture drifts. This test pins the default so a future change
    is deliberate and visible in a diff.
    """
    from core.editions import EDITION_CLOUD, edition_default

    assert edition_default("privacy_mode", EDITION_CLOUD) == "local_only"


def test_the_private_privacy_default_is_unchanged_by_phase_8():
    from core.editions import EDITION_PRIVATE, edition_default

    assert edition_default("privacy_mode", EDITION_PRIVATE) == "local_first"


def test_every_edition_privacy_default_is_a_mode_the_router_implements():
    """A default naming a mode the router does not have would resolve to the
    strictest at runtime — safe, but silently not what was configured."""
    from core.editions import EDITIONS, edition_default
    from core.models.policy import PrivacyMode, normalise_mode

    for edition in EDITIONS:
        value = str(edition_default("privacy_mode", edition))
        assert value in {m.value for m in PrivacyMode}, (
            f"{edition}'s privacy_mode default {value!r} is not a mode the "
            f"router implements"
        )
        assert normalise_mode(value).value == value
