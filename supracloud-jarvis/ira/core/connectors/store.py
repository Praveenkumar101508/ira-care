"""Tenant-scoped persistence for connectors, OAuth flows and receipts.

Every statement runs inside `tenant_connection`, so Row-Level Security binds
`app.tenant_id` to the authenticated Principal — the same arrangement as
`core/privacy/store.py` and `core/realtime/store.py`.

Two things here are deliberately NOT fail-soft, unlike the realtime telemetry
writes:

**Storing a credential.** If sealing or writing the credential fails, the
connect must fail. Reporting success would leave the user believing IRA has
access it does not have, and leave a live grant at Google that IRA cannot see or
revoke.

**Consuming an OAuth state.** If the single-use marking fails, the callback must
fail. Proceeding would mean a state that could be replayed.

Receipt writes ARE fail-soft, and `ledger_recorded` carries the honesty: a
receipt that was not written is reported as not written rather than assumed.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from core.connectors.gateway import ConnectedAccount
from core.connectors.oauth import PendingAuthorisation, StoredState
from core.connectors.secrets import SealedSecret
from core.identity.db_context import tenant_connection
from core.identity.principal import Principal
from core.interfaces.connector_gateway import AccessLevel, ActionReceipt

logger = logging.getLogger("ira.connectors.store")


class ConnectorStoreError(RuntimeError):
    """A persistence failure that must NOT be swallowed."""


# ── The scope catalogue ──────────────────────────────────────────────────────


async def offered_scopes(
    principal: Principal, connector: str, *, edition: str = "cloud"
) -> list[dict[str, Any]]:
    """Scopes this build offers for a connector, in this edition.

    Read from the table rather than from a Python constant so the consent screen
    and the database cannot disagree about what is being asked for — the screen
    is the only place the user learns what they are granting.
    """
    column = "offered_in_cloud" if edition == "cloud" else "offered_in_private"
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            f"SELECT scope, human_summary, access_kind FROM connector_scopes "
            f"WHERE connector = $1 AND {column} IS TRUE ORDER BY access_kind, scope",
            connector,
        )
    return [
        {
            "scope": r["scope"],
            "summary": r["human_summary"],
            "access_kind": r["access_kind"],
        }
        for r in rows
    ]


# ── Accounts ─────────────────────────────────────────────────────────────────


def _row_to_account(row: Any) -> ConnectedAccount:
    sealed = None
    if row["credential_ciphertext"] is not None:
        sealed = SealedSecret(
            ciphertext=bytes(row["credential_ciphertext"]),
            key_id=row["encryption_key_id"] or "",
            nonce=bytes(row["credential_nonce"] or b""),
        )
    return ConnectedAccount(
        account_id=str(row["id"]),
        connector=row["connector"],
        provider_account=row["provider_account"] or "",
        granted_scopes=tuple(row["granted_scopes"] or ()),
        access_level=row["access_level"],
        status=row["status"],
        sealed=sealed,
    )


_ACCOUNT_COLUMNS = (
    "id, connector, provider_account, granted_scopes, access_level, status, "
    "credential_ciphertext, encryption_key_id, credential_nonce, "
    "connected_at, last_used_at, expires_at, revoked_at"
)


async def account_for(
    principal: Principal, connector: str
) -> Optional[ConnectedAccount]:
    """The caller's live account for a connector, or None.

    None is a refusal the gateway turns into "connect Gmail first", not an
    error. Revoked rows are excluded here so a disconnected account cannot be
    used by a code path that forgot to check `status`.
    """
    async with tenant_connection(principal) as conn:
        row = await conn.fetchrow(
            f"SELECT {_ACCOUNT_COLUMNS} FROM connector_accounts "
            f"WHERE tenant_id = $1 AND user_id = $2 AND connector = $3 "
            f"AND revoked_at IS NULL ORDER BY connected_at DESC LIMIT 1",
            principal.tenant_id, principal.user_id, connector,
        )
    return _row_to_account(row) if row else None


async def list_accounts(principal: Principal) -> list[dict[str, Any]]:
    """Every account, for the settings screen. Never returns a credential —
    `credential_present` is a boolean, so the surface can say "connected"
    without the ciphertext travelling anywhere near a response body."""
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT id, connector, provider_account, granted_scopes, "
            "access_level, status, connected_at, last_used_at, expires_at, "
            "revoked_at, (credential_ciphertext IS NOT NULL) AS credential_present "
            "FROM connector_accounts WHERE tenant_id = $1 AND user_id = $2 "
            "ORDER BY connector",
            principal.tenant_id, principal.user_id,
        )
    return [
        {
            "account_id": str(r["id"]),
            "connector": r["connector"],
            "provider_account": r["provider_account"] or "",
            "granted_scopes": list(r["granted_scopes"] or ()),
            "access_level": r["access_level"],
            "status": r["status"],
            "credential_present": r["credential_present"],
            "connected_at": r["connected_at"].isoformat() if r["connected_at"] else None,
            "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
            "revoked_at": r["revoked_at"].isoformat() if r["revoked_at"] else None,
        }
        for r in rows
    ]


async def upsert_account(
    principal: Principal,
    *,
    connector: str,
    provider_account: str,
    granted_scopes: Sequence[str],
    sealed: SealedSecret,
    access_level: str = AccessLevel.OBSERVE.value,
    expires_at: Optional[datetime] = None,
) -> uuid.UUID:
    """Store a freshly granted credential.

    NOT fail-soft. If this raises, the connect must fail: reporting success
    would leave the user believing IRA has access it does not have, and leave a
    live grant at Google that IRA can neither see nor revoke.
    """
    try:
        async with tenant_connection(principal) as conn:
            return await conn.fetchval(
                """
                INSERT INTO connector_accounts
                    (tenant_id, user_id, connector, provider_account,
                     granted_scopes, access_level, status,
                     credential_ciphertext, encryption_key_id, credential_nonce,
                     expires_at)
                VALUES ($1,$2,$3,$4,$5,$6,'connected',$7,$8,$9,$10)
                ON CONFLICT (tenant_id, user_id, connector, provider_account)
                DO UPDATE SET
                    granted_scopes        = EXCLUDED.granted_scopes,
                    access_level          = EXCLUDED.access_level,
                    status                = 'connected',
                    credential_ciphertext = EXCLUDED.credential_ciphertext,
                    encryption_key_id     = EXCLUDED.encryption_key_id,
                    credential_nonce      = EXCLUDED.credential_nonce,
                    expires_at            = EXCLUDED.expires_at,
                    revoked_at            = NULL,
                    updated_at            = NOW()
                RETURNING id
                """,
                principal.tenant_id, principal.user_id, connector,
                provider_account, list(granted_scopes), access_level,
                sealed.ciphertext, sealed.key_id, sealed.nonce, expires_at,
            )
    except Exception as exc:  # noqa: BLE001 - deliberately NOT swallowed
        raise ConnectorStoreError(
            f"could not store the {connector} credential, so the connection "
            f"failed: {exc}. The grant may still be live at the provider — "
            f"check https://myaccount.google.com/permissions."
        ) from exc


async def set_access_level(
    principal: Principal, connector: str, level: str
) -> bool:
    async with tenant_connection(principal) as conn:
        result = await conn.execute(
            "UPDATE connector_accounts SET access_level = $4, updated_at = NOW() "
            "WHERE tenant_id = $1 AND user_id = $2 AND connector = $3 "
            "AND revoked_at IS NULL",
            principal.tenant_id, principal.user_id, connector, level,
        )
    return str(result).endswith("1")


async def mark_used(principal: Principal, connector: str) -> None:
    """Fail-soft: a missing `last_used_at` must not fail a successful read."""
    try:
        async with tenant_connection(principal) as conn:
            await conn.execute(
                "UPDATE connector_accounts SET last_used_at = NOW() "
                "WHERE tenant_id = $1 AND user_id = $2 AND connector = $3",
                principal.tenant_id, principal.user_id, connector,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not stamp last_used_at for %s: %s", connector, exc)


async def take_credential_for_revocation(
    principal: Principal, connector: str
) -> Optional[SealedSecret]:
    """Fetch the sealed credential so the caller can revoke it at the provider.

    Named for what it is for. Disconnect must revoke AND delete: deleting IRA's
    copy alone leaves the grant live at Google while telling the user they are
    disconnected, which is the most misleading possible outcome of a button
    labelled "Disconnect".
    """
    account = await account_for(principal, connector)
    return account.sealed if account else None


async def disconnect(principal: Principal, connector: str) -> bool:
    """Clear the credential and mark the account revoked.

    The schema refuses a revoked row that still holds a credential
    (`revoked_accounts_hold_no_credential`), so this cannot half-succeed into a
    state where the account looks disconnected and the token is still there.

    Revocation at the PROVIDER is the caller's job, using
    `take_credential_for_revocation` before calling this. That split is
    deliberate: this function cannot make a network call, and pretending it
    revokes remotely would be the lie described above.
    """
    async with tenant_connection(principal) as conn:
        result = await conn.execute(
            "UPDATE connector_accounts SET credential_ciphertext = NULL, "
            "encryption_key_id = NULL, credential_nonce = NULL, "
            "status = 'revoked', revoked_at = NOW(), updated_at = NOW() "
            "WHERE tenant_id = $1 AND user_id = $2 AND connector = $3 "
            "AND revoked_at IS NULL",
            principal.tenant_id, principal.user_id, connector,
        )
    return str(result).endswith("1")


# ── OAuth states ─────────────────────────────────────────────────────────────


async def save_state(principal: Principal, pending: PendingAuthorisation) -> None:
    """Persist an in-flight authorisation. NOT fail-soft.

    A flow whose state was not stored will fail at the callback with "unknown
    state", which is correct but confusing. Failing here instead means the user
    never leaves for the consent screen.
    """
    try:
        async with tenant_connection(principal) as conn:
            await conn.execute(
                """
                INSERT INTO connector_oauth_states
                    (state, tenant_id, user_id, connector, requested_scopes,
                     verifier_ciphertext, encryption_key_id, verifier_nonce,
                     code_challenge, expires_at, return_to)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                """,
                pending.state, principal.tenant_id, principal.user_id,
                pending.connector, list(pending.scopes),
                pending.sealed_verifier.ciphertext,
                pending.sealed_verifier.key_id,
                pending.sealed_verifier.nonce,
                pending.code_challenge, pending.expires_at, pending.return_to,
            )
    except Exception as exc:  # noqa: BLE001 - deliberately NOT swallowed
        raise ConnectorStoreError(
            f"could not start the authorisation: {exc}"
        ) from exc


async def load_state(principal: Principal, state: str) -> Optional[StoredState]:
    """The stored state, or None. RLS scopes it to the caller's tenant, so a
    state belonging to another tenant is indistinguishable from one that does
    not exist — which is the answer that leaks nothing."""
    async with tenant_connection(principal) as conn:
        row = await conn.fetchrow(
            "SELECT state, tenant_id, user_id, connector, requested_scopes, "
            "code_challenge, expires_at, consumed_at, verifier_ciphertext, "
            "encryption_key_id, verifier_nonce, return_to "
            "FROM connector_oauth_states WHERE state = $1",
            state,
        )
    if row is None:
        return None
    return StoredState(
        state=row["state"],
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        connector=row["connector"],
        requested_scopes=tuple(row["requested_scopes"] or ()),
        code_challenge=row["code_challenge"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
    )


async def sealed_verifier_for(
    principal: Principal, state: str
) -> Optional[SealedSecret]:
    async with tenant_connection(principal) as conn:
        row = await conn.fetchrow(
            "SELECT verifier_ciphertext, encryption_key_id, verifier_nonce "
            "FROM connector_oauth_states WHERE state = $1",
            state,
        )
    if row is None:
        return None
    return SealedSecret(
        ciphertext=bytes(row["verifier_ciphertext"]),
        key_id=row["encryption_key_id"],
        nonce=bytes(row["verifier_nonce"]),
    )


async def consume_state(principal: Principal, state: str) -> None:
    """Mark a state used. NOT fail-soft.

    The trigger in `028_connectors.sql` refuses a second consumption, so a
    replay fails here even if the handler's own check were wrong or raced —
    two callbacks arriving together is the ordinary case for a double-clicked
    consent screen. If this raises, the callback must fail: proceeding would
    mean a state that could be replayed.
    """
    try:
        async with tenant_connection(principal) as conn:
            result = await conn.execute(
                "UPDATE connector_oauth_states SET consumed_at = NOW() "
                "WHERE state = $1",
                state,
            )
        if not str(result).endswith("1"):
            raise ConnectorStoreError(
                "the OAuth state could not be marked used, so the callback is "
                "refused rather than risking a replayable state"
            )
    except ConnectorStoreError:
        raise
    except Exception as exc:  # noqa: BLE001 - deliberately NOT swallowed
        raise ConnectorStoreError(
            f"the OAuth state could not be marked used and the callback is "
            f"refused: {exc}"
        ) from exc


async def purge_expired_states(principal: Principal) -> int:
    """Delete states that expired without being used.

    An expired state is already refused by `verify_callback`, so this is
    housekeeping rather than a control — said plainly so nobody mistakes it for
    the expiry enforcement.
    """
    async with tenant_connection(principal) as conn:
        result = await conn.execute(
            "DELETE FROM connector_oauth_states WHERE tenant_id = $1 "
            "AND consumed_at IS NULL AND expires_at < NOW()",
            principal.tenant_id,
        )
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except ValueError:  # pragma: no cover
        return 0


# ── Receipts ─────────────────────────────────────────────────────────────────


async def write_receipt(
    principal: Principal,
    receipt: ActionReceipt,
    outcome: str = "ok",
    reason: str = "",
    *,
    influences: Optional[Any] = None,
) -> Optional[uuid.UUID]:
    """Persist a receipt. Fail-soft, and honest about it.

    Fail-soft because a connector READ that already happened must not be
    reported as failed by a receipt write that did. The honesty cost is carried
    by `ledger_recorded`, which stays False when nothing was written — so the
    surface can say the audit entry is missing rather than claim one that does
    not exist.
    """
    from core.connectors.taint import taint_findings

    tainted = bool(influences and influences.is_tainted)
    sources = list(influences.describe()) if tainted else []
    findings = list(taint_findings(influences)) if influences else []

    try:
        async with tenant_connection(principal) as conn:
            return await conn.fetchval(
                """
                INSERT INTO action_receipts
                    (tenant_id, user_id, connector, operation, purpose,
                     performed_at, outcome, data_accessed, sent_externally,
                     model_provider, approved_by_user, undo_available,
                     ledger_recorded, audit_event_id,
                     influenced_by_external, influence_sources,
                     injection_findings)
                VALUES ($1,$2,$3,$4,$5,COALESCE($6::timestamptz, NOW()),$7,$8,$9,
                        $10,$11,$12,$13,$14,$15,$16,$17)
                RETURNING id
                """,
                principal.tenant_id, principal.user_id, receipt.connector,
                receipt.operation,
                receipt.purpose if outcome != "denied" else (receipt.purpose or reason),
                receipt.performed_at or None, outcome,
                list(receipt.data_accessed), list(receipt.sent_externally),
                receipt.model_provider, receipt.approved_by_user,
                receipt.undo_available,
                bool(receipt.audit_event_id), receipt.audit_event_id,
                tainted, sources, findings,
            )
    except Exception as exc:  # noqa: BLE001 - fail-soft, logged
        logger.warning("could not persist an action receipt: %s", exc)
        return None


async def recent_receipts(
    principal: Principal, *, limit: int = 50
) -> list[dict[str, Any]]:
    """What IRA did, most recent first — including what it refused to do."""
    capped = max(1, min(int(limit), 200))
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT id, connector, operation, purpose, performed_at, outcome, "
            "data_accessed, sent_externally, approved_by_user, undo_available, "
            "ledger_recorded, influenced_by_external, influence_sources, "
            "injection_findings, redacted_at "
            "FROM action_receipts WHERE tenant_id = $1 "
            "ORDER BY performed_at DESC LIMIT $2",
            principal.tenant_id, capped,
        )
    return [
        {
            "receipt_id": str(r["id"]),
            "connector": r["connector"],
            "operation": r["operation"],
            "purpose": r["purpose"],
            "performed_at": r["performed_at"].isoformat() if r["performed_at"] else None,
            "outcome": r["outcome"],
            "data_accessed": list(r["data_accessed"] or ()),
            "sent_externally": list(r["sent_externally"] or ()),
            "approved_by_user": r["approved_by_user"],
            "undo_available": r["undo_available"],
            # Reported, not assumed: False means the audit entry is missing.
            "ledger_recorded": r["ledger_recorded"],
            "influenced_by_external": r["influenced_by_external"],
            "influence_sources": list(r["influence_sources"] or ()),
            "injection_findings": list(r["injection_findings"] or ()),
            "redacted": r["redacted_at"] is not None,
        }
        for r in rows
    ]


async def redact_receipts(principal: Principal) -> int:
    """Blank the descriptive columns on every receipt, keeping the facts.

    The same line ADR-0004 drew for `permission_decisions`: what survives is
    that an action of a given kind happened, when, and through which connector.
    What goes is what it was about.
    """
    async with tenant_connection(principal) as conn:
        result = await conn.execute(
            "UPDATE action_receipts SET redacted_at = NOW(), purpose = '', "
            "data_accessed = '{}', sent_externally = '{}', "
            "influence_sources = '{}' "
            "WHERE tenant_id = $1 AND redacted_at IS NULL",
            principal.tenant_id,
        )
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except ValueError:  # pragma: no cover
        return 0


__all__ = [
    "ConnectorStoreError",
    "account_for",
    "consume_state",
    "disconnect",
    "list_accounts",
    "load_state",
    "mark_used",
    "offered_scopes",
    "purge_expired_states",
    "recent_receipts",
    "redact_receipts",
    "save_state",
    "sealed_verifier_for",
    "set_access_level",
    "take_credential_for_revocation",
    "upsert_account",
    "write_receipt",
]
