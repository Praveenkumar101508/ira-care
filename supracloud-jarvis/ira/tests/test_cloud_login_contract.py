"""Structural guarantees about cloud login that need no database.

The behavioural suite lives in `tests/tenancy/test_p2_cloud_login.py` and needs
real PostgreSQL, so it runs in one CI job. These are the properties that can be
checked from the source and the app object, and they run everywhere — including
the database-less `test` job, which is where a regression in them is most likely
to be introduced and least likely to be noticed.

Each one is a property the brief for this batch named, expressed as something
that fails rather than as a sentence in a document:

  * login uses the P2-03 rate-limit identity, not a second `request.client.host`
  * there is no second JWT stack — cloud login mints nothing itself
  * cloud roles never resolve through the private edition's `scopes_for`
  * the route takes no tenant, workspace, role or scope parameter
"""
from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest

# Settings are validated at import for several of the modules below. These are
# the same placeholders the database-less CI job exports; setdefault so a real
# environment is never overwritten.
for _k, _v in {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-test",
    "REDIS_PASSWORD": "ci-test",
    "VLLM_API_KEY": "ci-test",
}.items():
    os.environ.setdefault(_k, _v)

IRA_ROOT = Path(__file__).resolve().parents[1]
LOGIN_SOURCE = IRA_ROOT / "core/identity/cloud_login.py"
ROUTES_SOURCE = IRA_ROOT / "auth/routes.py"

ROUTE_PATH = "/auth/cloud/login"


def _auth_app():
    """A FastAPI app carrying only the real auth routes.

    `app.factory.create_app()` would be closer to production and is the wrong
    tool here: it builds every router, middleware and background hook in the
    process, so a failure in any of them would be reported as a cloud-login
    regression. `register_auth_routes` is the unit under test, and it is the
    same function the factory calls.
    """
    from fastapi import FastAPI
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded

    from auth.routes import register_auth_routes

    limiter = Limiter(key_func=lambda _r: "test", enabled=False)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, lambda _r, _e: None)
    register_auth_routes(app, limiter)
    return app


def _route():
    app = _auth_app()
    from tests.route_introspection import routes_at
    routes = routes_at(app, ROUTE_PATH)
    assert routes, f"{ROUTE_PATH} is not registered by register_auth_routes"
    return routes[0]


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_names(tree: ast.Module) -> set[str]:
    """Every name this module imports, however it is written.

    An `ast` walk rather than a substring search, and the whole module rather
    than its top level: this codebase imports inside functions constantly to
    keep startup cheap, so a top-level-only scan would miss almost everything.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                found.add(f"{module}.{alias.name}")
                found.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
    return found


# ─────────────────────────────────────────────────────────────────────────────
# The scanners must work, or every assertion below passes vacuously
# ─────────────────────────────────────────────────────────────────────────────


def test_the_sources_exist_and_parse():
    assert LOGIN_SOURCE.is_file() and ROUTES_SOURCE.is_file()
    assert _imported_names(_tree(LOGIN_SOURCE)), "the import scan found nothing"


def test_the_import_scanner_sees_a_function_level_import(tmp_path):
    """Proves the walk reaches nested imports, which is where this codebase
    puts nearly all of them."""
    nested = tmp_path / "nested.py"
    nested.write_text("def f():\n    from security.roles import scopes_for\n    return scopes_for\n")
    assert "scopes_for" in _imported_names(_tree(nested))


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiting — P2-03
# ─────────────────────────────────────────────────────────────────────────────


def test_the_shared_limiter_keys_on_the_trusted_proxy_identity():
    """`Limiter(key_func=get_remote_address)` keys on the socket peer, so behind
    a balancer every client shares one bucket — one attacker exhausts the login
    budget for everybody. That is P2-03, and cloud login inherits the fix only
    for as long as this stays true."""
    from app.middleware import limiter
    from core.runtime.client_ip import rate_limit_key

    assert limiter._key_func is rate_limit_key


def test_the_cloud_login_route_is_rate_limited():
    endpoint = _route().endpoint
    source = inspect.getsource(endpoint)
    assert "limiter.limit" in source or hasattr(endpoint, "__wrapped__"), (
        "the cloud login route carries no rate limit. It is unauthenticated "
        "and it runs bcrypt, so it is both a brute-force target and a CPU one.")


def test_cloud_login_does_not_read_the_client_address_itself():
    """A second `request.client.host` reader is a second, spoofable identity.

    The whole point of P2-03 is that there is one derivation. `/auth/token`
    still passes the raw peer to the canary tripwire — telemetry, not a
    security decision — and the new path should not copy even that.
    """
    source = LOGIN_SOURCE.read_text(encoding="utf-8")
    assert "request.client" not in source
    assert "X-Forwarded-For" not in source
    assert "x-forwarded-for" not in source


# ─────────────────────────────────────────────────────────────────────────────
# One JWT stack
# ─────────────────────────────────────────────────────────────────────────────


def test_cloud_login_mints_no_tokens_of_its_own():
    """It authenticates; `api.middleware.auth` issues.

    A second token constructor is a second place for `ver` to be forgotten,
    which is precisely what P2-04 was: `_make_access_token(ver: int = 0)` made
    an unversioned token expressible, so a caller who forgot the argument minted
    a credential claiming a version nobody had looked up.
    """
    imported = _imported_names(_tree(LOGIN_SOURCE))
    for forbidden in ("jose", "jwt", "jose.jwt", "_make_access_token",
                      "_make_refresh_token", "create_token"):
        assert forbidden not in imported, (
            f"core/identity/cloud_login.py imports {forbidden!r}. Cloud login "
            f"must not construct tokens — a second issuer is a second place "
            f"for the version claim to go missing.")


def test_the_route_issues_through_the_existing_login_token_path():
    """The positive half. Without it the assertion above passes with the route
    issuing nothing at all.

    Asserted against the route's own body rather than a whole-file substring.
    It used to pin the exact line `return await create_login_tokens(...)`, which
    broke the moment Batch 8 needed the minted tokens in a local in order to
    record a session from them — a true property expressed so literally that a
    correct change looked like a regression. What matters is that this route,
    and no other code path, is what issues; the shape of the statement is not
    the guarantee.
    """
    source = ROUTES_SOURCE.read_text(encoding="utf-8")
    start = source.index("async def cloud_login(")
    end = source.index("@app.post", start)
    body = source[start:end]

    assert "create_login_tokens(outcome.subject)" in body, (
        "the cloud login route no longer issues through create_login_tokens, "
        "which is the one function that looks the token version up before "
        "stamping both tokens with it.")


# ─────────────────────────────────────────────────────────────────────────────
# Role separation
# ─────────────────────────────────────────────────────────────────────────────


def test_cloud_login_never_reaches_the_private_scope_map():
    """`scopes_for("primary_owner")` returns every private scope, including
    `private_memory_read` and `security_admin`. The vocabularies collide on that
    one name, so reusing the function would hand a customer who owns a tenant
    the scope set designed for the person who owns the server."""
    imported = _imported_names(_tree(LOGIN_SOURCE))
    assert "scopes_for" not in imported
    assert "security.roles.scopes_for" not in imported


def test_the_private_and_cloud_owner_scope_sets_still_differ():
    """The collision this separation exists for is real, and stays real.

    If the two maps ever converged, every guard above would pass while granting
    the thing they exist to withhold — so the difference is asserted rather than
    assumed.
    """
    from core.identity.cloud_roles import cloud_scopes_for
    from security.roles import scopes_for

    assert cloud_scopes_for("primary_owner") != scopes_for("primary_owner")
    assert "private_memory_read" not in cloud_scopes_for("primary_owner")
    assert "security_admin" not in cloud_scopes_for("primary_owner")


# ─────────────────────────────────────────────────────────────────────────────
# The route accepts no authority
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "forbidden", ["tenant_id", "workspace_id", "role", "scopes", "is_owner"],
)
def test_the_route_takes_no_authority_parameter(forbidden):
    """The strongest form of "request input is not authority": there is nothing
    to forge, because FastAPI binds no such parameter.

    A tenant parameter is the one a future batch will legitimately add — and
    when it does, `_resolve_cloud_principal` verifies it against membership
    before honouring it. Until then its absence is the guarantee.
    """
    route = _route()
    names = {p.name for p in route.dependant.query_params}
    names |= {p.name for p in route.dependant.body_params}
    names |= {p.name for p in route.dependant.path_params}
    names |= {p.name for p in route.dependant.header_params}

    assert forbidden not in names, (
        f"{ROUTE_PATH} binds a {forbidden!r} parameter. Anything the request "
        f"can say about its own authority is something an attacker can say.")


def test_the_route_binds_exactly_the_credential_parameters():
    """Pins the whole signature, not just the absence of five names.

    An allow-list rather than a deny-list: a sixth authority-shaped parameter
    nobody thought to forbid would pass every assertion above.
    """
    route = _route()
    names = {p.name for p in route.dependant.body_params}

    assert names == {"email", "password", "totp_code", "recovery_code"}, (
        f"{ROUTE_PATH} binds {sorted(names)}. Adding an input to the "
        f"unauthenticated login route is a decision to argue, not a "
        f"convenience — update this assertion deliberately.")


# ─────────────────────────────────────────────────────────────────────────────
# MFA recovery codes — batch 7
# ─────────────────────────────────────────────────────────────────────────────
#
# `recovery_code` is the fourth parameter this route has ever taken, and it was
# argued rather than added: it is a CREDENTIAL, in the same class as `password`
# and `totp_code`, and carries no statement about who the caller is or what they
# may do. The parametrised test above still runs against it — a recovery code is
# not a way to name a tenant, a role or a scope, and the assertions that say so
# apply to the route as a whole.

RECOVERY_PATH = "/auth/cloud/mfa/recovery-codes"


def _routes_at(path: str):
    app = _auth_app()
    from tests.route_introspection import routes_at
    found = routes_at(app, path)
    assert found, f"{path} is not registered by register_auth_routes"
    return found


def test_recovery_codes_can_actually_be_obtained():
    """A login parameter with no way to issue what it accepts is a dead field.

    Worse than dead: a `recovery_code` form field on a live authentication
    route reads, to anybody auditing the surface, as a working way back in. The
    issuing endpoint is what makes the claim true.
    """
    methods = set()
    for route in _routes_at(RECOVERY_PATH):
        methods |= set(getattr(route, "methods", ()) or ())
    assert "POST" in methods, (
        f"{RECOVERY_PATH} cannot issue codes, so nothing can ever satisfy the "
        f"`recovery_code` parameter on {ROUTE_PATH}")


def test_issuing_recovery_codes_requires_an_authenticated_session():
    """The whole mechanism turns on this.

    Codes are for somebody who still knows their password and has lost their
    authenticator. An unauthenticated issue endpoint would hand a second factor
    to anybody who asked — it would BE the takeover it exists to prevent.
    """
    for route in _routes_at(RECOVERY_PATH):
        names = {p.name for p in route.dependant.query_params}
        names |= {p.name for p in route.dependant.body_params}
        assert "user" not in names, (
            f"{RECOVERY_PATH} binds `user` as request input rather than "
            f"resolving it from the bearer token — the caller would be naming "
            f"whose codes to issue")
        assert _security_requirements(route), (
            f"{RECOVERY_PATH} has no authentication dependency. Anyone could "
            f"retire a user's codes, or mint themselves a second factor.")


def _security_requirements(route) -> list:
    """Every security dependency the route resolves, at any depth.

    `require_auth` sits behind `HTTPBearer`, so the requirement is registered on
    a sub-dependency rather than the route's own dependant — a top-level-only
    check would report "no authentication" on a route that has it, and pass on
    one that does not once somebody wrapped the dependency.
    """
    # P16-R1b: FastAPI 0.141 removed `Dependant.security_requirements`. The
    # property this function is actually about — "does this route resolve a
    # security scheme?" — is now read directly: a security dependency is one
    # whose `call` IS a `SecurityBase` (HTTPBearer, APIKeyHeader, OAuth2...).
    #
    # That is a closer statement of the thing than the old attribute was, and
    # it cannot be satisfied by a dependency that merely declares scopes.
    from fastapi.security.base import SecurityBase

    def _is_security(dep) -> bool:
        return isinstance(getattr(dep, "call", None), SecurityBase)

    found = [route.dependant] if _is_security(route.dependant) else []
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if _is_security(dep):
            found.append(dep)
        stack.extend(dep.dependencies)
    return found


def test_the_security_scanner_reports_nothing_for_an_open_route():
    """Proves the scanner above discriminates. It walks a dependency tree, and a
    walk that returned something for everything would pass vacuously."""
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/open")
    async def _open():  # pragma: no cover - never called
        return {}

    # No emptiness guard here: this test builds its OWN minimal app with a
    # handful of routes, so the guard (which asserts the several-hundred-route
    # real app) would fire on a correct test. The guard belongs on scans of
    # the real application.
    from tests.route_introspection import routes_at
    route = routes_at(app, "/open")[0]
    assert not _security_requirements(route)


def test_the_count_endpoint_never_returns_a_code():
    """It reports how many remain, and a remaining code is still a live
    credential — showing any part of one on a routine GET would leak the factor
    to whatever logs, caches or proxies the response passes through."""
    source = ROUTES_SOURCE.read_text(encoding="utf-8")
    start = source.index("async def count_recovery_codes")
    end = source.index("@app.post", start)
    body = source[start:end]

    for leaking in ("code_hash", "issued.codes", "consume_recovery_code",
                    "generate_recovery_codes"):
        assert leaking not in body, (
            f"the recovery-code count endpoint references {leaking!r}. It must "
            f"report a number and nothing else.")


@pytest.mark.asyncio
async def test_the_login_route_forwards_the_recovery_code():
    """Binding the parameter is not the same as passing it on.

    Every behavioural test in `tests/tenancy/test_p2_mfa_recovery.py` calls
    `authenticate_cloud_user` directly, so all twenty-two of them keep passing
    with the route accepting `recovery_code` and dropping it on the floor —
    verified by planting exactly that. The result is the worst shape available:
    a documented, rate-limited, apparently-working way back in that silently
    refuses every code, discovered by a user at the moment they have already
    lost their authenticator.

    No database. `authenticate_cloud_user` is replaced with a recorder that
    refuses, so the route returns 401 without minting anything and what is under
    test is the wiring alone.
    """
    import httpx

    from core.identity import cloud_login as cloud_login_module

    seen: dict = {}

    class _Refused(Exception):
        pass

    async def _record(email_, password_, **kwargs):
        seen["args"] = (email_, password_)
        seen["kwargs"] = kwargs
        raise cloud_login_module.CloudMfaRequired("nope")

    app = _auth_app()
    original_auth = cloud_login_module.authenticate_cloud_user
    import core.editions as editions_module
    original_edition = editions_module.current_edition
    cloud_login_module.authenticate_cloud_user = _record
    editions_module.current_edition = lambda: editions_module.EDITION_CLOUD
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as client:
            response = await client.post(ROUTE_PATH, data={
                "email": "someone@example.test",
                "password": "irrelevant",
                "recovery_code": "ABCDE-FGHIJ",
            })
    finally:
        cloud_login_module.authenticate_cloud_user = original_auth
        editions_module.current_edition = original_edition

    assert response.status_code == 401, response.text
    assert seen.get("kwargs", {}).get("recovery_code") == "ABCDE-FGHIJ", (
        f"{ROUTE_PATH} did not forward `recovery_code` to "
        f"authenticate_cloud_user; it passed {seen.get('kwargs')}. The "
        f"parameter is bound and discarded, which reads as a working recovery "
        f"path and is not one.")
