"""The administrator-access boundary: break-glass is a chokepoint, and it is shut.

── The finding (21 October 2026, read-only audit) ──────────────────────────

`core/models/admin_access.py` says, in its module docstring, that
`tests/architecture/test_admin_access_boundary.py` "asserts nothing else in the
codebase reads another user's rows". That file did not exist. A security module
citing a test that is not there is the defect `tests/readiness/
test_the_honesty_ledger.py` is built around: a verified claim must name a real,
passing test. This is that file.

The same audit found no test anywhere that a TENANT ADMINISTRATOR cannot read a
member's private conversations, messages or memories through the ordinary
read paths. That is the Business edition's non-negotiable B3: the admin UI and
APIs must not reveal private prompts, responses, transcripts, memories or files.

── What is pinned here ─────────────────────────────────────────────────────

Static, no database (names contain `static`, `caller` or `grant_route`, so
`-k "static or caller or grant_route"` selects them):

  * `read_for_admin` is the only coroutine in `admin_access.py`, the only
    function that takes a reader, refuses before the reader runs, and logs
    BEFORE it returns — a logging failure is re-raised, never swallowed.
  * The module's citation of this file is true.
  * `read_for_admin` is exported from `core/models/__init__.py` and has NO
    production caller. Break-glass is therefore effectively OFF: the only way to
    read another user's rows through the application is a function nothing
    calls. A caller appearing anywhere outside `admin_access.py` fails this file
    and forces a protected-change review (the owner-approval rule).
  * No route, and nothing outside `core/models/`, creates a grant. The only
    admin-access routes are the user's own view (`GET /admin-access`) and the
    direction that only reduces access (`POST /admin-access/{grant_id}/revoke`).
  * The chokepoint's contract, with no database: every non-live grant refuses
    without invoking the reader; a grant without the user's authorisation
    cannot be built.

Database-backed, real PostgreSQL, real RLS, as `ira_app` (skipped when
`IRA_TEST_DATABASE_DSN` / `IRA_TEST_ADMIN_DSN` are unset, exactly as the
tenancy suites skip):

  * `read_for_admin` refuses without a live, user-authorised grant, and the
    reader never runs.
  * With a live grant it returns the rows and `admin_access_log` holds the row
    that proves it; after the member revokes, a stale grant object is refused by
    the database trigger and the log does not grow.
  * The B3 attack: an `admin`, `owner` and `primary_owner` of the tenant each
    call the normal conversation, message and memory read paths for a MEMBER's
    rows and get nothing. The member's own reads return them (the control).
  * The attacker is what `resolve_principal` would build for that membership,
    not a straw man.

Every attack test here is written to fail if the boundary is removed; the
plants a reviewer should try are listed at the bottom. Tests marked "the
control" pass regardless.
"""
from __future__ import annotations

import ast
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

for _k, _v in {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-test",
    "REDIS_PASSWORD": "ci-test",
    "VLLM_API_KEY": "ci-test",
}.items():
    os.environ.setdefault(_k, _v)

# `memory.store` imports `memory.embeddings`, which imports `SentenceTransformer`
# by name. conftest stubs the package without that attribute; nothing here
# embeds anything.
_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    setattr(_st, "SentenceTransformer", object)
    setattr(_st, "CrossEncoder", object)

try:
    import asyncpg
except ImportError:  # pragma: no cover - the static half still runs
    asyncpg = None

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")

#: The static tests must run without a database, so the database-backed ones
#: skip individually rather than the module skipping as a whole.
needs_db = pytest.mark.skipif(
    not (DSN and ADMIN_DSN and asyncpg is not None and hasattr(asyncpg, "create_pool")),
    reason="IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
)

# tests/architecture/ -> tests/ -> ira/
IRA_ROOT = Path(__file__).resolve().parents[2]
ADMIN_ACCESS = IRA_ROOT / "core" / "models" / "admin_access.py"
MODELS_INIT = IRA_ROOT / "core" / "models" / "__init__.py"
API_DIR = IRA_ROOT / "api"

CHOKEPOINT = "read_for_admin"

#: The functions that bring a grant into being. Only `core/models/` may call
#: them: a route that could open a grant would let an administrator authorise
#: their own access, which `admin_access_grants.user_authorised_at NOT NULL`
#: exists to make impossible.
GRANT_WRITERS = frozenset({"create_grant", "build_grant"})
GRANT_WRITING_ALLOWED = frozenset({
    "core/models/store.py", "core/models/admin_access.py", "core/models/__init__.py",
})

#: Relative to the `/models` router. The user's view, and revocation. Nothing
#: that opens a grant.
EXPECTED_ADMIN_ACCESS_ROUTES = frozenset({
    ("get", "/admin-access"),
    ("post", "/admin-access/{grant_id}/revoke"),
})

REASON = "support ticket 4411: the member reported a lost reply and asked us to look"


# ── Static scanning helpers ──────────────────────────────────────────────────


def _skip(path: Path) -> bool:
    parts = path.parts
    return any(
        p in parts
        for p in ("tests", "scripts", "docs", "__pycache__", ".venv", "venv",
                  "site-packages", "future-scale", "node_modules")
    )


def _production_files() -> list[Path]:
    """Every non-test Python file in the ira package. Tests are excluded
    because proving the chokepoint works REQUIRES a test that calls it."""
    return sorted(p for p in IRA_ROOT.rglob("*.py") if not _skip(p))


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _bound_names(tree: ast.AST, name: str) -> set[str]:
    """Every local name that refers to `name` in this module.

    `from core.models import read_for_admin as _rfa` binds `_rfa`;
    `peek = read_for_admin` binds `peek`. A scan that matched the literal name
    only would be evaded by either — the first plant tried exactly that.
    """
    bound = {name}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == name:
                    bound.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            value = node.value
            if (isinstance(value, ast.Name) and value.id in bound) or (
                isinstance(value, ast.Attribute) and value.attr == name
            ):
                bound.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return bound


def _calls_to(tree: ast.AST, name: str) -> list[int]:
    """Line numbers of every call to `name`: as `name(...)`, as `x.name(...)`
    through the module or package, or through any local alias of it."""
    names = _bound_names(tree, name)
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in names:
            lines.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == name:
            lines.append(node.lineno)
    return lines


def _grant_writes(tree: ast.AST) -> list[str]:
    """Every way a module could bring a grant into being: a call to one of
    `GRANT_WRITERS`, or an INSERT into `admin_access_grants` written as SQL."""
    found: list[str] = []
    for writer in sorted(GRANT_WRITERS):
        for line in _calls_to(tree, writer):
            found.append(f"line {line}: calls {writer}()")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.lower()
            if "admin_access_grants" in text and "insert" in text:
                found.append(f"line {node.lineno}: SQL inserts into admin_access_grants")
    return found


def _admin_access_routes(tree: ast.AST) -> set[tuple[str, str]]:
    """`(method, path)` for every `@router.<method>("<path>")` whose path
    mentions admin-access."""
    routes: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)):
                continue
            if not deco.args or not isinstance(deco.args[0], ast.Constant):
                continue
            path = deco.args[0].value
            if isinstance(path, str) and "admin-access" in path:
                routes.add((deco.func.attr, path))
    return routes


# ── 1. Static: the chokepoint's shape ───────────────────────────────────────


def test_static_read_for_admin_is_the_only_reader_and_logs_before_it_returns():
    """The order the module calls "not negotiable": refuse, read, LOG, return.

    Checked on the AST so a docstring or comment cannot satisfy it, and so the
    plant that matters — deleting the `grant is None` refusal, or moving the
    log after the return — is caught by structure rather than by review.
    """
    tree = _parse(ADMIN_ACCESS)
    top = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    coroutines = [f.name for f in top if isinstance(f, ast.AsyncFunctionDef)]
    assert coroutines == [CHOKEPOINT], (
        f"admin_access.py defines coroutines {coroutines}; only {CHOKEPOINT} may "
        f"reach data, and it must be the only async path in the module")

    takes_reader = [
        f.name for f in top
        if "reader" in {a.arg for a in f.args.args + f.args.kwonlyargs}
    ]
    assert takes_reader == [CHOKEPOINT], (
        f"functions taking a reader: {takes_reader}; only {CHOKEPOINT} may")

    fn = next(f for f in top if f.name == CHOKEPOINT)

    # The first statement after the docstring refuses a missing grant.
    first = fn.body[1] if isinstance(fn.body[0], ast.Expr) else fn.body[0]
    assert isinstance(first, ast.If), "read_for_admin does not begin with a refusal"
    test = first.test
    assert (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name) and test.left.id == "grant"
        and isinstance(test.ops[0], ast.Is)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
    ), "the first check in read_for_admin is no longer `if grant is None`"

    awaited = [
        (n.lineno, n.value.func.id)
        for n in ast.walk(fn)
        if isinstance(n, ast.Await)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
    ]
    assert [name for _, name in awaited] == ["reader", "logger_fn"], (
        f"read_for_admin awaits {awaited}; it must await the reader, then the "
        f"logger, and nothing else")
    read_line, log_line = awaited[0][0], awaited[1][0]

    refusals = [
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Raise)
        and isinstance(n.exc, ast.Call)
        and isinstance(n.exc.func, ast.Name)
        and n.exc.func.id == "NotAuthorised"
    ]
    assert len(refusals) >= 4, (
        f"only {len(refusals)} NotAuthorised refusals: missing grant, wrong "
        f"administrator, wrong scope and not-live are four separate situations")
    assert max(refusals) < read_line, "a refusal sits after the read"

    returns = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert returns and min(returns) > log_line, (
        "read_for_admin returns before it logs: a crash between the two would "
        "leave an unlogged read, which is the thing this module exists to prevent")

    reraised = [
        s for n in ast.walk(fn) if isinstance(n, ast.Try)
        for h in n.handlers for s in h.body if isinstance(s, ast.Raise)
    ]
    assert reraised, "a logging failure is swallowed instead of refusing the read"


def test_static_the_module_cites_a_test_that_exists():
    """The honesty defect itself. The module names this file as its proof;
    the name must resolve to a real file — this one."""
    cited = "tests/architecture/test_admin_access_boundary.py"
    assert cited in ADMIN_ACCESS.read_text(encoding="utf-8"), (
        "admin_access.py no longer names its proof; if the proof moved, move "
        "this assertion with it rather than deleting it")
    assert (IRA_ROOT / cited).resolve() == Path(__file__).resolve()


# ── 2. Static: break-glass has no production caller ─────────────────────────


def test_read_for_admin_has_no_production_caller():
    """Exported, and uncalled. That is the state of break-glass on `main`, and it
    is the SAFE state: nothing in the application can read another user's rows
    until a caller is written, and writing one is a protected change.

    If this test fails because a caller appeared, that is the review trigger
    working. Do not add the file to an allow-list; take it to the owner.
    """
    files = _production_files()
    assert len(files) > 50, f"only {len(files)} production files found — scan is wrong"
    assert ADMIN_ACCESS in files and MODELS_INIT in files

    exported = any(
        isinstance(n, ast.ImportFrom)
        and n.module == "core.models.admin_access"
        and any(a.name == CHOKEPOINT for a in n.names)
        for n in ast.walk(_parse(MODELS_INIT))
    )
    assert exported, f"{CHOKEPOINT} is no longer exported from core.models"

    callers: list[str] = []
    for path in files:
        if path == ADMIN_ACCESS:
            continue
        try:
            tree = _parse(path)
        except SyntaxError:  # pragma: no cover - would fail the build elsewhere
            continue
        lines = _calls_to(tree, CHOKEPOINT)
        if lines:
            callers.append(f"{path.relative_to(IRA_ROOT)}: line(s) {lines}")

    assert not callers, (
        f"{CHOKEPOINT} is called in production code:\n  " + "\n  ".join(callers)
        + "\n\nBreak-glass access was OFF: the chokepoint existed and nothing "
        "called it. A caller turns it on. That is authorisation and tenant "
        "isolation, which the owner-approval rule says the owner approves first, in "
        "writing. Get that approval, then extend this test to name the one "
        "sanctioned caller — do not weaken the scan."
    )


# ── 3. Static: nothing opens a grant ────────────────────────────────────────


def test_no_grant_route_opens_or_creates_access():
    """`api/routes/models.py` says "There is no endpoint that grants admin
    access". This is the check that keeps it true, and it looks past the routes:
    nothing outside `core/models/` may call `create_grant` or `build_grant`, or
    write the INSERT itself."""
    api_files = sorted(p for p in API_DIR.rglob("*.py") if "__pycache__" not in p.parts)
    assert len(api_files) > 20, f"only {len(api_files)} files under api/ — scan is wrong"
    assert any(p.parent.name == "routes" and p.name == "models.py" for p in api_files)

    routes: set[tuple[str, str]] = set()
    for path in api_files:
        routes |= _admin_access_routes(_parse(path))
    assert routes == EXPECTED_ADMIN_ACCESS_ROUTES, (
        f"admin-access routes are {sorted(routes)}; expected only the user's "
        f"own view and revocation: {sorted(EXPECTED_ADMIN_ACCESS_ROUTES)}")

    writers: list[str] = []
    for path in _production_files():
        rel = path.relative_to(IRA_ROOT).as_posix()
        if rel in GRANT_WRITING_ALLOWED:
            continue
        try:
            tree = _parse(path)
        except SyntaxError:  # pragma: no cover
            continue
        for what in _grant_writes(tree):
            writers.append(f"{rel}: {what}")
    assert not writers, (
        "a grant can be created outside core/models/:\n  " + "\n  ".join(writers)
        + "\n\nCreating a grant needs the USER's authorisation, so it belongs "
        "to a consent flow, not to an administrative API. Owner approval first."
    )


# ── Prove the scanners can fail ─────────────────────────────────────────────


def test_static_scanner_catches_a_planted_caller(tmp_path):
    planted = tmp_path / "support_tools.py"
    planted.write_text(
        "from core.models import read_for_admin\n"
        "from core.models import admin_access as _aa\n"
        "from core.models.admin_access import read_for_admin as _rfa\n"
        "peek = read_for_admin\n"
        "\n"
        "async def four_ways(grant, reader, log):\n"
        "    kw = dict(admin_identity='s', scope='conversations', reader=reader, logger_fn=log)\n"
        "    return (await read_for_admin(grant, **kw),   # direct\n"
        "            await _aa.read_for_admin(grant, **kw),  # through the module\n"
        "            await _rfa(grant, **kw),              # import alias\n"
        "            await peek(grant, **kw))              # rebound name\n",
        encoding="utf-8",
    )
    assert len(_calls_to(_parse(planted), CHOKEPOINT)) == 4, (
        "the scan missed a direct, attribute-style, aliased or rebound call")

    innocent = tmp_path / "documented.py"
    innocent.write_text(
        '"""read_for_admin is the chokepoint; see admin_access.py."""\n'
        "# read_for_admin(grant, ...) — do not call this here\n"
        "NAME = 'read_for_admin'\n",
        encoding="utf-8",
    )
    assert _calls_to(_parse(innocent), CHOKEPOINT) == [], (
        "a docstring, comment or string was mistaken for a call")


def test_static_scanner_catches_a_planted_grant_route(tmp_path):
    planted = tmp_path / "routes_support.py"
    planted.write_text(
        "from fastapi import APIRouter\n"
        "from core.models import store\n"
        "router = APIRouter()\n"
        "\n"
        "@router.post('/admin-access')\n"
        "async def open_grant(principal):\n"
        "    return await store.create_grant(principal, {})\n"
        "\n"
        "SQL = 'INSERT INTO admin_access_grants (tenant_id) VALUES ($1)'\n",
        encoding="utf-8",
    )
    tree = _parse(planted)
    assert _admin_access_routes(tree) == {("post", "/admin-access")}
    assert _admin_access_routes(tree) != EXPECTED_ADMIN_ACCESS_ROUTES
    found = _grant_writes(tree)
    assert any("create_grant" in f for f in found), found
    assert any("SQL inserts" in f for f in found), found


# ── 4. The chokepoint's contract, with no database ──────────────────────────


async def test_caller_supplied_reader_never_runs_without_a_live_grant():
    """Six ways a grant is not live, and in none of them does the reader run.

    The reader is what touches the member's data; refusing after it ran would
    be a refusal in name only. Then the control: a live grant reads once, logs
    once, and returns — and a logger that fails turns the read into a refusal.
    """
    from core.models.admin_access import (
        AccessGrant, AdminAccessError, NotAuthorised, build_grant, read_for_admin,
    )

    support = "support@boundary.test"
    now = datetime.now(timezone.utc)

    def grant(**over) -> AccessGrant:
        base = dict(
            grant_id=str(uuid.uuid4()), tenant_id=str(uuid.uuid4()),
            subject_user_id=str(uuid.uuid4()), admin_identity=support,
            reason=REASON, scope="conversations",
            user_authorised_at=now - timedelta(minutes=1),
            granted_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
        )
        base.update(over)
        return AccessGrant(**base)

    reads: list[int] = []
    logs: list[tuple] = []

    async def reader():
        reads.append(1)
        return ["the member's private words"]

    async def logger_fn(g, what, count):
        logs.append((g.grant_id, what, count))

    refused = {
        "no grant": (None, support, "conversations"),
        "another administrator": (grant(), "someone-else@boundary.test", "conversations"),
        "another scope": (grant(), support, "memories"),
        "expired": (grant(granted_at=now - timedelta(hours=2),
                          user_authorised_at=now - timedelta(hours=2),
                          expires_at=now - timedelta(seconds=1)), support, "conversations"),
        "revoked": (grant(revoked_at=now), support, "conversations"),
        "not yet valid": (grant(granted_at=now + timedelta(minutes=5),
                                expires_at=now + timedelta(hours=1)), support, "conversations"),
    }
    for label, (g, who, scope) in refused.items():
        with pytest.raises(NotAuthorised):
            await read_for_admin(g, admin_identity=who, scope=scope,
                                 reader=reader, logger_fn=logger_fn)
        assert reads == [] and logs == [], f"{label}: the reader or logger ran"

    # User-authorised: there is no way to build a grant nobody authorised.
    with pytest.raises(AdminAccessError, match="USER authorised"):
        build_grant(tenant_id=str(uuid.uuid4()), subject_user_id=str(uuid.uuid4()),
                    admin_identity=support, reason=REASON, user_authorised_at=None)

    # The control: live grant → one read, one log line, the rows.
    live = grant()
    rows = await read_for_admin(live, admin_identity=support, scope="conversations",
                                reader=reader, logger_fn=logger_fn)
    assert rows == ["the member's private words"]
    assert reads == [1]
    assert logs == [(live.grant_id, "1 conversations record(s)", 1)]
    assert "private words" not in logs[0][1], "the log quoted the content"

    # A logging failure is a refusal, not a warning.
    async def broken(g, what, count):
        raise RuntimeError("admin_access_log unavailable")

    with pytest.raises(AdminAccessError, match="could not be logged"):
        await read_for_admin(grant(), admin_identity=support, scope="conversations",
                             reader=reader, logger_fn=broken)


# ── Database harness (tests/tenancy style) ──────────────────────────────────

TENANT = uuid.uuid4()
WORKSPACE = uuid.uuid4()
SLUG = f"aab-{TENANT.hex[:8]}"

MEMBER = uuid.uuid4()
MEMBER_EMAIL = f"member-{SLUG}@boundary.test"
MEMBER_SESSION = f"member-session-{SLUG}"
MEMBER_MESSAGES = ("member: my private question", "the answer to the member")
#: content -> `memory_embeddings.user_id`. Chat files memories under the
#: username and the Privacy Centre under the id; both are the member's own.
MEMBER_MEMORIES = {
    "member memory, filed under the id": str(MEMBER),
    "member memory, filed under the email": MEMBER_EMAIL,
}

#: Every elevated cloud tenant role (`core/identity/cloud_roles.py`). Each gets
#: its own account so the membership row and the principal agree.
ELEVATED_ROLES = ("admin", "owner", "primary_owner")
ADMINS = {role: uuid.uuid4() for role in ELEVATED_ROLES}


def _admin_email(role: str) -> str:
    return f"{role}-{SLUG}@boundary.test"


#: The administrator named on grants. Grants name a string, not a user.
SUPPORT = _admin_email("admin")


@pytest.fixture
async def pool():
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db_module._pool = db_module._pool, created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


@pytest.fixture
async def seeded(pool):
    """One cloud tenant and workspace: a member with a two-message conversation
    and two memories, and one account per elevated role.

    Yields the admin connection and the member's conversation id.

    **What this fixture leaves behind, and why.** `admin_access_grants` and
    `admin_access_log` are append-only by trigger, and `tenants`/`users`
    cascade into them, so a tenant or subject user that ever held a grant
    cannot be deleted (`test_commercial_isolation_rls.py` lives with the same).
    Teardown removes every membership, workspace, organisation and content row
    — the residue `test_zz_no_fixture_residue.py` cares about — and leaves the
    tenant row, the grant subject and the evidence rows. Administrator accounts
    are deleted: nothing references them.
    """
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await _wipe(admin)
        await admin.execute(
            "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud') "
            "ON CONFLICT (id) DO NOTHING",
            TENANT, f"Admin access boundary {SLUG}", SLUG)
        org = await admin.fetchval(
            "INSERT INTO organisations (tenant_id,name,slug) VALUES ($1,'O',$2) RETURNING id",
            TENANT, SLUG)
        await admin.execute(
            "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default) "
            "VALUES ($1,$2,$3,'W',$4,TRUE)", WORKSPACE, TENANT, org, SLUG)

        accounts = [(MEMBER, MEMBER_EMAIL, "member", "member")]
        accounts += [(ADMINS[r], _admin_email(r), r, "workspace_admin") for r in ELEVATED_ROLES]
        for user, mail, tenant_role, workspace_role in accounts:
            await admin.execute(
                "INSERT INTO users (id,email,display_name,auth_provider,status,email_verified_at) "
                "VALUES ($1,$2,'R','password','active',NOW()) ON CONFLICT (id) DO NOTHING",
                user, mail)
            await admin.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
                "VALUES ($1,$2,$3,'active')", TENANT, user, tenant_role)
            await admin.execute(
                "INSERT INTO workspace_memberships (tenant_id,workspace_id,user_id,role) "
                "VALUES ($1,$2,$3,$4)", TENANT, WORKSPACE, user, workspace_role)

        member_conv = await admin.fetchval(
            "INSERT INTO conversations (tenant_id,workspace_id,session_id,owner_user_id) "
            "VALUES ($1,$2,$3,$4) RETURNING id",
            TENANT, WORKSPACE, MEMBER_SESSION, MEMBER)
        for role, text in zip(("user", "assistant"), MEMBER_MESSAGES):
            await admin.execute(
                "INSERT INTO messages (conversation_id,tenant_id,workspace_id,owner_user_id,"
                " role,content) VALUES ($1,$2,$3,$4,$5,$6)",
                member_conv, TENANT, WORKSPACE, MEMBER, role, text)
        for content, namespace in MEMBER_MEMORIES.items():
            await admin.execute(
                "INSERT INTO memory_embeddings "
                "  (tenant_id,workspace_id,user_id,source_type,content,embedding) "
                "VALUES ($1,$2,$3,'note',$4, array_fill(0.01::real, ARRAY[1024])::vector)",
                TENANT, WORKSPACE, namespace, content)
        yield admin, member_conv
    finally:
        await _wipe(admin)
        await admin.close()


async def _wipe(admin):
    for table in ("workspace_memberships", "tenant_memberships", "messages",
                  "conversations", "memory_embeddings", "workspaces", "organisations"):
        await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1", TENANT)
    # Accounts that are the subject of a grant cannot go (see the fixture).
    await admin.execute(
        "DELETE FROM users WHERE id = ANY($1::uuid[]) AND NOT EXISTS "
        "(SELECT 1 FROM admin_access_grants g WHERE g.subject_user_id = users.id)",
        [MEMBER, *ADMINS.values()])


def _member():
    """The member, as the workspace resolver builds a cloud principal."""
    from core.identity.cloud_roles import cloud_scopes_for
    from core.identity.principal import Principal

    return Principal(user_id=MEMBER, tenant_id=TENANT, workspace_id=WORKSPACE,
                     session_id=uuid.uuid4(), edition="cloud",
                     roles=frozenset({"member"}), scopes=cloud_scopes_for("member"),
                     workspace_role="member", username=MEMBER_EMAIL, is_owner=False)


def _administrator(role: str):
    """A tenant administrator of `role`, with the workspace's admin role too.

    `is_owner` is False because `_resolve_cloud_principal` hard-codes it: a
    tenant's owner is not the deployment owner. Checked against the resolver by
    `test_db_the_attacker_is_what_the_resolver_builds`.
    """
    from core.identity.cloud_roles import cloud_scopes_for
    from core.identity.principal import Principal

    return Principal(user_id=ADMINS[role], tenant_id=TENANT, workspace_id=WORKSPACE,
                     session_id=uuid.uuid4(), edition="cloud",
                     roles=frozenset({role}), scopes=cloud_scopes_for(role),
                     workspace_role="workspace_admin", username=_admin_email(role),
                     is_owner=False)


def _grant_from(row):
    from core.models.admin_access import AccessGrant

    return AccessGrant(
        grant_id=str(row["id"]), tenant_id=str(row["tenant_id"]),
        subject_user_id=str(row["subject_user_id"]), admin_identity=row["admin_identity"],
        reason=row["reason"], scope=row["scope"],
        user_authorised_at=row["user_authorised_at"], granted_at=row["granted_at"],
        expires_at=row["expires_at"], revoked_at=row["revoked_at"],
    )


def _support_tools(admin, member_conv, support):
    """A reader and a logger for the administrator `support`.

    The reader is the SUPERUSER connection: under RLS no application principal
    other than the member can see these rows at all, which is the point of the
    attack tests below. These tests are about the chokepoint, not the reader.
    """
    from core.models.store import log_admin_access

    reads: list[int] = []

    async def reader():
        reads.append(1)
        return await admin.fetch(
            "SELECT content FROM messages WHERE conversation_id = $1 ORDER BY created_at",
            member_conv)

    async def logger_fn(grant, what, count):
        await log_admin_access(support, grant, what, count)

    return reads, reader, logger_fn


# ── 5. Database: refused without a live, user-authorised grant ──────────────


@needs_db
async def test_db_read_for_admin_refuses_a_grant_that_is_not_live(seeded):
    from core.models.admin_access import NotAuthorised, build_grant, read_for_admin
    from core.models.store import create_grant, live_grant, revoke_grant

    admin, member_conv = seeded
    member, support = _member(), _administrator("admin")
    reads, reader, logger_fn = _support_tools(admin, member_conv, support)

    async def lookup():
        return await live_grant(support, subject_user_id=str(MEMBER),
                                admin_identity=SUPPORT, scope="conversations")

    # Nothing authorised.
    assert await lookup() is None
    with pytest.raises(NotAuthorised):
        await read_for_admin(None, admin_identity=SUPPORT, scope="conversations",
                             reader=reader, logger_fn=logger_fn)

    # Expired. Seeded directly: the application cannot create a grant that is
    # already over, and `live_grant` checks the window in SQL as well.
    row = await admin.fetchrow(
        "INSERT INTO admin_access_grants (tenant_id,subject_user_id,admin_identity,reason,"
        " scope,user_authorised_at,granted_at,expires_at) "
        "VALUES ($1,$2,$3,$4,'conversations', NOW()-interval '3 hours',"
        " NOW()-interval '3 hours', NOW()-interval '1 hour') RETURNING *",
        TENANT, MEMBER, SUPPORT, REASON)
    assert await lookup() is None, "live_grant returned an expired grant"
    with pytest.raises(NotAuthorised, match="expired"):
        await read_for_admin(_grant_from(row), admin_identity=SUPPORT,
                             scope="conversations", reader=reader, logger_fn=logger_fn)

    # A live grant the member authorised — for someone else, and for another scope.
    grant_id = await create_grant(member, build_grant(
        tenant_id=TENANT, subject_user_id=MEMBER, admin_identity=SUPPORT,
        reason=REASON, scope="conversations", hours=1,
        user_authorised_at=datetime.now(timezone.utc)))
    live = await lookup()
    assert live is not None and live.grant_id == str(grant_id)
    with pytest.raises(NotAuthorised, match="not"):
        await read_for_admin(live, admin_identity=_admin_email("owner"),
                             scope="conversations", reader=reader, logger_fn=logger_fn)
    with pytest.raises(NotAuthorised, match="separate scope"):
        await read_for_admin(live, admin_identity=SUPPORT, scope="memories",
                             reader=reader, logger_fn=logger_fn)

    # Revoked by the member, through the function the route calls.
    assert await revoke_grant(member, grant_id)
    assert await lookup() is None, "live_grant returned a revoked grant"

    assert reads == [], "the reader ran during a refused read"
    assert await admin.fetchval(
        "SELECT count(*) FROM admin_access_log WHERE tenant_id = $1", TENANT) == 0


# ── 6. Database: a live grant reads once, and the log proves it ─────────────


@needs_db
async def test_db_a_live_grant_reads_once_and_the_log_row_proves_it(seeded):
    from core.models.admin_access import AdminAccessError, build_grant, read_for_admin
    from core.models.store import access_log_for_user, create_grant, live_grant, revoke_grant

    admin, member_conv = seeded
    member, support = _member(), _administrator("admin")
    reads, reader, logger_fn = _support_tools(admin, member_conv, support)

    grant_id = await create_grant(member, build_grant(
        tenant_id=TENANT, subject_user_id=MEMBER, admin_identity=SUPPORT,
        reason=REASON, scope="conversations", hours=1,
        user_authorised_at=datetime.now(timezone.utc)))
    live = await live_grant(support, subject_user_id=str(MEMBER),
                            admin_identity=SUPPORT, scope="conversations")
    assert live is not None and live.grant_id == str(grant_id)

    rows = await read_for_admin(live, admin_identity=SUPPORT, scope="conversations",
                                reader=reader, logger_fn=logger_fn)
    assert [r["content"] for r in rows] == list(MEMBER_MESSAGES)
    assert reads == [1]

    logged = await admin.fetch(
        "SELECT admin_identity, what_was_read, record_count FROM admin_access_log "
        " WHERE grant_id = $1", grant_id)
    assert len(logged) == 1, "the read returned without a log row"
    assert (logged[0]["admin_identity"], logged[0]["record_count"]) == (SUPPORT, 2)
    assert logged[0]["what_was_read"] == "2 conversations record(s)"
    for text in MEMBER_MESSAGES:
        assert text not in logged[0]["what_was_read"], "the log quoted the content"

    # The member's own surface — what `GET /admin-access` returns — shows it.
    seen = await access_log_for_user(member)
    assert [(e["admin"], e["record_count"], e["scope"]) for e in seen] == [
        (SUPPORT, 2, "conversations")]

    # The member revokes. The grant OBJECT the administrator holds still passes
    # every Python check; the database trigger refuses the log row, and the read
    # is refused with it. The log does not grow.
    assert await revoke_grant(member, grant_id)
    with pytest.raises(AdminAccessError, match="could not be logged"):
        await read_for_admin(live, admin_identity=SUPPORT, scope="conversations",
                             reader=reader, logger_fn=logger_fn)
    assert await admin.fetchval(
        "SELECT count(*) FROM admin_access_log WHERE grant_id = $1", grant_id) == 1


# ── 7. Database: the B3 attack ──────────────────────────────────────────────


@needs_db
@pytest.mark.parametrize("role", ELEVATED_ROLES)
async def test_db_a_tenant_administrator_cannot_read_a_members_private_rows(seeded, role):
    """What `/chat/history`, the context read and the Privacy Centre do, called
    by a tenant administrator naming the MEMBER's conversation, session and
    memories. Every path returns nothing of the member's.

    RLS on `conversations`/`messages` admits `owner_user_id = app.user_id`
    (ADR-0009, migration 093) and `tenant_connection`/`workspace_connection`
    bind `app.user_id` to the caller and nothing about their role; the memory
    actions narrow by the caller's own namespaces. There is no administrator
    branch in any of it, and this test is what keeps there from being one.
    """
    from core.privacy.export import export_subject_data
    from core.privacy.store import list_memories, pending_memories
    from memory.store import ensure_conversation, get_recent_messages

    _, member_conv = seeded
    attacker = _administrator(role)

    # Conversations and messages, by id and by session.
    assert await get_recent_messages(attacker, str(member_conv), limit=50) == []
    exported = await export_subject_data(attacker, scope="conversations")
    assert exported.ok, exported.error
    assert {t.table: t.count for t in exported.tables} == {"messages": 0, "conversations": 0}, (
        f"a {role}'s export carried the member's conversation")
    theirs = await ensure_conversation(attacker, MEMBER_SESSION)
    assert theirs != str(member_conv), "the member's session id resolved for an administrator"
    assert await get_recent_messages(attacker, theirs, limit=50) == []

    # Memories.
    assert {m.content for m in await list_memories(attacker)} == set()
    assert {m.content for m in await pending_memories(attacker)} == set()
    exported = await export_subject_data(attacker, scope="memories")
    assert exported.ok, exported.error
    assert {row["content"] for t in exported.tables if t.table == "memory_embeddings"
            for row in t.rows} == set(), f"a {role}'s export carried the member's memories"


@needs_db
async def test_db_the_control_the_member_reads_their_own_rows(seeded):
    """The control: the same paths, called by the member, return everything.
    Without this, the attack above would pass over an empty table."""
    from core.privacy.export import export_subject_data
    from core.privacy.store import list_memories
    from memory.store import ensure_conversation, get_recent_messages

    _, member_conv = seeded
    member = _member()

    assert await ensure_conversation(member, MEMBER_SESSION) == str(member_conv)
    msgs = await get_recent_messages(member, str(member_conv), limit=50)
    assert [m["content"] for m in msgs] == list(MEMBER_MESSAGES)
    exported = await export_subject_data(member, scope="conversations")
    assert {t.table: t.count for t in exported.tables} == {"messages": 2, "conversations": 1}
    assert {m.content for m in await list_memories(member)} == set(MEMBER_MEMORIES)


@needs_db
async def test_db_the_attacker_is_what_the_resolver_builds(seeded):
    """The hand-built administrators above carry exactly the identity, roles and
    scopes `resolve_principal` derives from the membership rows — so the attack
    is against a real tenant administrator, not a weaker stand-in."""
    from core.editions import EDITION_CLOUD
    from core.identity.resolver import resolve_principal

    for role in ELEVATED_ROLES:
        resolved = await resolve_principal(_admin_email(role), edition=EDITION_CLOUD,
                                           tenant_id=TENANT)
        built = _administrator(role)
        assert (resolved.user_id, resolved.tenant_id, resolved.roles, resolved.scopes,
                resolved.is_owner) == (built.user_id, built.tenant_id, built.roles,
                                       built.scopes, False), role
        assert resolved.roles == frozenset({role})


# ── plants.md ───────────────────────────────────────────────────────────────
#
# Plant each defect, run this file, and watch the named test go red before
# trusting it (the negative-control rule). Revert afterwards.
#
#   1. In `core/models/admin_access.py::read_for_admin`, delete the
#      `if grant is None: raise NotAuthorised(...)` block (or move
#      `return rows` above the `await logger_fn(...)`).
#        → test_static_read_for_admin_is_the_only_reader_and_logs_before_it_returns
#        → test_caller_supplied_reader_never_runs_without_a_live_grant
#        → test_db_read_for_admin_refuses_a_grant_that_is_not_live
#
#   2. In any module under `api/routes/` (or `core/` outside `admin_access.py`),
#      add `rows = await read_for_admin(grant, ...)`, with or without an import
#      alias.
#        → test_read_for_admin_has_no_production_caller
#
#   Two more, for the other halves of the boundary:
#
#   3. Add `@router.post("/admin-access")` calling `_store.create_grant(...)`
#      to `api/routes/models.py`.
#        → test_no_grant_route_opens_or_creates_access
#
#   4. Rebuild the `conversations`/`messages` policies without the
#      `owner_user_id = app.user_id` predicate (the 049 template), or make
#      `workspace_connection` bind `app.user_id` to the tenant's owner for an
#      `admin` principal.
#        → test_db_a_tenant_administrator_cannot_read_a_members_private_rows
#      (`test_db_the_control_the_member_reads_their_own_rows` stays green.)
