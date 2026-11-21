import os
import sys
import types
import pytest

# The unit suite never downloads model weights. faster-whisper is installed in
# the test environment (requirements-test.txt), so a code path that reaches the
# Hub would otherwise pull hundreds of megabytes mid-run — and pass or fail on
# what the network did. Offline, such a load fails and the code under test takes
# its documented fallback. Set before huggingface_hub is imported, which reads it
# once; setdefault so a deliberate local run can still override it.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

# ---------------------------------------------------------------------------
# Git's repository-locating variables are removed for the whole run.
#
# Several suites build throwaway repositories with `git -C <tmp> ...`
# (tests/coding/*), and the code they exercise does the same. Inside a git hook,
# git exports GIT_DIR. When the push starts from a linked worktree
# (`git worktree add`, which CONTRIBUTING recommends), it is an ABSOLUTE path
# to the real repository, and GIT_DIR overrides `-C`. Under the pre-push hook,
# those throwaway commands therefore ran against the developer's repository:
# they set core.bare=true, replaced user.name/user.email with the fixture's
# identity, and committed fixture commits onto the checked-out branch.
# Observed on 2026-10-18. tests/test_git_env_isolation.py reproduces it.
# ---------------------------------------------------------------------------
for _git_var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                 "GIT_PREFIX", "GIT_NAMESPACE"):
    os.environ.pop(_git_var, None)

# ---------------------------------------------------------------------------
# Lightweight stubs for modules that require native extensions or network
# connections that are not present in the CI / unit-test environment.
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs):
    """Insert a minimal stub module into sys.modules."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules.setdefault(name, mod)
    return mod


# asyncpg — PostgreSQL client.
#
# Stubbed ONLY when the real driver is absent, on the same reasoning as the
# livekit block below: when asyncpg IS installed, shadowing it would make
# tests/tenancy/test_key_management_rls.py silently `importorskip` itself and
# never exercise `core/connectors/keyring.py` against a real database — a
# skipped crypto-shredding proof that still shows green.
#
# `utils.db` goes with it. That module is nothing but an asyncpg pool, so a real
# asyncpg and a stubbed pool is the one combination that cannot work: the
# real-database tests set `utils.db._pool` so the production `tenant_connection`
# runs unmodified, and a stub has no `_pool` to set. Tests that patch
# `utils.db.acquire` are unaffected — the real module exports the same names.
import importlib.util as _ilu


def _real_asyncpg_present() -> bool:
    try:
        return _ilu.find_spec("asyncpg") is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


if _real_asyncpg_present():
    import utils.db as _db  # noqa: F401  - imported so nothing later stubs it
else:
    _asyncpg = _stub("asyncpg")
    _asyncpg.Pool = object
    _asyncpg.Connection = object
    _db = _stub("utils.db", acquire=None, get_pool=None)


# worker.notifier — Telegram/email/WebSocket; replaced by test patches
#
# `notify` is stubbed because the delivery side really does send Telegram
# messages and email. `notify_channel` is NOT delivery — it reads settings to
# decide which channel a principal's notifications belong on, and
# `api/routes/notifications.py` imports it at module scope. Stubbing the module
# without it meant that route could not be IMPORTED under the test conftest, so
# `create_app()` failed for every suite that builds the real app. P12-01 found
# it by trying to exercise the route; P12-03 hit it again from a different
# direction, which is why the fix is here rather than in one suite's conftest.
#
# Taken from the real module rather than faked, so it cannot drift from the
# behaviour the route depends on.
def _real_notify_channel():
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "worker._notifier_real",
        pathlib.Path(__file__).resolve().parents[1] / "worker" / "notifier.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_stub("worker.notifier", notify=None, notify_report=None)
try:
    _notifier_real = _real_notify_channel()
    for _name in ("notify_channel", "Priority", "_persist"):
        if hasattr(_notifier_real, _name):
            setattr(sys.modules["worker.notifier"], _name,
                    getattr(_notifier_real, _name))
except Exception:  # noqa: BLE001 — a stub that is merely incomplete is the
    # status quo, and failing conftest import would take every suite with it.
    pass

# worker.backup — not exercised by webhook tests
_stub("worker.backup",
      list_backups=None,
      restore_from_file=None,
      run_database_backup=None)

# sentence_transformers — ML library not in test env.
#
# `SentenceTransformer` is named because `memory/embeddings.py` does
# `from sentence_transformers import SentenceTransformer` at module scope, and a
# stub without the attribute makes that module unimportable. The effect was not
# "the model is unavailable" but "any code path that reaches embeddings dies on
# an ImportError before it reaches the database" — which is how P12-01 found
# `api/routes/notifications.py` and the memory callers unobservable by any
# suite. The name only has to exist; every test that needs a vector replaces
# `embed_one`/`embed`, so nothing constructs it.
_stub("sentence_transformers", SentenceTransformer=object)

# psutil — system metrics lib used by the health route; not in the lightweight test env
_stub("psutil")

# uvloop — optional performance lib
_stub("uvloop")

# livekit — stub ONLY if the real Agents SDK isn't importable. When livekit-agents
# IS installed (voice image / dev workstation), conftest must NOT shadow it, or the voice
# import-smoke (test_voice_imports.py) would silently importorskip itself and never
# actually validate against the real LiveKit 1.x API. We probe livekit.agents
# specifically (a bare leftover `livekit` namespace dir must still fall back to the stub).


def _real_livekit_agents_present() -> bool:
    try:
        return _ilu.find_spec("livekit.agents") is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


if not _real_livekit_agents_present():
    _livekit = _stub("livekit")
    _livekit_api = _stub("livekit.api")
    _livekit.api = _livekit_api
    _livekit_api.WebhookReceiver = object


# ---------------------------------------------------------------------------
# A workspace-bound Principal for handler and service tests.
#
# Batch 5C made the workspace an ARGUMENT rather than an ambient fact: the
# retrieval, storage and drafting functions all take a Principal carrying a
# verified workspace, because a function that resolves its own scope can
# resolve the wrong one and nothing at the call site would show it.
#
# Unit tests need one to pass in. This builds it directly rather than going
# through `select_workspace`, which is correct here: what these tests are about
# is the behaviour downstream of verification. The verification ITSELF is
# tested against a real PostgreSQL in tests/tenancy/test_phase3b_batch5c.py,
# where a fake would prove nothing.
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace_principal():
    return make_workspace_principal()


def make_workspace_principal(*, role: str = "workspace_admin", is_owner: bool = True):
    """A Principal that `workspace_connection` will accept.

    A plain function as well as a fixture, because several of these suites call
    the services directly from helpers that have no access to fixtures.
    """
    import uuid

    from core.editions import EDITION_PRIVATE
    from core.identity.principal import Principal

    return Principal(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        edition=EDITION_PRIVATE,
        workspace_id=uuid.uuid4(),
        workspace_role=role,
        username="owner",
        is_owner=is_owner,
    )


def patch_db_connections(monkeypatch, conn):
    """Point every connection helper at `conn` for a handler/service unit test.

    These suites already faked `utils.db.acquire`. Batch 5C moved the code onto
    `workspace_connection`, which does more than acquire: it opens a
    transaction, sets three GUCs and re-checks the tenant/workspace pair. A fake
    that only answers `fetch`/`execute` no longer satisfies it.

    Rather than teach every fake connection object to impersonate a
    transaction, this replaces the helper itself. What that gives up is stated
    plainly: these tests no longer prove anything about SCOPING — a fake accepts
    any principal. That proof lives in tests/tenancy/test_phase3b_batch5c.py
    against a real PostgreSQL as `ira_app`, where RLS is real and a wrong
    workspace returns nothing. What these tests still prove is the behaviour
    downstream of the connection, which is what they were always about.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _acquire():
        yield conn

    @asynccontextmanager
    async def _scoped(*args, **kwargs):
        yield conn

    monkeypatch.setattr(sys.modules["utils.db"], "acquire", _acquire, raising=False)

    import core.identity.db_context as _ctx

    for name in ("workspace_connection", "tenant_connection", "owner_connection"):
        monkeypatch.setattr(_ctx, name, _scoped, raising=False)


# ---------------------------------------------------------------------------
# The lockdown state, pinned for suites that are about something else.
#
# Every action path reads the lockdown state before it acts (F65, 31 October
# 2026). This suite runs with no Redis and no PostgreSQL, so that read reports
# UNKNOWN, and UNKNOWN refuses `high` and `critical` actions with 423. That is
# the policy working, not a test defect: a suite about access grants, Android
# taps or the Armory would otherwise be measuring the lockdown policy instead
# of its subject.
#
# So a suite that is not about lockdown may ask for INACTIVE, by name:
#
#     pytestmark = pytest.mark.usefixtures("lockdown_inactive")
#
# Opt-in, never autouse, so the coupling is visible in each file that depends
# on it (the same reasoning as `test_action_guard.py`'s own pin). The policy
# is asserted where it belongs: `test_p14r1_lockdown_enforcement.py`,
# `test_lockdown_product_callers.py`, and against real Redis and PostgreSQL in
# `tests/tenancy/test_lockdown_refuses_action_routes.py`.
# ---------------------------------------------------------------------------

@pytest.fixture
def lockdown_inactive(monkeypatch):
    from utils.security_tools import LockdownState

    async def _inactive():
        return LockdownState.INACTIVE

    monkeypatch.setattr("utils.security_tools.get_lockdown_state", _inactive)


# ---------------------------------------------------------------------------
# The Trust Ledger, pinned to "accepting", for suites that are about something
# else (P1-09, 1 November 2026).
#
# Owner-confirmed actions now fail closed: when the Trust Ledger cannot record
# an action's intent, the action is refused (Class A), and only backup restore
# and access revoke fall back to the journal (Class B). This suite runs without
# PostgreSQL, so every real ledger append fails, and a suite about memory
# forget, Android taps or the Command Center would otherwise be measuring the
# ledger policy instead of its subject.
#
# So a suite that is not about the ledger may ask for one that accepts writes,
# by name, exactly as it asks for `lockdown_inactive`:
#
#     pytestmark = pytest.mark.usefixtures("ledger_recording")
#
# The fixture returns the records, in order, for a test that wants to look.
# Opt-in, never autouse, so the coupling is visible in each file. The policy is
# asserted where it belongs: `test_ledger_fail_closed.py`,
# `test_ledger_journal.py`, and against real PostgreSQL in
# `tests/tenancy/test_ledger_fail_closed_pg.py`.
# ---------------------------------------------------------------------------

@pytest.fixture
def ledger_recording(monkeypatch):
    appended: list[dict] = []

    async def _accepting(kind, *, user_id, action_id, task_id=None, record=None, **_kw):
        appended.append({"seq": len(appended) + 1, "kind": kind, "user_id": user_id,
                         "action_id": action_id, "task_id": task_id,
                         "record": record or {}})
        return appended[-1]

    monkeypatch.setattr("trust.ledger.append_record", _accepting)
    return appended


# ---------------------------------------------------------------------------
# Every test gets its own Trust Ledger fallback journal (P1-09).
#
# The journal's default home is `~/.ira/trust-journal`, the real one of
# whoever runs the suite. A test that reaches it (a restore while the ledger is
# down, an execution record the ledger refused) must not write there, and must
# not see another test's entries or this process's memory of them. Nothing is
# created unless a test writes; the directory is under pytest's own temporary
# root.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _ledger_journal_isolated(tmp_path_factory, monkeypatch):
    import uuid as _uuid

    root = tmp_path_factory.getbasetemp() / "ledger-journals" / _uuid.uuid4().hex
    monkeypatch.setenv("IRA_LEDGER_JOURNAL_DIR", str(root))
    try:
        from trust import journal as _journal
    except Exception:  # noqa: BLE001 — a suite that cannot import it cannot reach it
        yield
        return
    _journal._reset_for_tests()
    yield
    _journal._reset_for_tests()
