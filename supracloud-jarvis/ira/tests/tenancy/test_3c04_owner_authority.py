"""3C-04 — owner authority cannot be acquired by picking a username.

`is_owner()` was `username == get_settings().ira_admin_username`.

`ira_admin_username` is **one deployment-wide string**. A username is
**per-tenant** — that is the same property 3C-02 turned into a cache-key
collision, and here it is an authorisation decision. In a single-owner
deployment the comparison is sound: there is one account and one owner. In a
multi-tenant deployment it grants deployment-wide owner authority to whichever
tenant's user happens to be called `admin`.

What that authority reaches:

  * `GET /backup/download/{filename}` — a plain `pg_dump` of the **entire**
    database. Not one tenant's export: every tenant's conversations, messages,
    memory vault and file metadata, in one gzip, over HTTP.
  * `POST /backup/restore` — arbitrary SQL applied with the database's own
    credentials. That is not a data read, it is the end of RLS.
  * `is_owner=` passed into the agent layer, which decides **which tools the
    model may execute**. Phase 3C requires internal tools to run on verified
    user authority; this made that flag a name collision.

Thirteen route modules wrote the comparison out by hand. `Principal.is_owner`
has existed since Phase 1 with a docstring saying, in as many words, that it
exists "so cloud principals can never become the owner by name collision" — and
nothing outside `life_map` and `today` used it. That is 3C-02's shape for the
third time: the correct mechanism present, documented, and not called.

── The fix ─────────────────────────────────────────────────────────────────

`is_owner()` returns False in cloud, whatever the name is, and every gate goes
through it. Fail closed: a deployment whose owner model is not yet built has no
owner, so nothing is owner-gated open. It is not a claim that cloud owner
authority is *implemented* — it is a refusal to let a string comparison stand in
for it.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

# Assembling the real app pulls memory.embeddings and worker.notifier; conftest
# stubs them but leaves attributes the import chain reads unset. Same fill-in as
# tests/tenancy/test_workspace_scope_boundary.py. The app is built to be READ.
_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    _st.SentenceTransformer = object
    _st.CrossEncoder = object
_wn = sys.modules.get("worker.notifier")
if _wn is not None and not hasattr(_wn, "REDIS_NOTIFY_CHANNEL"):
    _wn.REDIS_NOTIFY_CHANNEL = "ira:notify"
    _wn.notify_channel = lambda principal=None: "ira:notify"

pytest.importorskip("fastapi")
pytest.importorskip("slowapi")

from fastapi import HTTPException  # noqa: E402

from api.middleware.auth import (  # noqa: E402
    OWNER_ACCESS_REQUIRED,
    is_owner,
    require_owner,
)
from config import get_settings  # noqa: E402
from core.editions import EDITION_CLOUD, EDITION_PRIVATE  # noqa: E402

ROUTES = pathlib.Path(__file__).resolve().parents[2] / "api" / "routes"


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_CLOUD)


@pytest.fixture
def private(monkeypatch):
    monkeypatch.setattr("core.editions.current_edition", lambda: EDITION_PRIVATE)


@pytest.fixture
def admin_username() -> str:
    return get_settings().ira_admin_username


# ─────────────────────────────────────────────────────────────────────────────
# The decision
# ─────────────────────────────────────────────────────────────────────────────


def test_the_configured_admin_name_grants_nothing_in_cloud(cloud, admin_username):
    """The finding, in one assertion.

    This is not a guessed value or a forged header. It is a tenant's user
    holding an ordinary username that happens to equal a deployment setting.
    """
    assert is_owner(admin_username) is False, (
        "a username equal to ira_admin_username still grants deployment-wide "
        "owner authority in cloud — which reaches a pg_dump of every tenant and "
        "the tool-execution flag")


def test_no_username_at_all_is_the_owner_in_cloud(cloud, admin_username):
    """Not just the configured name: nothing is. Cloud owner authority is not
    implemented, so the honest answer for every input is no."""
    for candidate in (admin_username, admin_username.upper(), "admin", "root",
                      "owner", "", "praveen"):
        assert is_owner(candidate) is False, f"{candidate!r} became the owner"


def test_the_private_edition_still_has_an_owner(private, admin_username):
    """The single-owner deployment is the case the comparison was written for
    and it must keep working — a fix that logged everyone out of their own
    machine would be a worse outcome than the bug."""
    assert is_owner(admin_username) is True


def test_a_stranger_is_not_the_owner_in_private(private, admin_username):
    """Positive control for the test above: if `is_owner` returned True
    unconditionally in private, every assertion here would still pass without
    it."""
    assert is_owner(admin_username + "x") is False
    assert is_owner("") is False


def test_a_case_variant_is_not_the_owner(private, admin_username):
    """Usernames are compared exactly. A case-insensitive match would widen the
    owner set by however many spellings the name has."""
    flipped = admin_username.upper()
    if flipped == admin_username:
        flipped = admin_username.lower()
    if flipped == admin_username:
        pytest.skip("configured admin username has no case variant")
    assert is_owner(flipped) is False


# ─────────────────────────────────────────────────────────────────────────────
# The dependency every gate now goes through
# ─────────────────────────────────────────────────────────────────────────────


def test_the_owner_gate_refuses_in_cloud(cloud, admin_username):
    with pytest.raises(HTTPException) as exc:
        require_owner(admin_username)
    assert exc.value.status_code == 403


def test_the_owner_gate_admits_the_owner_in_private(private, admin_username):
    assert require_owner(admin_username) == admin_username


def test_the_owner_gate_refuses_a_stranger_in_private(private, admin_username):
    with pytest.raises(HTTPException) as exc:
        require_owner(admin_username + "-not")
    assert exc.value.status_code == 403


def test_the_refusal_is_not_an_oracle(cloud, private, admin_username):
    """"You are not the owner" and "this deployment has no owner" must read the
    same.

    A distinct message for the cloud case would tell an unauthenticated prober
    which edition a deployment runs and whether a given username is the
    configured one — small, but it is exactly the enumeration oracle Phase 3C
    asks about, and it costs nothing to not have.
    """
    # `private` and `cloud` are both requested so the order of application is
    # explicit; re-patch per branch rather than relying on fixture ordering.
    import core.editions

    original = core.editions.current_edition
    try:
        core.editions.current_edition = lambda: EDITION_CLOUD
        with pytest.raises(HTTPException) as in_cloud:
            require_owner(admin_username)
        core.editions.current_edition = lambda: EDITION_PRIVATE
        with pytest.raises(HTTPException) as in_private:
            require_owner(admin_username + "-not")
    finally:
        core.editions.current_edition = original

    assert in_cloud.value.status_code == in_private.value.status_code == 403
    assert in_cloud.value.detail == in_private.value.detail == OWNER_ACCESS_REQUIRED


# ─────────────────────────────────────────────────────────────────────────────
# Nothing re-implements it
# ─────────────────────────────────────────────────────────────────────────────

#: The one module allowed to read these settings: it is where the comparisons
#: live.
COMPARISON_HOME = "api/middleware/auth.py"

#: Deployment-wide identity settings. Each is ONE string for the whole
#: deployment, and each was being compared against a per-tenant username.
#: `ira_voice_service_username` is the sharper of the two — see 3C-08 — because
#: matching it makes the request body's `is_voice_owner` authoritative.
IDENTITY_SETTINGS = ("ira_admin_username", "ira_voice_service_username")

#: Call sites that pass the admin username as a VALUE rather than comparing it.
#: These are not authorisation decisions. They are recorded here so the guard
#: below stays a guard about gates and does not become a blanket ban it would be
#: tempting to silence.
VALUE_USES = {
    "brain.py": "identity handed to the cognitive kernel — see 3C-05",
    "chat.py": "identity handed to the cognitive kernel, behind an is_owner gate",
}


#: Every directory holding executable backend code. P2-07 is why this is not
#: just `api/routes/`: the scope enforcer decided owner-ness by comparing the
#: token's `sub` claim against the configured admin name, and the guard below
#: never saw it because it lives in `security/`. A guard that scans one
#: directory proves something about one directory.
SCANNED_DIRS = (
    "api", "security", "auth", "app", "agents", "commands", "worker",
    "voice", "core", "memory", "tasks", "actions", "utils",
)


#: Comparisons that DENY rather than grant. `is_primary_owner_username` exists
#: to protect the owner's name: it refuses to create a delegated account with
#: it, refuses to authenticate one, and refuses to revoke or demote the primary
#: owner. A `True` there is strictly more restrictive, so the cloud problem
#: 3C-04 describes — a name collision granting authority — cannot arise from it.
#:
#: This is an exception with a claim attached, not a silencer. The claim is
#: pinned by `test_the_owner_name_check_is_only_ever_used_to_refuse` below.
DENY_DIRECTION = {
    "security/delegated.py": (
        "is_primary_owner_username and reserved_identity — reserve the owner's "
        "and the voice service's names; every caller refuses on a match"),
}

#: Every call site of that helper, at this head. A NEW caller must be reviewed
#: for direction rather than inheriting the exception silently.
OWNER_NAME_CHECK_CALLERS = {
    "security/delegated.py",   # validate_new_username, authenticate_delegated
    "api/routes/access.py",    # revoke and role-change, both refusing
}


def _backend_sources():
    ira = pathlib.Path(__file__).resolve().parents[2]
    for name in SCANNED_DIRS:
        directory = ira / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path.relative_to(ira).as_posix(), path


def test_no_backend_module_decides_authority_by_comparing_the_admin_username():
    """The rule, across all executable backend code.

    P2-07 escaped because this guard scanned `api/routes/` only. The comparison
    reads as completely ordinary code — it is what the codebase did everywhere
    for a year — so the guard is on the shape, not on a list of known offenders,
    and the directory list is wide enough that the next one cannot hide by
    living somewhere else.
    """
    offenders = []
    for rel, path in _backend_sources():
        if rel == COMPARISON_HOME or rel in DENY_DIRECTION:
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            setting = next((s for s in IDENTITY_SETTINGS if s in line), None)
            if setting is None:
                continue
            # A comparison — however the left-hand side is spelled: `username`,
            # `principal.username`, `token_sub`, `claims.sub`.
            if re.search(rf"(==|!=)\s*[^=]*{setting}|{setting}\s*(==|!=)", line):
                offenders.append(f"{rel}: {line.strip()}")

    assert not offenders, (
        f"authority decided by comparing a deployment-wide identity setting "
        f"against a per-tenant name: {offenders}\n\n"
        f"That setting is one string for the whole deployment; a username is "
        f"per-tenant. Use `is_owner()` / `require_owner`, which refuse in "
        f"cloud. This is P2-07 and 3C-04.")


def test_the_owner_name_check_is_only_ever_used_to_refuse():
    """The claim behind the one exception above.

    `is_primary_owner_username` is allowed to compare the admin name because
    every caller uses the answer to say no. If a new call site appears, it has
    to be read — a helper that refuses in four places and grants in a fifth is
    exactly the shape 3C-04 was.
    """
    callers = set()
    for rel, path in _backend_sources():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            if "is_primary_owner_username" in line and "def " not in line:
                callers.add(rel)

    unexpected = sorted(callers - OWNER_NAME_CHECK_CALLERS)
    assert not unexpected, (
        f"new call sites for is_primary_owner_username: {unexpected}. Confirm "
        f"each uses the answer to REFUSE. The helper is exempt from the "
        f"comparison guard only because it never grants.")

    gone = sorted(OWNER_NAME_CHECK_CALLERS - callers)
    assert not gone, (
        f"{gone} no longer call is_primary_owner_username — narrow "
        f"OWNER_NAME_CHECK_CALLERS so the claim keeps meaning something.")


#: Every module calling `reserved_identity`. It compares both identity settings
#: and shares the exemption above for the same reason: in each of its three
#: callers (validate_new_username, authenticate_delegated, get_delegated_role)
#: a match refuses.
RESERVED_IDENTITY_CALLERS = {"security/delegated.py"}


def test_the_reserved_identity_check_is_only_ever_used_to_refuse():
    """The same claim for the second helper. A caller elsewhere must be read
    for direction before it joins this set."""
    callers = set()
    for rel, path in _backend_sources():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if re.search(r"\breserved_identity\(", line) and "def reserved_identity" not in line:
                callers.add(rel)

    assert callers == RESERVED_IDENTITY_CALLERS, (
        f"reserved_identity is called from {sorted(callers)}, expected "
        f"{sorted(RESERVED_IDENTITY_CALLERS)}. Confirm each new caller REFUSES "
        f"on a match, or narrow the set if a caller went away.")


def test_the_widened_guard_actually_scans_more_than_one_directory():
    """Anti-vacuity for the widening itself.

    If `SCANNED_DIRS` resolved to nothing, or to only `api`, the guard above
    would be green for the same reason it was green while P2-07 sat in
    `security/`.
    """
    seen = {rel.split("/")[0] for rel, _ in _backend_sources()}
    assert "security" in seen, "the guard is not scanning security/ — P2-07's home"
    assert len(seen) >= 6, f"the guard scans only {sorted(seen)}"
    assert sum(1 for _ in _backend_sources()) > 200


def test_no_route_decides_authority_by_comparing_the_admin_username():
    """The original narrower check, kept as a fast, targeted signal."""
    offenders = []
    for path in sorted(ROUTES.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            setting = next((s for s in IDENTITY_SETTINGS if s in line), None)
            if setting is None:
                continue
            # A comparison — `==` or `!=` — is a gate.
            if re.search(rf"(==|!=)\s*[^=]*{setting}|{setting}\s*(==|!=)", line):
                offenders.append(f"{path.name}: {line.strip()}")
            elif path.name not in VALUE_USES:
                offenders.append(
                    f"{path.name}: {line.strip()}  (undocumented use — if this "
                    f"is not a gate, add it to VALUE_USES with a reason)")

    assert not offenders, (
        f"authority decided by username comparison: {offenders}\n\n"
        f"`ira_admin_username` is one deployment-wide string and a username is "
        f"per-tenant, so the comparison grants owner authority to any tenant's "
        f"user holding that name. Use `is_owner()`/`require_owner`, which "
        f"refuse in cloud.")


def test_the_guard_can_actually_see_a_comparison():
    """The guard above is a regex over source, and a regex that matched nothing
    would be permanently, silently green. So: plant the shape in a string here
    and confirm the pattern fires on it."""
    planted = "    if username != get_settings().ira_admin_username:"
    assert re.search(r"(==|!=)\s*[^=]*ira_admin_username|ira_admin_username\s*(==|!=)",
                     planted), "the guard's pattern no longer matches the defect it exists to catch"


def test_the_comparison_still_exists_somewhere():
    """Counterpart to the guard: the rule is "one place", not "nowhere".

    If the comparison vanished entirely, `is_owner` would be returning a
    constant and the private edition would have no owner — which the tests above
    would catch, but this says why in one line.
    """
    text = (pathlib.Path(__file__).resolve().parents[2]
            / COMPARISON_HOME).read_text(encoding="utf-8")
    for setting in IDENTITY_SETTINGS:
        assert setting in text, (
            f"{COMPARISON_HOME} no longer reads {setting}, so nothing decides "
            f"that identity in the private edition")


# ─────────────────────────────────────────────────────────────────────────────
# The gates are still attached
# ─────────────────────────────────────────────────────────────────────────────

#: Routes that must resolve the owner dependency. Without this the whole file
#: passes on an application that deleted every gate — "no comparison found" is
#: exactly what a deleted check looks like to the guard above.
MUST_REQUIRE_OWNER = (
    "/api/v1/backup/list",
    "/api/v1/backup/download/{filename}",
    "/api/v1/backup/create",
    "/api/v1/backup/restore",
)


@pytest.fixture(scope="module")
def app():
    from app.factory import create_app

    return create_app()


def _dependency_calls(route) -> set:
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return set()
    found, stack = set(), [dependant]
    while stack:
        node = stack.pop()
        if node.call is not None:
            found.add(node.call)
        stack.extend(node.dependencies)
    return found


def test_the_owner_gated_routes_still_resolve_the_gate(app):
    """Positive control, and the reason the backup paths are named explicitly.

    `/backup/download` returns a `pg_dump` of the whole database. If its
    dependency were dropped, every guard in this file would still be green: no
    comparison, no offender, no failure. Only this notices.
    """
    routes = {}
    from tests.route_introspection import iter_routes, assert_enumeration_is_not_empty
    assert_enumeration_is_not_empty(app)
    # P16-R1b: key on the EFFECTIVE path — see route_introspection.
    for route, effective_path in iter_routes(app):
        if effective_path:
            routes.setdefault(effective_path, []).append(route)

    missing = []
    for path in MUST_REQUIRE_OWNER:
        matched = routes.get(path, [])
        if not matched:
            missing.append(f"{path} (route not found — renamed?)")
            continue
        if not any(require_owner in _dependency_calls(r) for r in matched):
            missing.append(path)

    assert not missing, (
        f"these routes no longer resolve the owner gate: {missing}. "
        f"/backup/download returns a pg_dump of every tenant's data and "
        f"/backup/restore applies arbitrary SQL with the database's own "
        f"credentials.")
