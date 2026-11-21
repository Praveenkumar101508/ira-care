"""Tenant namespacing for stores that have no Row-Level Security.

PostgreSQL enforces isolation itself (see `postgres/023_tenancy.sql`). Redis and
object storage cannot: they have a flat keyspace and will happily return
whatever key you ask for. For those, the namespace IS the boundary, so it has to
be derived — never assembled by hand at each call site, because two call sites
that disagree about the scheme is exactly how one tenant reads another's state.

Every helper here takes a :class:`Principal` and refuses without a tenant.
"""
from __future__ import annotations

import posixpath
import re
from typing import Iterable

from core.identity.db_context import MissingTenantContext
from core.identity.principal import Principal

#: Characters allowed in a caller-supplied key segment. Anything else is
#: rejected rather than escaped: a key that needs escaping to be safe is a key
#: whose safety depends on the escaper being correct everywhere.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._@:-]+$")


def _require_tenant(principal: Principal) -> str:
    if principal is None or getattr(principal, "tenant_id", None) is None:
        raise MissingTenantContext(
            "A tenant-namespaced key requires an authenticated Principal."
        )
    return str(principal.tenant_id)


def _validate(segments: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in segments:
        seg = str(raw).strip()
        if not seg:
            raise ValueError("Empty key segment")
        if seg in (".", ".."):
            raise ValueError(f"Key segment {raw!r} attempts traversal")
        if not _SAFE_SEGMENT.match(seg):
            raise ValueError(
                f"Key segment {raw!r} contains characters that are not allowed. "
                "Allowed: letters, digits, dot, underscore, at, colon, hyphen."
            )
        cleaned.append(seg)
    if not cleaned:
        raise ValueError("At least one key segment is required")
    return cleaned


def redis_key(principal: Principal, *segments: str) -> str:
    """Namespaced Redis key: ``ira:t:{tenant}:{segments...}``.

    The tenant sits immediately after a fixed prefix so a `SCAN` for one
    tenant's keys is a prefix match, and so an accidental un-namespaced key is
    visually obvious in redis-cli.
    """
    tenant = _require_tenant(principal)
    return ":".join(["ira", "t", tenant, *_validate(segments)])


def redis_user_key(principal: Principal, *segments: str) -> str:
    """Per-user key inside the tenant namespace.

    Separate from :func:`redis_key` because tenant-wide and per-user state have
    different lifetimes: deleting a user must not wipe tenant state, and
    deleting a tenant must wipe both.
    """
    tenant = _require_tenant(principal)
    user = str(principal.user_id)
    return ":".join(["ira", "t", tenant, "u", user, *_validate(segments)])


def object_path(principal: Principal, *segments: str) -> str:
    """Object-storage path: ``tenants/{tenant}/{segments...}``.

    Traversal is rejected in :func:`_validate`, then the result is normalised and
    re-checked — belt and braces, because a path that escapes its prefix in
    object storage is a cross-tenant read wearing a filename.
    """
    tenant = _require_tenant(principal)
    prefix = f"tenants/{tenant}"
    path = posixpath.normpath(posixpath.join(prefix, *_validate(segments)))
    if path != prefix and not path.startswith(prefix + "/"):
        raise ValueError(f"Resolved path {path!r} escapes tenant prefix {prefix!r}")
    return path


def tenant_prefix(principal: Principal) -> str:
    """Every Redis key for this tenant. Used by tenant deletion."""
    return f"ira:t:{_require_tenant(principal)}:*"


# ─────────────────────────────────────────────────────────────────────────────
# Workspace namespacing (Phase 3B batch 5A)
#
# Redis has a flat keyspace, so for cached workspace content the KEY is the
# boundary — there is no RLS to fall back on. Two properties matter and both are
# easy to get wrong by hand:
#
#   * the workspace id must be in the key, or two workspaces share a cache
#     entry and one reads the other's content out of Redis while the database
#     is isolating them perfectly;
#   * the TENANT id must be in the key as well, even though workspace ids are
#     UUIDs and collision is not a practical worry. It keeps deletion a prefix
#     operation: erasing a tenant is one scan over `ira:t:{tenant}:*` and it
#     reaches the workspace keys too, rather than leaving them orphaned under a
#     prefix nothing enumerates.
#
# Neither function accepts a workspace argument. Both take it from the verified
# principal, so a caller cannot pass the workspace it wishes it had.
# ─────────────────────────────────────────────────────────────────────────────


def _require_workspace(principal: Principal) -> str:
    from core.identity.db_context import MissingWorkspaceContext

    _require_tenant(principal)
    workspace_id = getattr(principal, "workspace_id", None)
    if workspace_id is None:
        raise MissingWorkspaceContext(
            "A workspace-namespaced key requires a verified active workspace. "
            "There is no unscoped fallback: a key without a workspace is a key "
            "two workspaces would share."
        )
    return str(workspace_id)


def redis_workspace_key(principal: Principal, *segments: str) -> str:
    """Namespaced Redis key: ``ira:t:{tenant}:w:{workspace}:{segments...}``.

    Nested under the tenant prefix rather than parallel to it, so
    :func:`tenant_prefix` still matches every key belonging to the tenant and
    tenant deletion does not have to know that workspaces exist.
    """
    tenant = _require_tenant(principal)
    workspace = _require_workspace(principal)
    return ":".join(["ira", "t", tenant, "w", workspace, *_validate(segments)])


def workspace_prefix(principal: Principal) -> str:
    """Every Redis key for this workspace. Used by workspace deletion and by
    invalidation that must not reach across workspaces."""
    return f"ira:t:{_require_tenant(principal)}:w:{_require_workspace(principal)}:*"


def workspace_object_path(principal: Principal, *segments: str) -> str:
    """Object-storage path: ``tenants/{tenant}/workspaces/{workspace}/{...}``.

    Same traversal rejection and re-check as :func:`object_path`. The prefix is
    verified server-side from the principal; a client-supplied object key is
    never trusted to name its own tenant or workspace, because a path is just a
    string and the store will return whatever it is asked for.
    """
    tenant = _require_tenant(principal)
    workspace = _require_workspace(principal)
    prefix = f"tenants/{tenant}/workspaces/{workspace}"
    path = posixpath.normpath(posixpath.join(prefix, *_validate(segments)))
    if path != prefix and not path.startswith(prefix + "/"):
        raise ValueError(f"Resolved path {path!r} escapes workspace prefix {prefix!r}")
    return path


__all__ = [
    "redis_key",
    "redis_user_key",
    "redis_workspace_key",
    "object_path",
    "workspace_object_path",
    "tenant_prefix",
    "workspace_prefix",
]
