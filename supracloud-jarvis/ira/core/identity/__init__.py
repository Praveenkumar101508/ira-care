"""Identity, principal resolution and tenant scoping."""
from core.identity.auth_provider import (
    AuthenticatedSubject,
    AuthenticationFailed,
    AuthMethod,
    AuthProvider,
    PasswordAuthProvider,
    TokenSigningPolicy,
    assert_signing_policy_acceptable,
)
from core.identity.background import (
    background_principal,
    for_each_tenant,
    for_tenant,
)
from core.identity.db_context import (
    MissingTenantContext,
    assert_tenant_scoped,
    current_tenant,
    tenant_connection,
)
from core.identity.namespacing import (
    object_path,
    redis_key,
    redis_user_key,
    tenant_prefix,
)
from core.identity.principal import (
    IRA_UUID_NAMESPACE,
    PRIVATE_TENANT_ID,
    Principal,
    private_user_id,
    system_principal,
)
from core.identity.resolver import (
    CloudIdentityNotAvailable,
    require_principal,
    resolve_principal,
)

__all__ = [
    # principal
    "Principal",
    "PRIVATE_TENANT_ID",
    "IRA_UUID_NAMESPACE",
    "private_user_id",
    "system_principal",
    "resolve_principal",
    "require_principal",
    "CloudIdentityNotAvailable",
    # tenant-scoped database access
    "tenant_connection",
    "current_tenant",
    "assert_tenant_scoped",
    "MissingTenantContext",
    # namespacing for stores without RLS
    "redis_key",
    "redis_user_key",
    "object_path",
    "tenant_prefix",
    # background work
    "background_principal",
    "for_tenant",
    "for_each_tenant",
    # authentication
    "AuthMethod",
    "AuthenticatedSubject",
    "AuthenticationFailed",
    "AuthProvider",
    "PasswordAuthProvider",
    "TokenSigningPolicy",
    "assert_signing_policy_acceptable",
]
