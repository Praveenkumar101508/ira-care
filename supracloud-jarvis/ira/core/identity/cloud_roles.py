"""Cloud tenant roles → scopes. A SEPARATE map from the private edition's.

`security/roles.py` maps IRA Private's roles — `viewer`, `trusted_user`,
`family_admin`, `owner_equivalent`, `primary_owner`. `tenant_memberships.role`
is a different vocabulary entirely: `primary_owner`, `owner`, `admin`, `member`,
`viewer`, `billing_admin`, `security_admin`.

The two overlap on exactly two names, and that overlap is a trap rather than a
convenience:

    scopes_for("primary_owner")  →  every scope, including security_admin and
                                    private_memory_read

`private_memory_read` is documented in `security/roles.py` as owner-private:
*"only primary_owner has private_memory_read (owner-private memories stay
private)"*. That sentence is about the OWNER OF THE DEPLOYMENT — one person, one
box. A tenant's primary owner is a customer. Reusing `scopes_for` for cloud
would have silently handed every customer who owns a tenant the scope set
designed for the person who owns the server, and it would have looked correct
at the call site because the role name matched.

So there is a second map, and the names are checked against the database's own
vocabulary by `test_p2_cloud_identity.py` rather than trusted from here — the
CHECK constraint on `tenant_memberships.role` is the authority for what a role
can be, and a map that drifts from it fails closed but silently.

── The two scopes cloud never grants ───────────────────────────────────────

`private_memory_read` and `security_admin` appear in NO cloud role. The first
is the owner's private memory on a single-owner box; there is no cloud analogue
and inventing one here would be inventing a capability rather than mapping one.
The second gates deployment-level security administration — `is_owner`, the
canary tripwire, the owner gate — which no tenant may reach; `Principal.is_owner`
is hard-false in cloud (3C-04, 3C-08, P2-07) and a scope that implied otherwise
would be a second route to the same authority.

`billing_admin` and `security_admin` are TENANT roles whose capabilities are
billing and tenant-security surfaces that do not exist yet. They are mapped to
the same scopes as `member` rather than left unmapped, because an unmapped role
yields the empty set and therefore denies everything — which reads in production
as "billing admin is broken" rather than "billing has not been built". Their
elevated capabilities arrive with the surfaces they administer.
"""
from __future__ import annotations

from security.roles import (
    SCOPE_ACTIONS_APPROVE,
    SCOPE_BILLING_MANAGE,
    SCOPE_BILLING_READ,
    SCOPE_ACTIONS_REQUEST,
    SCOPE_CHAT,
    SCOPE_SETTINGS_READ,
    SCOPE_SETTINGS_WRITE,
    SCOPE_SHARED_MEMORY_READ,
    SCOPE_TRUST_LEDGER_READ,
    SCOPE_SHARED_MEMORY_WRITE,
    SCOPE_VOICE,
)

#: Every role `tenant_memberships.role` permits. Kept as a frozenset so a role
#: the database allows but this module has never heard of is detectable, rather
#: than silently collapsing to "no scopes".
CLOUD_TENANT_ROLES: frozenset[str] = frozenset({
    "primary_owner", "owner", "admin", "member",
    "viewer", "billing_admin", "security_admin",
})

_READ_ONLY = frozenset({SCOPE_CHAT})

_MEMBER = frozenset({
    SCOPE_CHAT, SCOPE_VOICE,
    SCOPE_SHARED_MEMORY_READ, SCOPE_SHARED_MEMORY_WRITE,
    SCOPE_ACTIONS_REQUEST,
})

# `admin` and above may read the tenant's Trust Ledger; `member` and `viewer`
# may not. P10-02, on the owner's decision — the alternative was a customer with
# an audit trail nobody on their side could inspect.
# `admin` and above may also read the tenant's commercial position (P11-05).
_ADMIN = _MEMBER | frozenset({
    SCOPE_ACTIONS_APPROVE, SCOPE_SETTINGS_READ, SCOPE_TRUST_LEDGER_READ,
    # READ only. `SCOPE_BILLING_MANAGE` is deliberately absent: an engineer with
    # `admin` should not be able to commit the tenant to a recurring charge, or
    # cancel the plan it runs on, because their role happened to include the
    # scope that lets them see the invoice.
    SCOPE_BILLING_READ,
})

# The owner roles add settings-writing and, from B2, the authority to
# COMMIT the tenant to a recurring charge. `_ADMIN` deliberately does not
# gain it: reading what is owed and buying are different authorities.
_OWNER = _ADMIN | frozenset({SCOPE_SETTINGS_WRITE, SCOPE_BILLING_MANAGE})

CLOUD_ROLE_SCOPES: dict[str, frozenset[str]] = {
    "primary_owner": _OWNER,
    "owner": _OWNER,
    "admin": _ADMIN,
    "member": _MEMBER,
    "viewer": _READ_ONLY,
    # ...and `billing_admin` stops being one of those, for the same reason
    # `security_admin` did. The header said their "elevated capabilities arrive
    # with the surfaces they administer"; P11-05 built the billing surface, so
    # this is the role it was named for.
    #
    # The billing scope ONLY — not `_ADMIN` — because paying the bill is not
    # approving actions or writing settings. A finance person who can read what
    # the company owes should not thereby be able to approve a governed action.
    "billing_admin": _MEMBER | frozenset({SCOPE_BILLING_READ,
                                          SCOPE_BILLING_MANAGE}),
    # ...and `security_admin` stops being one of those, because the surface it
    # administers now exists. The header said their "elevated capabilities
    # arrive with the surfaces they administer"; the tenant Trust Ledger is the
    # first such surface, and this is the owner-decided read-only audit role.
    # It is the ledger scope ONLY — not `_ADMIN` — because approving actions and
    # writing settings are not auditing.
    "security_admin": _MEMBER | frozenset({SCOPE_TRUST_LEDGER_READ}),
}


def cloud_scopes_for(role: str) -> frozenset[str]:
    """Scopes for a cloud tenant role. An unknown role gets NONE (fail closed).

    Deliberately not `security.roles.scopes_for`. See the module header: the
    vocabularies collide on `primary_owner`, and reusing that function would
    grant a customer the deployment owner's scope set.
    """
    return CLOUD_ROLE_SCOPES.get(role, frozenset())


__all__ = ["CLOUD_ROLE_SCOPES", "CLOUD_TENANT_ROLES", "cloud_scopes_for"]
