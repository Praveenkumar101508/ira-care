"""The Trust Console tells only the owner about a weak credential.

`GET /api/v1/trust/status` is open to the household roles that hold
`settings_read` (`family_admin`, `owner_equivalent`). Its warnings said when
`IRA_ADMIN_PASSWORD` was one of three published values ("admin", "password",
the `.env.example` placeholder), which is enough to sign in as the owner, and
when `IRA_SECRET_KEY` was the published placeholder, which is enough to sign a
token as the owner. Both are facts about the owner's own credentials, so only
the owner sees them now. The privacy-posture warnings stay visible to everyone
who may read the page.

The status is computed from what the caller is shown, so an "attention" with
no visible reason cannot point at a hidden credential warning.

Each attack test fails on `main` at 310e8c6; that run is its negative control.
"""
import asyncio
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import pytest

pytest.importorskip("fastapi")

import api.routes.trust as trust

HOUSEHOLD = "pda-granted"


class _Cfg:
    dev_mode = False
    ira_secret_key = "CHANGE_ME_ira_secret_key_here"
    ira_admin_password = "admin"
    ira_allow_external_api = False
    ira_require_api_consent = True
    ira_privacy_mode = "local_first"
    web_search_enabled = False
    llm_backend = "ollama"
    ollama_base_url = "http://localhost:11434/v1"
    ira_model_profile = "balanced_local"
    postgres_host = "localhost"
    embedding_model = "BAAI/bge-large-en-v1.5"
    embedding_device = "cpu"


@pytest.fixture
def owner(monkeypatch):
    """The trust module's settings replaced; `is_owner` stays the real one."""
    from config import get_settings

    async def _none():
        return None

    cfg = _Cfg()
    monkeypatch.setattr(trust, "get_settings", lambda: cfg)
    monkeypatch.setattr(trust, "cortex_local_only_warning", lambda: None)
    monkeypatch.setattr(trust, "research_backends_warning", lambda: None)
    monkeypatch.setattr(trust, "_voice_enrolled", _none)
    monkeypatch.setattr(trust, "_last_login", _none)
    monkeypatch.setattr(trust, "_pending_actions", lambda: 0)
    return get_settings().ira_admin_username, cfg


def _status(user):
    return asyncio.run(trust.trust_status(_user=user))


def test_the_owner_is_told_about_weak_credentials(owner):
    """The control."""
    name, _ = owner
    out = _status(name)
    assert out["status"] == "attention"
    assert any("IRA_ADMIN_PASSWORD" in w for w in out["security_warnings"])
    assert any("IRA_SECRET_KEY" in w for w in out["security_warnings"])


def test_a_household_account_is_not_told_about_the_owners_credentials(owner):
    out = _status(HOUSEHOLD)
    leaked = [w for w in out["security_warnings"]
              if "IRA_ADMIN_PASSWORD" in w or "IRA_SECRET_KEY" in w]
    assert not leaked, f"a household account was told: {leaked}"


def test_a_household_accounts_status_does_not_hint_at_them(owner):
    out = _status(HOUSEHOLD)
    assert out["status"] == "local_only", out


def test_a_household_account_still_sees_the_privacy_posture(owner):
    """The control for over-filtering: the switches stay visible."""
    _, cfg = owner
    cfg.ira_allow_external_api = True
    out = _status(HOUSEHOLD)
    assert out["status"] == "attention"
    assert any("IRA_ALLOW_EXTERNAL_API" in w for w in out["security_warnings"])
