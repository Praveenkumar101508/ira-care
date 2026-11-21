"""P0-03: destructive actions need a confirmation bound to that action.

Two gaps found by the 31 October 2026 audit of destructive and irreversible
actions, each closed with the approval machinery the rest of IRA already uses
(`utils/approval.py`), not a parallel one:

* **Backup restore** (`POST /api/v1/backup/restore`) applies the uploaded SQL
  to the live database with the database's own credentials. Its only
  confirmation was a fixed header, `X-Restore-Confirm: yes`: the same for
  every archive, so it confirmed nothing in particular, and one request was
  enough. It now takes two requests, like every other destructive route. The
  first returns a one-time token for THIS archive (its SHA-256 and size are in
  the preview) and changes nothing. The second, with that token and the owner
  password (constitution B2: high risk needs the password), restores. A token
  issued for one archive does not restore another.

* **Architect apply by voice.** With `IRA_ARCHITECT_ENABLED` on, saying
  "architect apply" to the voice agent ran `git apply`, committed to IRA's own
  source and restarted services. The voice service asserts the owner from the
  request body, so a spoken phrase was the whole of the approval. Applying is
  now refused on a voice turn; the owner applies by typing it, as before.

Every refusal is paired with a test that the owner can still do the action
with the approval it needs.
"""
from __future__ import annotations

import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import asyncio
import gzip
import hashlib
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    setattr(_st, "SentenceTransformer", object)

# This unit job has no Redis, so the lockdown state reads UNKNOWN, which
# refuses high and critical actions (restore and architect apply among them).
# Every test here runs with the lockdown off, so the refusals it checks are the
# token, the password and the voice guard, not the lockdown. The lockdown's own
# refusal of restore is checked below, by setting the state to ACTIVE.
pytestmark = pytest.mark.usefixtures("lockdown_inactive")


# ── Backup restore ────────────────────────────────────────────────────────────


class _BackupSettings:
    def __init__(self, backup_dir: pathlib.Path):
        self.backup_dir = str(backup_dir)


@pytest.fixture
def fresh_password_hash():
    """`_admin_password_hash` is cached per process. Another test that swaps
    `get_settings` could leave a hash of a different password behind, and the
    owner's real password would then be refused for a reason unrelated to
    this file. Clear it on the way in and out."""
    from api.middleware.auth import _admin_password_hash

    _admin_password_hash.cache_clear()
    yield
    _admin_password_hash.cache_clear()


@pytest.fixture
def restore_app(tmp_path, monkeypatch, fresh_password_hash):
    """The backup router alone, with `require_auth` answered by the caller's
    name. `require_owner` and the approval guardrail are the real ones."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    import api.routes.backup as route
    from api.middleware.auth import require_auth

    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(route, "get_settings", lambda: _BackupSettings(backup_dir))

    restored: list[bytes] = []
    snapshots: list[str] = []

    async def fake_restore(path):
        restored.append(pathlib.Path(path).read_bytes())
        return True, f"Restore complete from {pathlib.Path(path).name}"

    async def fake_snapshot():
        snapshots.append("taken")
        return None

    monkeypatch.setattr(route, "restore_from_file", fake_restore)
    monkeypatch.setattr(route, "run_database_backup", fake_snapshot)

    def client(user: str) -> AsyncClient:
        app = FastAPI()
        app.include_router(route.router, prefix="/api/v1")
        app.dependency_overrides[require_auth] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://p0-03.invalid")

    return SimpleNamespace(client=client, restored=restored, snapshots=snapshots,
                           backup_dir=backup_dir)


def _owner() -> str:
    from config import get_settings

    return get_settings().ira_admin_username


def _owner_password() -> str:
    from config import get_settings

    return get_settings().ira_admin_password


def _archive(sql: bytes) -> bytes:
    return gzip.compress(sql)


async def _post_restore(client, archive: bytes, *, headers=None, **form):
    return await client.post(
        "/api/v1/backup/restore",
        files={"file": ("backup.sql.gz", archive, "application/gzip")},
        data={k: v for k, v in form.items() if v is not None},
        headers=headers or {},
    )


async def test_the_fixed_header_alone_no_longer_restores(restore_app):
    """The gap. `X-Restore-Confirm: yes` is the same for every archive, so one
    request carrying it restored whatever was uploaded. Now that request only
    stages the restore and returns a token for this archive."""
    archive = _archive(b"SELECT 'p0-03 header only';\n")
    async with restore_app.client(_owner()) as client:
        response = await _post_restore(client, archive, headers={"X-Restore-Confirm": "yes"})

    assert restore_app.restored == [], (
        "a single request with the fixed confirmation header restored the "
        "database: the confirmation was not bound to this archive")
    assert restore_app.snapshots == [], "staging must not touch the database"
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmation_required"
    assert body["token"]
    assert hashlib.sha256(archive).hexdigest() in body["preview"], (
        "the preview must name the archive by its hash, so the token approves "
        "these bytes and no others")
    assert not list(restore_app.backup_dir.glob("restore_*")), (
        "nothing is written to disk until the restore is confirmed")


async def test_a_token_for_one_archive_does_not_restore_another(restore_app):
    first = _archive(b"SELECT 'the archive the owner looked at';\n")
    other = _archive(b"DROP TABLE everything_important;\n")
    async with restore_app.client(_owner()) as client:
        staged = (await _post_restore(client, first)).json()
        swapped = await _post_restore(client, other, confirm_token=staged["token"],
                                      owner_password=_owner_password())
        # The mismatch consumed the token, so it cannot then be tried again.
        retried = await _post_restore(client, first, confirm_token=staged["token"],
                                      owner_password=_owner_password())

    assert swapped.status_code == 409, swapped.text
    assert retried.status_code == 409, retried.text
    assert restore_app.restored == [], "a token approved a different archive"


async def test_the_token_needs_the_owner_password(restore_app):
    """B2: high risk needs the owner password. A wrong or missing password
    refuses without spending the token, so the owner can retype it."""
    archive = _archive(b"SELECT 'p0-03 password';\n")
    async with restore_app.client(_owner()) as client:
        token = (await _post_restore(client, archive)).json()["token"]
        missing = await _post_restore(client, archive, confirm_token=token)
        wrong = await _post_restore(client, archive, confirm_token=token,
                                    owner_password="not-the-owner-password")
        assert restore_app.restored == [], "restored without the owner password"
        right = await _post_restore(client, archive, confirm_token=token,
                                    owner_password=_owner_password())

    assert missing.status_code == 403, missing.text
    assert wrong.status_code == 403, wrong.text
    assert right.status_code == 200, right.text
    assert restore_app.restored == [archive]


async def test_the_owner_can_still_restore_with_the_token_and_password(restore_app):
    """The legitimate flow, end to end: stage, read the preview, confirm."""
    archive = _archive(b"SELECT 'p0-03 owner restore';\n")
    async with restore_app.client(_owner()) as client:
        staged = await _post_restore(client, archive)
        assert staged.status_code == 200, staged.text
        token = staged.json()["token"]
        done = await _post_restore(client, archive, confirm_token=token,
                                   owner_password=_owner_password())
        again = await _post_restore(client, archive, confirm_token=token,
                                    owner_password=_owner_password())

    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "restored"
    assert body["sha256"] == hashlib.sha256(archive).hexdigest()
    assert restore_app.restored == [archive], "the restore ran the previewed bytes, once"
    assert restore_app.snapshots == ["taken"], "the pre-restore snapshot still runs"
    assert again.status_code == 409, "a token restores once"
    assert not list(restore_app.backup_dir.glob("restore_*")), "the upload is removed afterwards"


async def test_someone_other_than_the_owner_can_neither_stage_nor_restore(restore_app):
    archive = _archive(b"SELECT 'p0-03 not the owner';\n")
    async with restore_app.client(_owner()) as owner:
        token = (await _post_restore(owner, archive)).json()["token"]
    async with restore_app.client("someone-else") as other:
        staged = await _post_restore(other, archive)
        confirmed = await _post_restore(other, archive, confirm_token=token,
                                        owner_password=_owner_password())

    assert staged.status_code == 403
    assert confirmed.status_code == 403
    assert restore_app.restored == []


async def test_a_corrupt_archive_is_refused_before_any_token(restore_app):
    async with restore_app.client(_owner()) as client:
        response = await _post_restore(client, b"this is not gzip")
    assert response.status_code == 400
    assert "corrupt" in response.text
    assert restore_app.restored == []


def _lockdown(monkeypatch, state_name: str) -> None:
    from utils.security_tools import LockdownState

    state = LockdownState[state_name]

    async def _get():
        return state

    monkeypatch.setattr("utils.security_tools.get_lockdown_state", _get)


async def test_a_lockdown_refuses_the_restore_at_stage_and_at_confirm(restore_app, monkeypatch):
    """A token staged before the lockdown does not restore during it, and no new
    token is issued. Lifting the lockdown lets the same token restore."""
    archive = _archive(b"SELECT 'p0-03 restore during lockdown';\n")
    async with restore_app.client(_owner()) as client:
        token = (await _post_restore(client, archive)).json()["token"]

        _lockdown(monkeypatch, "ACTIVE")
        staged = await _post_restore(client, archive)
        confirmed = await _post_restore(client, archive, confirm_token=token,
                                        owner_password=_owner_password())
        assert staged.status_code == 423, staged.text
        assert confirmed.status_code == 423, confirmed.text
        assert confirmed.headers.get("X-IRA-Lockdown") == "active"
        assert "token" not in staged.json(), "a lockdown must not issue a token"
        assert restore_app.restored == [], "restored during a lockdown"
        assert restore_app.snapshots == [], "touched the database during a lockdown"

        _lockdown(monkeypatch, "INACTIVE")
        after = await _post_restore(client, archive, confirm_token=token,
                                    owner_password=_owner_password())
    assert after.status_code == 200, after.text
    assert restore_app.restored == [archive], "the refusal must not spend the token"


async def test_an_unreadable_lockdown_state_refuses_the_restore(restore_app, monkeypatch):
    """Restore is critical: when the lockdown state cannot be read, it is refused."""
    _lockdown(monkeypatch, "UNKNOWN")
    async with restore_app.client(_owner()) as client:
        response = await _post_restore(client, _archive(b"SELECT 1;\n"))
    assert response.status_code == 423, response.text
    assert response.headers.get("X-IRA-Lockdown") == "unknown"
    assert restore_app.restored == []


# ── Architect apply by voice ──────────────────────────────────────────────────


class _ArchitectCfg:
    ira_admin_username = "owner"
    ira_voice_service_username = "ira-voice"
    dev_mode = True            # skips the biometric/restricted-domain gate
    owner_name = "Praveen"


async def _say_architect_apply(monkeypatch, *, user: str, is_voice: bool,
                               is_voice_owner: bool = False):
    """Drive `chat_stream` to its apply branch, as tests/test_architect_apply.py
    does, and report whether `apply_implementation` ran and what was said."""
    import json

    import api.routes.chat as chatmod
    from api.routes.chat import ChatRequest, chat_stream

    monkeypatch.setenv("IRA_ARCHITECT_ENABLED", "true")
    monkeypatch.setattr(chatmod, "get_settings", lambda: _ArchitectCfg())
    monkeypatch.setattr("api.middleware.auth.get_settings", lambda: _ArchitectCfg())
    monkeypatch.setattr(chatmod, "ensure_conversation", AsyncMock(return_value="conv1"))
    monkeypatch.setattr(chatmod, "retrieve", AsyncMock(return_value=[]))
    monkeypatch.setattr(chatmod, "_USE_CORTEX", False)
    monkeypatch.setattr("agents.supervisor.classify",
                        AsyncMock(return_value={"active_agent": "conversational",
                                                "use_deep_model": False}))
    monkeypatch.setattr("agents.architect_agent.is_architect_trigger", lambda q: False)
    monkeypatch.setattr("agents.architect_agent.is_implement_trigger", lambda q: False)
    monkeypatch.setattr("agents.architect_agent.is_apply_trigger", lambda q: True)
    monkeypatch.setattr("api.routes.architect._get_state",
                        AsyncMock(return_value={"implementation": "PATCH-DIFF",
                                                "pending_apply": True}))
    monkeypatch.setattr("api.routes.architect._set_state", AsyncMock())
    apply_impl = AsyncMock(return_value=SimpleNamespace(
        success=True, message="applied", error=None))
    monkeypatch.setattr("utils.auto_implement.apply_implementation", apply_impl)

    response = await chat_stream(
        ChatRequest(message="architect apply", session_id="s1",
                    image_b64=None, mime_type="image/jpeg",
                    is_voice=is_voice, is_voice_owner=is_voice_owner),
        _user=user,
    )
    said = []
    async for event in response.body_iterator:
        data = event.get("data") if isinstance(event, dict) else None
        if data:
            said.append(json.loads(data).get("token") or "")
    return apply_impl, "".join(said)


async def test_the_voice_agent_cannot_apply_architect_changes(monkeypatch):
    """The gap. The voice service is believed when it says the speaker is the
    owner, so a spoken "architect apply" committed to IRA's source and
    restarted services with nothing else asked."""
    apply_impl, said = await _say_architect_apply(
        monkeypatch, user="ira-voice", is_voice=True, is_voice_owner=True)

    apply_impl.assert_not_called()
    assert "voice" in said.lower(), said


async def test_a_voice_turn_from_the_owners_own_session_cannot_apply_either(monkeypatch):
    """The browser's voice loop runs under the owner's own token. It is still a
    spoken command, so it is refused the same way."""
    apply_impl, _said = await _say_architect_apply(monkeypatch, user="owner", is_voice=True)
    apply_impl.assert_not_called()


async def test_the_owner_can_still_apply_by_typing_it(monkeypatch):
    apply_impl, _said = await _say_architect_apply(monkeypatch, user="owner", is_voice=False)
    assert apply_impl.called, "the typed owner command must still apply"
    assert apply_impl.call_args.kwargs.get("dry_run") is False
