"""A credential whose value is published in history must not be in use.

Six credentials were committed with real values in July 2026
(docs/cloud/SECRET_ROTATION_REGISTER.md); every clone has them. IRA now refuses
to start with a published signing key, admin password or webhook secret, warns
about a published canary or cortex key, and shows the owner (only the owner)
which ones on the trust page. `scripts/security/check_credential_rotation.py`
answers the same question for an .env file, printing names only.

The tests plant a digest of a value of their own: the real published values are
not, and must not be, in this repository's tests.
"""
from __future__ import annotations

import importlib.util
import logging
import pathlib
import re
import subprocess

import pytest

from core.runtime import published_credentials as pc

PLANTED = "a-value-this-test-publishes-for-itself-1"
ROOT = pathlib.Path(__file__).resolve().parents[3]


@pytest.fixture
def planted(monkeypatch):
    """Every name's published set gains the digest of PLANTED."""
    table = {name: frozenset({*digests, pc.digest(name, PLANTED)})
             for name, digests in pc.PUBLISHED_DIGESTS.items()}
    monkeypatch.setattr(pc, "PUBLISHED_DIGESTS", table)
    for name in pc.WARNED:
        monkeypatch.delenv(name, raising=False)
    return PLANTED


class _Cfg:
    ira_secret_key = "test-only-unpublished-signing-key"
    ira_admin_password = "an-unpublished-admin-password"
    webhook_secret = ""


def test_the_table_names_all_six_and_holds_only_digests():
    assert set(pc.PUBLISHED_DIGESTS) == {*pc.REFUSED, *pc.WARNED}
    assert len(pc.REFUSED) + len(pc.WARNED) == 6
    for name, digests in pc.PUBLISHED_DIGESTS.items():
        assert digests, f"{name} has no published digest"
        for d in digests:
            assert re.fullmatch(r"[0-9a-f]{64}", d), name


def test_the_digest_is_salted_scrypt_not_a_fast_hash():
    """A fast hash of a short value can be reversed by guessing; scrypt cannot, cheaply.

    The same value under two names digests differently, so a value reused across
    variables is not visible as a repeat, and neither SHA-256 nor an unsalted
    scrypt of it appears in the table.
    """
    import hashlib

    v = "short-guessable-value"
    assert pc.SCRYPT["n"] * pc.SCRYPT["r"] * pc.SCRYPT["p"] >= 2**15 * 8 * 3
    assert pc.digest("IRA_SECRET_KEY", v) != pc.digest("IRA_ADMIN_PASSWORD", v)
    assert pc.digest("IRA_SECRET_KEY", v) != hashlib.sha256(v.encode()).hexdigest()
    assert pc.digest("IRA_SECRET_KEY", v) == hashlib.scrypt(
        v.encode(), salt=b"ira-published-credential:IRA_SECRET_KEY", **pc.SCRYPT).hex()


def test_a_published_value_is_recognised_and_nothing_else_is(planted):
    assert pc.is_published("IRA_SECRET_KEY", PLANTED)
    assert not pc.is_published("IRA_SECRET_KEY", PLANTED + "x")
    assert not pc.is_published("IRA_SECRET_KEY", "")
    assert not pc.is_published("IRA_SECRET_KEY", None)
    assert not pc.is_published("NOT_ONE_OF_THE_SIX", PLANTED)


def test_settings_and_the_environment_are_both_read(planted, monkeypatch):
    cfg = _Cfg()
    assert pc.published_in_use(cfg) == []
    cfg.webhook_secret = PLANTED
    monkeypatch.setenv("IRA_CANARY_TOKEN", PLANTED)
    assert pc.published_in_use(cfg) == ["WEBHOOK_SECRET", "IRA_CANARY_TOKEN"]


@pytest.mark.parametrize("field,name", [("ira_secret_key", "IRA_SECRET_KEY"),
                                        ("ira_admin_password", "IRA_ADMIN_PASSWORD"),
                                        ("webhook_secret", "WEBHOOK_SECRET")])
def test_start_up_refuses_a_published_access_credential(planted, field, name, caplog):
    from app.lifespan import _refuse_published_credentials

    cfg = _Cfg()
    setattr(cfg, field, PLANTED)
    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(RuntimeError, match=name):
            _refuse_published_credentials(cfg)
    assert PLANTED not in caplog.text, "a credential value reached the log"


@pytest.mark.parametrize("name", ["IRA_CANARY_TOKEN", "IRA_CANARY_USERNAME", "IRA_CORTEX_KEY"])
def test_start_up_warns_about_a_published_canary_or_cortex_key(planted, name, monkeypatch,
                                                               caplog):
    from app.lifespan import _refuse_published_credentials

    monkeypatch.setenv(name, PLANTED)
    with caplog.at_level(logging.CRITICAL):
        _refuse_published_credentials(_Cfg())          # does not raise
    assert name in caplog.text
    assert PLANTED not in caplog.text


def test_start_up_is_quiet_with_unpublished_values(planted, caplog):
    from app.lifespan import _refuse_published_credentials

    with caplog.at_level(logging.CRITICAL):
        _refuse_published_credentials(_Cfg())
    assert "published" not in caplog.text


def test_the_validator_runs_the_refusal(planted):
    """`_validate_config` is what start-up calls; the refusal must be in it."""
    import asyncio

    from app.lifespan import _validate_config

    cfg = _Cfg()
    cfg.ira_secret_key = PLANTED
    with pytest.raises(RuntimeError, match="IRA_SECRET_KEY"):
        asyncio.run(_validate_config(cfg))


def test_only_the_owner_sees_which_credential_is_published():
    from api.routes import trust

    for name in (*pc.REFUSED, *pc.WARNED):
        assert name in trust._CREDENTIAL_WARNINGS, (
            f"a non-owner would be told that {name} is the published value")


# ── the owner's check script ───────────────────────────────────────────────

def _script():
    path = ROOT / "scripts" / "security" / "check_credential_rotation.py"
    spec = importlib.util.spec_from_file_location("check_credential_rotation", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod



def test_the_table_matches_a_fresh_scan_of_history(tmp_path):
    """The digests are what the rebuild script finds, on a full clone."""
    shallow = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--is-shallow-repository"],
                             capture_output=True, text=True).stdout.strip()
    if shallow != "false":
        pytest.skip("needs a full clone: the published values are in history")
    out = tmp_path / "digests.json"
    r = subprocess.run(["python3", str(ROOT / "scripts" / "security" /
                                       "published_credential_digests.py"), str(out)],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    import json

    fresh = {name: frozenset(d) for name, d in json.loads(out.read_text()).items()}
    assert fresh == pc.PUBLISHED_DIGESTS
