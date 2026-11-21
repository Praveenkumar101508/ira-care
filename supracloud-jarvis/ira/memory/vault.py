"""Who may recall Memory Vault rows in chat.

The marker the rows carry is `memory.namespaces.VAULT_NAMESPACE`. The vault
route (`api/routes/memory.py`) writes it, chat retrieval (`memory/store.py`)
reads it, and `security/delegated.py` reserves it as an account name, so all
three take it from that one registry rather than spelling it here a second time.

Kept dependency-free: importing `memory.store` from the route would pull in the
embedding model at import time.
"""
from __future__ import annotations

from core.editions import EDITION_CLOUD


def may_recall_vault(principal) -> bool:
    """Whether chat retrieval for `principal` should include vault rows.

    The vault is owner-private: `private_memory_read` belongs to
    `primary_owner` alone, and the scope enforcer refuses `/api/v1/memory` to
    every delegated role. Retrieval follows the same rule.

    Owner-ness is read from the verified principal, never from a username, so a
    delegated account cannot gain the vault by what it is called. A cloud
    principal never qualifies: the cloud resolver sets `is_owner=False`, and a
    tenant owner is not the deployment owner (3C-04). Checking the edition here
    as well means a mis-built cloud principal fails closed.
    """
    if getattr(principal, "is_owner", False) is not True:
        return False
    return getattr(principal, "edition", EDITION_CLOUD) != EDITION_CLOUD


__all__ = ["may_recall_vault"]
