"""3C-02 — no cache key reaches across a tenant or a workspace.

`core/identity/namespacing.py` was written in Batch 5A to namespace Redis keys
and object-storage paths. Until this finding it had **no caller outside itself**
— correct and inert, exactly the state 5A's workspace helpers were in before 5C
wired them. A module that is never called protects nothing, and its existence
makes the gap harder to see rather than easier, because a reader greps, finds
it, and concludes the problem is solved.

Meanwhile `api/routes/chat.py` built its own:

    f"chat:{username}:{session_id}:{hash(message)}"

Usernames are per-tenant, so two tenants' `alice` shared a prefix, with a
client-supplied `session_id` in the middle. The cached value is a chat
**response**.

The matrix here uses **the same username in every tenant**, deliberately. A
fixture with unique usernames would never catch a key that names one.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    _st.SentenceTransformer = object
    _st.CrossEncoder = object

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from tests.tenancy.adversarial import matrix  # noqa: E402,F401

from core.identity.db_context import MissingWorkspaceContext  # noqa: E402
from core.identity.namespacing import (  # noqa: E402
    object_path,
    redis_key,
    redis_workspace_key,
    tenant_prefix,
    workspace_object_path,
    workspace_prefix,
)


# ─────────────────────────────────────────────────────────────────────────────
# The keys themselves
# ─────────────────────────────────────────────────────────────────────────────


def test_the_same_key_request_differs_in_every_workspace(matrix):
    """The property the whole finding turns on.

    Same username, same segments — because that is the collision that was
    reachable. A and B are one tenant and must still differ; C is another tenant
    entirely.
    """
    a = redis_workspace_key(matrix.a.principal(), "chat", "session-1", "hash")
    b = redis_workspace_key(matrix.b.principal(), "chat", "session-1", "hash")
    c = redis_workspace_key(matrix.c.principal(), "chat", "session-1", "hash")

    assert matrix.a.username == matrix.b.username == matrix.c.username, (
        "the fixture no longer shares a username across tenants, so this test "
        "cannot catch a key that names one")
    assert len({a, b, c}) == 3, f"keys collide: {a!r} {b!r} {c!r}"


def test_a_workspace_key_sits_under_its_tenant_prefix(matrix):
    """Nested, not parallel — so deleting a tenant reaches its workspaces
    without tenant deletion having to know workspaces exist."""
    key = redis_workspace_key(matrix.a.principal(), "chat", "s")
    prefix = tenant_prefix(matrix.a.principal()).rstrip("*")

    assert key.startswith(prefix)
    assert not key.startswith(tenant_prefix(matrix.c.principal()).rstrip("*"))


def test_one_workspaces_prefix_does_not_match_anothers_keys(matrix):
    """Invalidation scoped to a workspace must not reach its neighbour's keys.

    A wildcard delete is how a cache is cleared, and a prefix that matched too
    much would evict — or expose, under SCAN — another workspace's entries.
    """
    mine = redis_workspace_key(matrix.a.principal(), "chat", "s")
    theirs = redis_workspace_key(matrix.b.principal(), "chat", "s")

    a_prefix = workspace_prefix(matrix.a.principal()).rstrip("*")
    assert mine.startswith(a_prefix)
    assert not theirs.startswith(a_prefix)


def test_a_key_cannot_be_built_without_a_workspace(matrix):
    """No unscoped fallback. A key without a workspace is a key two workspaces
    would share, which is the defect rather than a degraded mode."""
    with pytest.raises(MissingWorkspaceContext):
        redis_workspace_key(matrix.a.without_workspace(), "chat", "s")


def test_client_supplied_segments_cannot_climb_out_of_the_namespace(matrix):
    """The segments come from a request; the prefix does not.

    A session id shaped like another tenant's key must stay underneath the
    caller's own prefix — either rejected outright or contained. What must not
    happen is a key that resolves into somebody else's namespace.
    """
    hostile = f"..:..:ira:t:{matrix.c.tenant_id}:w:{matrix.c.workspace_id}"
    try:
        key = redis_workspace_key(matrix.a.principal(), "chat", hostile)
    except (ValueError, MissingWorkspaceContext):
        return  # rejected outright is a fine answer

    assert key.startswith(workspace_prefix(matrix.a.principal()).rstrip("*")), (
        "a caller-supplied segment escaped its own namespace")


def test_object_paths_are_scoped_the_same_way(matrix):
    """Object storage is the other half of the module, and the same argument
    applies: a path is just a string and the store returns whatever it is
    asked for, so the prefix has to be server-derived."""
    a = workspace_object_path(matrix.a.principal(), "files", "report.pdf")
    b = workspace_object_path(matrix.b.principal(), "files", "report.pdf")
    c = workspace_object_path(matrix.c.principal(), "files", "report.pdf")

    assert len({a, b, c}) == 3
    assert str(matrix.a.tenant_id) in a and str(matrix.a.workspace_id) in a
    assert not a.startswith(f"tenants/{matrix.c.tenant_id}")


def test_a_traversal_segment_cannot_escape_an_object_path(matrix):
    """`../` in a filename is the oldest trick there is, and object keys are
    built from filenames."""
    for hostile in ("../../etc/passwd", "..", "a/../../b"):
        try:
            path = workspace_object_path(matrix.a.principal(), "files", hostile)
        except ValueError:
            continue  # rejected outright
        assert path.startswith(f"tenants/{matrix.a.tenant_id}/"), (
            f"{hostile!r} escaped the tenant prefix: {path!r}")


def test_tenant_keys_still_differ_across_tenants(matrix):
    """The tenant-level helpers, for completeness — a workspace fix that broke
    the tenant arm would be a regression in the direction 023 already closed."""
    assert redis_key(matrix.a.principal(), "x") != redis_key(matrix.c.principal(), "x")
    assert object_path(matrix.a.principal(), "x") != object_path(matrix.c.principal(), "x")


# ─────────────────────────────────────────────────────────────────────────────
# The module is actually used
# ─────────────────────────────────────────────────────────────────────────────


def test_the_chat_route_builds_its_key_through_the_module():
    """The finding was not that the helpers were wrong. It was that nothing
    called them.

    So this asserts the wiring rather than the behaviour: a hand-built f-string
    key in this route is the defect returning, and it reads as perfectly
    ordinary code.
    """
    import pathlib
    import re

    source = pathlib.Path(__file__).resolve().parents[2].joinpath(
        "api/routes/chat.py").read_text(encoding="utf-8")

    assert "redis_workspace_key" in source, (
        "api/routes/chat.py no longer namespaces its cache key through "
        "core/identity/namespacing.py")

    handbuilt = re.findall(r'cache_key\s*=\s*f["\']', source)
    assert not handbuilt, (
        "a cache key in api/routes/chat.py is built from an f-string again. "
        "The previous one was keyed on a username, and usernames are "
        "per-tenant.")


def test_no_route_hand_builds_a_cache_key():
    """The same rule, across every route, so the next one added is covered.

    Scoped to `api/routes/` because that is where request-derived values are in
    scope — a hand-built key elsewhere may have no principal to namespace with,
    and widening this to the whole tree would produce noise rather than a guard.
    """
    import pathlib
    import re

    routes = pathlib.Path(__file__).resolve().parents[2] / "api" / "routes"
    offenders = []
    for path in sorted(routes.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in re.findall(r'cache_key\s*=\s*f["\'][^"\']*["\']', text):
            offenders.append(f"{path.name}: {match}")

    assert not offenders, (
        f"cache keys built by hand rather than through "
        f"core/identity/namespacing.py: {offenders}. A key that does not name "
        f"the tenant is one two tenants can share.")
