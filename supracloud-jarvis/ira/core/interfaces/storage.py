"""Storage interfaces — tenant-scoped by construction.

The point of this module is one property: **you cannot express a query without
naming a tenant.** Every method takes a :class:`Principal`, and implementations
derive the tenant from it rather than accepting a tenant argument a caller could
supply. That removes the single largest cloud risk — a forgotten
``WHERE tenant_id = ...`` — from the space of things a caller can get wrong.

Phase 1 defines the seam only. Today's `memory/store.py` continues to serve IRA
Private unchanged; Phase 2 adds tenant columns, row-level security and the
adversarial isolation suite behind these signatures.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from core.identity.principal import Principal


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """A stored memory with the provenance fields the Privacy Centre needs.

    The metadata here is not decoration: Phase 5 lets a user see WHY something
    was stored and expire it. Fields added later cannot be backfilled for
    records written now, which is why they are defined before they are used.
    """

    id: str
    content: str
    #: Where it came from: "conversation", "document", "connector:gmail", ...
    source_type: str = ""
    source_reference: str = ""
    #: Why it was kept, in words a user could read.
    storage_reason: str = ""
    sensitivity: str = "normal"
    confidence: float = 1.0
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    last_used_at: Optional[str] = None
    #: Connectors permitted to use this memory. Empty = none.
    allowed_connectors: frozenset[str] = frozenset()
    #: Whether this may be sent to an external model provider.
    external_provider_policy: str = "deny"
    metadata: Mapping[str, Any] = None  # type: ignore[assignment]


@runtime_checkable
class MemoryStore(Protocol):
    """Vector-backed memory, scoped to the principal's tenant.

    Note there is no `tenant_id` parameter anywhere. That is the design: the
    tenant comes from the authenticated principal, so a caller cannot pass one
    in — and a browser cannot influence one.
    """

    async def store(self, principal: Principal, record: MemoryRecord) -> str:
        ...

    async def retrieve(
        self,
        principal: Principal,
        query: str,
        *,
        top_k: int = 5,
        min_similarity: float = 0.6,
    ) -> Sequence[MemoryRecord]:
        ...

    async def get(self, principal: Principal, record_id: str) -> Optional[MemoryRecord]:
        ...

    async def delete(self, principal: Principal, record_id: str) -> bool:
        ...

    async def expire_before(self, principal: Principal, iso_timestamp: str) -> int:
        ...


@runtime_checkable
class KeyValueStore(Protocol):
    """Ephemeral state (Redis today).

    Implementations MUST namespace keys by tenant. A shared flat keyspace is how
    two tenants end up reading each other's session state.
    """

    async def get(self, principal: Principal, key: str) -> Optional[bytes]:
        ...

    async def set(
        self, principal: Principal, key: str, value: bytes, *, ttl_seconds: int | None = None
    ) -> None:
        ...

    async def delete(self, principal: Principal, key: str) -> bool:
        ...


@runtime_checkable
class ObjectStore(Protocol):
    """Blobs (avatars, exports, attachments).

    Implementations MUST derive the path prefix from the principal's tenant and
    MUST reject a key that escapes it — `../` traversal across a tenant boundary
    is a cross-tenant read wearing a filename.
    """

    async def put(self, principal: Principal, key: str, data: bytes, *, content_type: str) -> str:
        ...

    async def get(self, principal: Principal, key: str) -> Optional[bytes]:
        ...

    async def delete(self, principal: Principal, key: str) -> bool:
        ...


def tenant_key(principal: Principal, key: str) -> str:
    """Namespace `key` under the principal's tenant.

    Provided so implementations do not each invent their own scheme — a
    divergent scheme is a collision waiting to happen.
    """
    cleaned = key.strip().lstrip("/")
    if ".." in cleaned.split("/"):
        raise ValueError(f"Key {key!r} attempts to traverse outside its tenant namespace")
    return f"tenant:{principal.tenant_id}:{cleaned}"


__all__ = [
    "MemoryRecord",
    "MemoryStore",
    "KeyValueStore",
    "ObjectStore",
    "tenant_key",
]
