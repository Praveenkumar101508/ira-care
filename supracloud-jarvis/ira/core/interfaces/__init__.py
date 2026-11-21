"""Core interfaces — the seams both editions build against.

Nothing here imports an edition-specific module. That is enforced by
tests/architecture/test_edition_boundaries.py, not by convention.
"""
from core.interfaces.connector_gateway import (
    AccessLevel,
    ActionReceipt,
    Connector,
    ConnectorCall,
    ConnectorGateway,
    ConnectorResult,
    UnavailableConnectorGateway,
)
from core.interfaces.model_provider import (
    CompletionRequest,
    CompletionResult,
    Locality,
    ModelGateway,
    ModelProvider,
    ProviderDisclosure,
    Tier,
)
from core.interfaces.permission import (
    ActionRequest,
    DenyAllBroker,
    DENY_UNKNOWN,
    Effect,
    PermissionBroker,
    PermissionDecision,
    RiskLevel,
)
from core.interfaces.storage import (
    KeyValueStore,
    MemoryRecord,
    MemoryStore,
    ObjectStore,
    tenant_key,
)

__all__ = [
    # permission
    "Effect", "RiskLevel", "ActionRequest", "PermissionDecision",
    "PermissionBroker", "DenyAllBroker", "DENY_UNKNOWN",
    # model
    "Locality", "Tier", "ProviderDisclosure", "CompletionRequest",
    "CompletionResult", "ModelProvider", "ModelGateway",
    # storage
    "MemoryRecord", "MemoryStore", "KeyValueStore", "ObjectStore", "tenant_key",
    # connectors
    "AccessLevel", "ConnectorCall", "ActionReceipt", "ConnectorResult",
    "Connector", "ConnectorGateway", "UnavailableConnectorGateway",
]
