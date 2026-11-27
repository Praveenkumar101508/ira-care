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


# ── the history scanner (D-004) ───────────────────────────────────────────────

SCANNER = ROOT / "scripts" / "security" / "published_credential_digests.py"
VALUE_SHAPE = re.compile(r"[0-9a-f]{64}")


def _run_scanner(repo: pathlib.Path, out: pathlib.Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["python3", "-I", str(SCANNER), str(out), "--repo", str(repo), *extra],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)


def _read(out: pathlib.Path) -> dict[str, frozenset[str]]:
    import json

    return {name: frozenset(d) for name, d in json.loads(out.read_text()).items()}


def _git(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                        "HOME": str(repo), "PATH": "/usr/bin:/bin"})


def _commit(repo: pathlib.Path, path: str, text: str | None, message: str) -> None:
    target = repo / path
    if text is None:
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


# Planted values, assembled at run time so no credential-shaped literal sits in the file.
PLANTED_KEY = "planted-" + "signing-key-" + "for-the-scanner-test-" + "0001"
PLANTED_HOOK = "planted-" + "webhook-secret-" + "on-a-side-branch-" + "0002"
PLANTED_GONE = "planted-" + "admin-password-" + "deleted-later-" + "0003"


@pytest.fixture
def synthetic_repo(tmp_path):
    """A small history with values in a deleted version, on a side branch, and placeholders."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "supracloud-jarvis/.env.example",
            f"IRA_SECRET_KEY={PLANTED_KEY}\nIRA_ADMIN_PASSWORD={PLANTED_GONE}\n"
            "WEBHOOK_SECRET=\nIRA_CORTEX_KEY=CHANGE_ME_cortex\nIRA_CANARY_TOKEN=<your-token>\n"
            "IRA_CANARY_USERNAME=ci-test\nOTHER=not-one-of-the-six\n", "first")
    _commit(repo, "supracloud-jarvis/.env.example",
            "IRA_SECRET_KEY=change-me\nIRA_ADMIN_PASSWORD=\nWEBHOOK_SECRET=\n", "values removed")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "deploy/.env.cloud.example", f'WEBHOOK_SECRET="{PLANTED_HOOK}"  # trailing\n',
            "side branch value")
    _commit(repo, "deploy/.env.cloud.example", None, "side branch file deleted")
    _git(repo, "checkout", "-q", "main")
    return repo


def test_the_scanner_finds_values_in_deleted_versions_and_on_other_branches(synthetic_repo, tmp_path):
    out = tmp_path / "d.json"
    r = _run_scanner(synthetic_repo, out)
    assert r.returncode == 0, r.stderr
    fresh = _read(out)
    assert fresh["IRA_SECRET_KEY"] == {pc.digest("IRA_SECRET_KEY", PLANTED_KEY)}
    assert fresh["IRA_ADMIN_PASSWORD"] == {pc.digest("IRA_ADMIN_PASSWORD", PLANTED_GONE)}
    assert fresh["WEBHOOK_SECRET"] == {pc.digest("WEBHOOK_SECRET", PLANTED_HOOK)}, "quotes and comment stripped"


def test_the_scanner_ignores_placeholders_and_names_outside_the_six(synthetic_repo, tmp_path):
    out = tmp_path / "d.json"
    assert _run_scanner(synthetic_repo, out).returncode == 0
    fresh = _read(out)
    assert fresh["IRA_CORTEX_KEY"] == frozenset()          # CHANGE_ME_...
    assert fresh["IRA_CANARY_TOKEN"] == frozenset()        # <your-token>
    assert fresh["IRA_CANARY_USERNAME"] == frozenset()     # ci-test, a known test placeholder
    assert "OTHER" not in fresh
    assert set(fresh) == {*pc.REFUSED, *pc.WARNED}


def test_the_scanner_prints_provenance_and_never_a_value_or_digest(synthetic_repo, tmp_path):
    out = tmp_path / "d.json"
    r = _run_scanner(synthetic_repo, out)
    text = r.stdout + r.stderr
    for planted in (PLANTED_KEY, PLANTED_HOOK, PLANTED_GONE):
        assert planted not in text
        assert planted[8:30] not in text, "not even part of a value"
    assert not VALUE_SHAPE.search(text), "no digest on the terminal"
    assert "supracloud-jarvis/.env.example" in r.stdout and "deploy/.env.cloud.example" in r.stdout
    assert "PUBLISHED IRA_SECRET_KEY,IRA_ADMIN_PASSWORD" in r.stdout
    assert "placeholders only" in r.stdout


def test_a_history_of_placeholders_alone_publishes_nothing(tmp_path):
    repo = tmp_path / "clean"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, ".env.example", "IRA_SECRET_KEY=\nIRA_ADMIN_PASSWORD=change-me\n"
            "WEBHOOK_SECRET=ci-test-secret-key\n", "placeholders")
    out = tmp_path / "d.json"
    assert _run_scanner(repo, out).returncode == 0
    assert all(d == frozenset() for d in _read(out).values())


def test_include_inherited_merges_the_written_table(synthetic_repo, tmp_path):
    out = tmp_path / "d.json"
    assert _run_scanner(synthetic_repo, out, "--include-inherited").returncode == 0
    fresh = _read(out)
    for name, known in pc.PUBLISHED_DIGESTS.items():
        assert known <= fresh[name], name
    assert pc.digest("IRA_SECRET_KEY", PLANTED_KEY) in fresh["IRA_SECRET_KEY"]


def missing_from_table(fresh: dict[str, frozenset[str]]) -> dict[str, frozenset[str]]:
    """Digests the history publishes that the refusal table does not know: must be empty."""
    return {name: d - pc.PUBLISHED_DIGESTS.get(name, frozenset())
            for name, d in fresh.items() if d - pc.PUBLISHED_DIGESTS.get(name, frozenset())}


def test_the_comparison_fails_when_a_published_value_is_not_in_the_table(synthetic_repo, tmp_path):
    """Negative control for the check below: the planted values are, by design, not in the table."""
    out = tmp_path / "d.json"
    assert _run_scanner(synthetic_repo, out).returncode == 0
    gaps = missing_from_table(_read(out))
    assert set(gaps) == {"IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "WEBHOOK_SECRET"}


def test_the_table_matches_a_fresh_scan_of_history(tmp_path):
    """On a full clone: nothing this repository's history publishes is missing from the table,
    and the table equals the scan merged with the inherited provenance (D-004)."""
    shallow = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--is-shallow-repository"],
                             capture_output=True, text=True).stdout.strip()
    if shallow != "false":
        pytest.skip("needs a full clone: published values are found in history")
    own = tmp_path / "own.json"
    r = subprocess.run(["python3", "-I", str(SCANNER), str(own)], cwd=ROOT, capture_output=True,
                       text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    assert not VALUE_SHAPE.search(r.stdout + r.stderr), "a digest reached the terminal"
    assert "scanned " in r.stdout
    assert missing_from_table(_read(own)) == {}, "a value published in this history is not refused"
    full = tmp_path / "full.json"
    r = subprocess.run(["python3", "-I", str(SCANNER), str(full), "--include-inherited"], cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    assert _read(full) == pc.PUBLISHED_DIGESTS
