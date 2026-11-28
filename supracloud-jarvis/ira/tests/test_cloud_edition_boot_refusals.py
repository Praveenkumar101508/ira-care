"""The owner-grade capabilities refuse to BOOT in the cloud edition.

`config.py`'s cloud validator refuses three flags outright, so the process does
not start rather than starting with them on. None of the three had a test at the
configuration level: `test_dev_mode_edition_gate.py` covers what `DEV_MODE` does
to the *auth* path once running, and nothing covered the refusal that stops it
running at all. A refusal with no test is a refusal that can be deleted by
accident, and this one is load-bearing for ADR-0001.

── Why boot and not per request ─────────────────────────────────────────────

All three are already closed on the request path. `is_owner()` returns False in
cloud whatever the username is, and every Armory and Forge route depends on
`require_owner` → `is_owner`, so the endpoints refuse regardless of the flag.
This file is not asserting that a hole is closed; it is asserting *where* it
closes.

That distinction is the point. A router that mounts, publishes itself in
`/docs`, and then answers 403 tells a cloud operator the capability is present
and merely unauthorised — which invites someone to go looking for the
authorisation. A process that refuses to start says the capability does not
belong in this edition, once, at the only moment anybody is reading the logs.

Forge was the one refusing later than the other two, which is why it is here.
"""
from __future__ import annotations

import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

#: The flags the cloud validator refuses, and the word its message must carry so
#: a reader knows WHICH capability was refused. Parametrised rather than written
#: three times, because the next owner-grade capability should be one row.
REFUSED_ON_CLOUD = [
    ("ARMORY_ENABLED", "true", "ARMORY_ENABLED"),
    ("DEV_MODE", "true", "DEV_MODE"),
    ("IRA_FORGE_ENABLED", "true", "IRA_FORGE_ENABLED"),
    # D-109: non-commercial (CC BY-NC) voice weights never run in the commercial service.
    ("IRA_VOICE_ENGINE", "omnivoice", "IRA_VOICE_ENGINE"),
]


def _clear_settings():
    from config import get_settings

    get_settings.cache_clear()


@pytest.fixture
def cloud_env(monkeypatch):
    """Cloud edition, every refused flag off, secrets present."""
    for key, _value, _word in REFUSED_ON_CLOUD:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRA_EDITION", "cloud")
    for key in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
                "REDIS_PASSWORD", "VLLM_API_KEY"):
        monkeypatch.setenv(key, "test-placeholder")
    _clear_settings()
    yield monkeypatch
    _clear_settings()


def test_cloud_starts_with_every_owner_flag_off(cloud_env):
    """The control. Without it the refusals below could pass because cloud
    never starts at all, and the tests would be measuring nothing."""
    from config import get_settings

    settings = get_settings()
    assert settings.ira_edition == "cloud"


@pytest.mark.parametrize("flag,value,word", REFUSED_ON_CLOUD,
                         ids=[f[0] for f in REFUSED_ON_CLOUD])
def test_the_flag_is_refused_at_boot_on_cloud(cloud_env, flag, value, word):
    """Setting it must raise, and the message must name the flag.

    The message is asserted because 'it raised' is satisfied by any
    misconfiguration — a missing secret, a bad DSN — and an operator reading
    'validation error' learns nothing about which capability they may not have.
    """
    cloud_env.setenv(flag, value)
    _clear_settings()

    from config import get_settings

    with pytest.raises(RuntimeError) as excinfo:
        get_settings()

    message = str(excinfo.value)
    assert word in message, (
        f"the refusal for {flag} does not name it: {message!r}"
    )
    assert "cloud" in message.lower(), (
        f"the refusal for {flag} does not say which edition refused it: {message!r}"
    )


@pytest.mark.parametrize("flag,value,word", REFUSED_ON_CLOUD,
                         ids=[f[0] for f in REFUSED_ON_CLOUD])
def test_the_same_flag_is_allowed_on_private(monkeypatch, flag, value, word):
    """Each is a Private-edition capability, not a banned one.

    Without this the parametrisation above would be satisfied by a validator
    that refused the flag everywhere, which would break the product it is meant
    to protect.
    """
    monkeypatch.setenv("IRA_EDITION", "private")
    for key in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
                "REDIS_PASSWORD", "VLLM_API_KEY"):
        monkeypatch.setenv(key, "test-placeholder")
    # DEV_MODE carries its own non-edition guards — it is refused on a public
    # domain and on a non-loopback bind. Give it the local values those want, so
    # this test measures the EDITION rule and not one of those.
    monkeypatch.setenv("IRA_DOMAIN", "localhost")
    monkeypatch.setenv("API_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv(flag, value)
    _clear_settings()

    from config import get_settings

    settings = get_settings()  # must not raise
    assert settings.ira_edition == "private"
    _clear_settings()
