"""security/scope_enforcer.py — default-deny route access for delegated users.

The existing require_auth only proves WHO is calling; every route was written
for a single owner. Now that delegated accounts can log in, this middleware
decides WHAT a non-owner token may reach — and the answer is "nothing", except
the short allowlist below. Unmapped paths are DENIED for delegated users, so a
new route is private-by-default until someone consciously maps it.

The primary owner (the .env admin account) is completely unaffected.
"""
from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from security.roles import (
    SCOPE_CHAT,
    SCOPE_SETTINGS_READ,
    SCOPE_VOICE,
    role_can,
)

logger = logging.getLogger("ira.scope_enforcer")

# (METHODS or None=any, path prefix, required scope). First match wins.
# Deliberately small: delegated users get chat, basic voice, and (family_admin+)
# a read-only look at the Trust Console. Memory Vault, profile, onboarding,
# access management, backups, architect, actions … are all unmapped → denied.
PATH_SCOPES: tuple[tuple[frozenset[str] | None, str, str], ...] = (
    (None, "/api/v1/chat", SCOPE_CHAT),
    (frozenset({"POST"}), "/api/v1/voice/say", SCOPE_VOICE),
    (frozenset({"POST"}), "/api/v1/voice/transcribe", SCOPE_VOICE),
    (frozenset({"GET"}), "/api/v1/voice/token", SCOPE_VOICE),
    (frozenset({"GET"}), "/api/v1/trust/status", SCOPE_SETTINGS_READ),
)

# Paths the enforcer never touches: they are public or carry their own auth
# story (login/logout/refresh must work for delegated users too).
_EXEMPT_PREFIXES = ("/auth/", "/health", "/docs", "/openapi.json", "/notifications")

_ROLE_CACHE_TTL = 30.0  # seconds; small enough that a revoke bites quickly
_role_cache: dict[str, tuple[float, str | None]] = {}


def allowed_for_role(role: str, method: str, path: str) -> bool:
    """Pure decision: may `role` call `method path`? Default deny."""
    for methods, prefix, scope in PATH_SCOPES:
        if path.startswith(prefix) and (methods is None or method.upper() in methods):
            return role_can(role, scope)
    return False


def _needs_enforcement(path: str) -> bool:
    if any(path.startswith(p) for p in _EXEMPT_PREFIXES):
        return False
    return path.startswith("/api/") or path.startswith("/ws/") or path.startswith("/mobile")


async def _cached_role(username: str) -> str | None:
    now = time.monotonic()
    hit = _role_cache.get(username)
    if hit and now - hit[0] < _ROLE_CACHE_TTL:
        return hit[1]
    from security.delegated import get_delegated_role

    role = await get_delegated_role(username)
    _role_cache[username] = (now, role)
    return role


def invalidate_role_cache(username: str | None = None) -> None:
    if username is None:
        _role_cache.clear()
    else:
        _role_cache.pop(username, None)


class DelegatedScopeMiddleware(BaseHTTPMiddleware):
    """Deny-by-default gate for non-owner bearer tokens.

    Requests without a bearer token pass through — the routes' own require_auth
    rejects those. Owner tokens pass through untouched. Delegated tokens are
    checked against the allowlist; anything unmapped gets 403.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not _needs_enforcement(path):
            return await call_next(request)

        # Read the token exactly as the routes' `HTTPBearer` does. Passing on a
        # token this middleware cannot decode is safe only if the route cannot
        # decode it either. This used to slice `header[7:]`, so "Bearer  <jwt>"
        # (two spaces) failed to decode here, passed through, and was accepted
        # by the route, whose parser strips the space: any role reached every
        # route guarded by `require_auth` alone.
        from fastapi.security.utils import get_authorization_scheme_param

        scheme, token = get_authorization_scheme_param(request.headers.get("authorization"))
        if scheme.lower() != "bearer" or not token:
            return await call_next(request)  # no token → route returns 401 itself

        from api.middleware.auth import decode_token

        try:
            username = decode_token(token).sub
        except Exception:
            return await call_next(request)  # invalid token → route returns 401 itself

        from api.middleware.auth import dev_mode_active, is_owner

        # P2-02 + 3C-04. This line carried two defects.
        #
        # The raw dev-mode setting skipped scope enforcement for every caller,
        # ungated by edition. And owner-ness was decided by comparing the
        # configured admin name against `username`, which came from the token's
        # own `sub` claim — so in cloud a tenant's user holding that name would
        # pass the middleware unrestricted on the strength of their name.
        #
        # That is 3C-04 exactly, and its guard never saw it: the guard scans
        # `api/routes/` and this file is in `security/`. Both now go through
        # helpers that refuse in cloud.
        if dev_mode_active() or is_owner(username):
            return await call_next(request)  # the primary owner is never restricted

        role = await _cached_role(username)
        if role is None or not allowed_for_role(role, request.method, path):
            logger.info("scope_enforcer: denied %s %s for %r (role=%s)",
                        request.method, path, username, role)
            return Response(
                content='{"detail":"This account\'s role does not allow that."}',
                status_code=403, media_type="application/json",
            )
        return await call_next(request)


__all__ = ["PATH_SCOPES", "allowed_for_role", "DelegatedScopeMiddleware", "invalidate_role_cache"]
