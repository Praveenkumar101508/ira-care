"""security/roles.py — role and permission-scope definitions (PR #67).

Single source of truth for who may do what. Two hard rules are encoded here,
not left to callers:

  1. primary_owner is ONLY the original owner (the .env admin account). It is
     never grantable, and no other role includes security_admin — so nobody
     can remove or replace the primary owner or weaken their security.
  2. Remembering a person grants nothing: relationship records default to
     ACCESS_NONE, and turning a person into a user goes through the delegated
     access wizard (owner password + confirmation), never through speech.

Destructive/outbound actions remain confirmation-gated for every role,
including primary_owner — roles gate what you may ASK for, the approval
guardrail gates what actually EXECUTES.
"""
from __future__ import annotations

# ── Roles ─────────────────────────────────────────────────────────────────────

ROLE_VIEWER = "viewer"
ROLE_TRUSTED_USER = "trusted_user"
ROLE_FAMILY_ADMIN = "family_admin"
ROLE_OWNER_EQUIVALENT = "owner_equivalent"
ROLE_PRIMARY_OWNER = "primary_owner"

ROLES = (
    ROLE_VIEWER,
    ROLE_TRUSTED_USER,
    ROLE_FAMILY_ADMIN,
    ROLE_OWNER_EQUIVALENT,
    ROLE_PRIMARY_OWNER,
)

# Roles the owner may grant to someone else. primary_owner is deliberately
# absent — there is exactly one, forever.
GRANTABLE_ROLES = (
    ROLE_VIEWER,
    ROLE_TRUSTED_USER,
    ROLE_FAMILY_ADMIN,
    ROLE_OWNER_EQUIVALENT,
)

# ── Permission scopes ────────────────────────────────────────────────────────

SCOPE_CHAT = "chat"
SCOPE_VOICE = "voice"
SCOPE_SHARED_MEMORY_READ = "shared_memory_read"
SCOPE_SHARED_MEMORY_WRITE = "shared_memory_write"
SCOPE_PRIVATE_MEMORY_READ = "private_memory_read"
SCOPE_ACTIONS_REQUEST = "actions_request"
SCOPE_ACTIONS_APPROVE = "actions_approve"
SCOPE_SETTINGS_READ = "settings_read"
SCOPE_SETTINGS_WRITE = "settings_write"
SCOPE_SECURITY_ADMIN = "security_admin"

#: Read the tenant's Trust Ledger — the hash-chained record of every governed
#: action. Added in P10-02 on the owner's decision that a tenant administrator
#: may inspect their OWN tenant's ledger, because a cloud customer with an audit
#: trail they cannot read has an audit trail in name only.
#:
#: A separate scope rather than a reuse of `settings_read`: the ledger names
#: what was done, to what, and by whom, and bundling it with "may see the
#: settings page" would widen an existing role by accident. Deliberately NOT
#: granted to an ordinary member — an audit ledger is tenant-wide, and every
#: member reading every colleague's actions is a different product.
SCOPE_TRUST_LEDGER_READ = "trust_ledger_read"

#: Read the tenant's commercial position — which plan, what it entitles, what
#: has been invoiced. Added in P11-05, and it is what `cloud_roles.py` meant
#: when it said `billing_admin`'s "elevated capabilities arrive with the
#: surfaces they administer". This is that surface.
#:
#: A separate scope rather than a reuse of `settings_read`, for the same reason
#: the ledger got its own: an invoice says what an organisation is paying and
#: how much it has used, and folding that into "may see the settings page"
#: would widen several existing roles by accident.
#:
#: Deliberately NOT granted to an ordinary member or viewer. What the company
#: spends is not everyone's business, and a product where it is should decide
#: that on purpose rather than inherit it from a scope named something else.
SCOPE_BILLING_READ = "billing_read"

#: CHANGE the tenant's commercial position — start a subscription, open the
#: provider's billing portal, cancel.
#:
#: Separate from `SCOPE_BILLING_READ` because reading what a company owes and
#: committing it to a recurring charge are different authorities, and the second
#: is the one that spends money. A finance viewer who may see the invoice should
#: not thereby be able to cancel the plan the whole tenant runs on, and an
#: engineer with `admin` should not be able to start a paid subscription because
#: their role happened to include the read scope.
#:
#: Deliberately NOT part of `_ADMIN` in `cloud_roles.py`. It is held by
#: `primary_owner`, `owner` and `billing_admin` — the roles whose job IS the
#: commercial relationship — and by nobody else.
SCOPE_BILLING_MANAGE = "billing_manage"

SCOPES = (
    SCOPE_CHAT,
    SCOPE_VOICE,
    SCOPE_SHARED_MEMORY_READ,
    SCOPE_SHARED_MEMORY_WRITE,
    SCOPE_PRIVATE_MEMORY_READ,
    SCOPE_ACTIONS_REQUEST,
    SCOPE_ACTIONS_APPROVE,
    SCOPE_SETTINGS_READ,
    SCOPE_SETTINGS_WRITE,
    SCOPE_SECURITY_ADMIN,
    SCOPE_TRUST_LEDGER_READ,
    SCOPE_BILLING_READ,
    SCOPE_BILLING_MANAGE,
)

# What each role may ask for. Note the two invariants:
#   - only primary_owner has security_admin (protects the owner's account),
#   - only primary_owner has private_memory_read (owner-private memories stay private).
ROLE_SCOPES: dict[str, frozenset[str]] = {
    ROLE_VIEWER: frozenset({SCOPE_CHAT}),
    ROLE_TRUSTED_USER: frozenset({SCOPE_CHAT, SCOPE_VOICE, SCOPE_SHARED_MEMORY_READ}),
    ROLE_FAMILY_ADMIN: frozenset({
        SCOPE_CHAT, SCOPE_VOICE,
        SCOPE_SHARED_MEMORY_READ, SCOPE_SHARED_MEMORY_WRITE,
        SCOPE_ACTIONS_REQUEST, SCOPE_SETTINGS_READ,
    }),
    ROLE_OWNER_EQUIVALENT: frozenset({
        SCOPE_CHAT, SCOPE_VOICE,
        SCOPE_SHARED_MEMORY_READ, SCOPE_SHARED_MEMORY_WRITE,
        SCOPE_ACTIONS_REQUEST, SCOPE_ACTIONS_APPROVE,
        SCOPE_SETTINGS_READ, SCOPE_SETTINGS_WRITE,
    }),
    ROLE_PRIMARY_OWNER: frozenset(SCOPES),
}

# ── People access levels (relationship records) ─────────────────────────────

ACCESS_NONE = "no_access"
# A person's access_level is either no_access or the role of the delegated
# account created for them through the wizard.
ACCESS_LEVELS = (ACCESS_NONE,) + GRANTABLE_ROLES


def scopes_for(role: str) -> frozenset[str]:
    """Scopes for a role; unknown roles get NO scopes (fail closed)."""
    return ROLE_SCOPES.get(role, frozenset())


def role_can(role: str, scope: str) -> bool:
    return scope in scopes_for(role)


def can_modify_user(actor_is_primary_owner: bool, target_is_primary_owner: bool) -> bool:
    """May the actor change/remove the target account?

    Nobody — not even owner_equivalent — may touch the primary owner. The
    primary owner may manage everyone else. (The API additionally restricts
    all account management to the primary owner.)
    """
    if target_is_primary_owner:
        return False
    return actor_is_primary_owner


__all__ = [
    "ROLES", "GRANTABLE_ROLES", "SCOPES", "ROLE_SCOPES", "ACCESS_LEVELS", "ACCESS_NONE",
    "ROLE_VIEWER", "ROLE_TRUSTED_USER", "ROLE_FAMILY_ADMIN", "ROLE_OWNER_EQUIVALENT",
    "ROLE_PRIMARY_OWNER",
    "SCOPE_CHAT", "SCOPE_VOICE", "SCOPE_SHARED_MEMORY_READ", "SCOPE_SHARED_MEMORY_WRITE",
    "SCOPE_PRIVATE_MEMORY_READ", "SCOPE_ACTIONS_REQUEST", "SCOPE_ACTIONS_APPROVE",
    "SCOPE_SETTINGS_READ", "SCOPE_SETTINGS_WRITE", "SCOPE_SECURITY_ADMIN",
    "SCOPE_TRUST_LEDGER_READ", "SCOPE_BILLING_READ", "SCOPE_BILLING_MANAGE",
    "scopes_for", "role_can", "can_modify_user",
]
