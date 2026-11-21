"""Pairing codes and device tokens.

A watch is paired by a code the family app or dashboard shows once. The code
carries the tenant and workspace the device belongs to (so the pairing request
can be bound to the right rows under row-level security before anything is
looked up) and a random secret. Only the SHA-256 of the whole code is stored.

Once paired, the watch holds a device token of the same shape, again stored
only as a hash. A request carrying the token is bound to the token's tenant and
workspace, and the hash is then looked up inside that boundary: a token minted
for one family can never be matched against another family's devices, because
the connection it is checked on cannot see them.

Neither the code nor the token is ever written to a log or stored in clear.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

PAIRING_CODE_TTL = timedelta(minutes=15)
_PAIR_PREFIX = "cp1"
_TOKEN_PREFIX = "cd1"


@dataclass(frozen=True, slots=True)
class Credential:
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID
    secret: str
    kind: str  # "pairing" or "token"

    def encode(self) -> str:
        prefix = _PAIR_PREFIX if self.kind == "pairing" else _TOKEN_PREFIX
        scope = base64.urlsafe_b64encode(self.tenant_id.bytes + self.workspace_id.bytes).decode().rstrip("=")
        return f"{prefix}.{scope}.{self.secret}"


def digest(value: str) -> str:
    """The stored form of a code or token: hex SHA-256 of the whole string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_pairing_code(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> Credential:
    # 8 characters from a 32-symbol alphabet with no ambiguous glyphs: enough
    # entropy for a 15-minute window, short enough to type on a watch.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    secret = "".join(secrets.choice(alphabet) for _ in range(8))
    return Credential(tenant_id, workspace_id, secret, "pairing")


def new_device_token(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> Credential:
    return Credential(tenant_id, workspace_id, secrets.token_urlsafe(32), "token")


def parse(value: str) -> Optional[Credential]:
    """Decode a code or token into its scope and secret; None if malformed.

    Malformed means malformed: nothing here checks whether the secret is RIGHT.
    That is decided by comparing `digest(value)` with the stored hash inside the
    scope the credential names.
    """
    if not isinstance(value, str) or value.count(".") != 2 or len(value) > 200:
        return None
    prefix, scope, secret = value.split(".")
    if prefix == _PAIR_PREFIX:
        kind = "pairing"
    elif prefix == _TOKEN_PREFIX:
        kind = "token"
    else:
        return None
    if not secret or not scope:
        return None
    try:
        raw = base64.urlsafe_b64decode(scope + "=" * (-len(scope) % 4))
    except (ValueError, TypeError):
        return None
    if len(raw) != 32:
        return None
    return Credential(uuid.UUID(bytes=raw[:16]), uuid.UUID(bytes=raw[16:]), secret, kind)
