"""Envelope encryption for MFA secrets (P2-05).

    tenant KEK  (IRA_SECRET_KEY-derived, via core.connectors.secrets)
         ↓  wraps
    per-USER MFA DEK      user_mfa_keys.wrapped_dek
         ↓  encrypts
    the TOTP seed         totp_secrets.secret_ciphertext

**No new cryptography.** Every primitive here comes from
`core.connectors.secrets` — `wrap_dek`, `_unwrap_dek`, `seal_with`,
`use_secret_async`. This module supplies the key *hierarchy* and the AAD, not
the algorithms.

── Why per-user rather than the existing per-tenant DEK ────────────────────

`keyring.py` mints DEKs per `(tenant, purpose)` and `tenant_data_keys` carries a
tenant-only RLS policy. Sealing a second factor under that key would mean any
tenant-bound connection could reach the material that opens it — handing a
tenant administrator the decryption capability that `052`'s policies exist to
withhold. A tenant-scoped DEK *can* be safe when nothing tenant-scoped can reach
the unwrapping service; here the unwrapping service reads a table a tenant-bound
connection can read, so it is not.

── Why the DEK is a layer rather than sealing under the KEK directly ───────

Rotation. When the tenant root key rotates, `rewrap_user_mfa_key` re-wraps the
DEK and **no TOTP seed is decrypted or re-encrypted**. Sealing seeds directly
under a rotating key would mean holding every plaintext second factor in memory
during routine key maintenance, and an interruption halfway would leave rows
nobody can open. This is also the concrete defect P2-05 found in
`rotate_tenant`, which re-seals only `connector_accounts` and then destroys the
old key.

── The AAD ─────────────────────────────────────────────────────────────────

`mfa_aad` binds tenant, user and version. Cross-tenant and cross-user
decryption then fail in the cryptography, not only in RLS — so a
database-write attacker who can move rows still cannot read them, and the
boundary does not depend on a session variable having been set.
"""
from __future__ import annotations

import logging
import secrets as _stdlib_secrets
from typing import Any, Callable, Optional

from core.connectors.secrets import (
    KEY_BYTES,
    SealedSecret,
    SecretsError,
    WrappedKey,
    _unwrap_dek,
    dek_key_id,
    seal_with,
    use_secret_async,
    wrap_dek,
)

logger = logging.getLogger("ira.security.mfa_crypto")

#: Wrapping AAD for a user's MFA DEK. Every component is load-bearing, for the
#: reasons `wrap_aad` gives: drop the tenant or the user and a wrapped key can be
#: moved between them by anyone who can write to the table; drop the version and
#: it can be moved across a rotation.
def mfa_wrap_aad(tenant_id: Any, user_id: Any, version: int) -> str:
    return f"mfa-dek:{tenant_id}:{user_id}:v{int(version)}"


def mfa_aad(tenant_id: Any, user_id: Any, version: int) -> str:
    """AAD for the sealed seed itself.

    Distinct from the wrapping AAD so that a wrapped key and a sealed seed can
    never be confused for one another by a decryption routine.
    """
    return f"mfa-seed:{tenant_id}:{user_id}:v{int(version)}"


class MfaKeyUnavailable(Exception):
    """No usable MFA key for this principal — missing, destroyed, or unopenable.

    Deliberately distinct from "this user has no enrolment": a caller must not
    read "I cannot open the key" as "there is no second factor", which is the
    fail-open direction P2-05 exists to close.
    """


def _assert_principal(principal) -> None:
    if principal is None or getattr(principal, "tenant_id", None) is None \
            or getattr(principal, "user_id", None) is None:
        raise MfaKeyUnavailable(
            "MFA key material requires a principal naming both a tenant and a "
            "user — refusing to guess either.")


async def _live_key(conn, principal):
    """This user's one live key row, or None. Never a destroyed or retired one."""
    return await conn.fetchrow(
        "SELECT key_version, wrapped_dek, wrap_nonce, kek_id FROM user_mfa_keys "
        "WHERE tenant_id = $1 AND user_id = $2 "
        "AND retired_at IS NULL AND destroyed_at IS NULL",
        principal.tenant_id, principal.user_id)


def _open_key(row, principal, env) -> tuple[bytes, int]:
    version = int(row["key_version"])
    return _unwrap_dek(
        WrappedKey(material=bytes(row["wrapped_dek"]),
                   nonce=bytes(row["wrap_nonce"]),
                   kek_id=row["kek_id"]),
        aad=mfa_wrap_aad(principal.tenant_id, principal.user_id, version),
        env=env), version


async def active_mfa_dek(
    principal, *, env: Optional[dict] = None, mint: bool = True,
) -> tuple[bytes, int]:
    """This user's live MFA DEK and its version.

    Minted on first use rather than at signup, exactly as `active_dek` is: a
    user who never enrols never has a key, so there is nothing to rotate,
    destroy or explain.

    ── `mint=False`, and why opening a seed must never create a key ─────────

    `use_totp_secret` passes `mint=False`. Minting on the *read* path looks
    harmless and is not: after `destroy_mfa_keys`, a read would mint a brand
    new key, and the caller would then hold a perfectly valid DEK that opens
    nothing — turning "this second factor was destroyed" into "this second
    factor is mysteriously wrong". It also made the shred untestable, because
    the read path quietly undid it.

    ── The mint, and its two failure modes ─────────────────────────────────

    The version is `max(key_version) + 1` over ALL of this user's rows,
    destroyed ones included, because the primary key is
    `(tenant_id, user_id, key_version)` and a destroyed v1 still occupies it.
    Starting at 1 unconditionally meant a user who re-enrolled after an erasure
    collided with their own tombstone.

    The mint runs in its OWN transaction. `tenant_connection` opens one, and a
    failed INSERT inside it poisons every subsequent statement — so the
    "loser re-reads" recovery, written against the same connection, could only
    ever have raised `InFailedSQLTransactionError`. The race path is a fresh
    transaction, which is the only way it can read anything at all.
    """
    _assert_principal(principal)
    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal) as conn:
        row = await _live_key(conn, principal)
    if row is not None:
        return _open_key(row, principal, env)

    if not mint:
        raise MfaKeyUnavailable(
            "this user has no live MFA key — it was never minted, or it was "
            "destroyed. Refusing to mint one here: a key minted on a read path "
            "opens nothing that was sealed before it existed.")

    try:
        async with tenant_connection(principal) as conn:
            version = int(await conn.fetchval(
                "SELECT coalesce(max(key_version), 0) + 1 FROM user_mfa_keys "
                "WHERE tenant_id = $1 AND user_id = $2",
                principal.tenant_id, principal.user_id) or 1)
            dek = _stdlib_secrets.token_bytes(KEY_BYTES)
            wrapped = wrap_dek(
                dek,
                aad=mfa_wrap_aad(principal.tenant_id, principal.user_id, version),
                env=env)
            await conn.execute(
                "INSERT INTO user_mfa_keys "
                "(tenant_id,user_id,key_version,wrapped_dek,wrap_nonce,kek_id) "
                "VALUES ($1,$2,$3,$4,$5,$6)",
                principal.tenant_id, principal.user_id, version,
                wrapped.material, wrapped.nonce, wrapped.kek_id)
            return dek, version
    except Exception:
        # Lost the mint race, or the row appeared underneath us. Re-read in a
        # NEW transaction — the one above is aborted — because the winner's key
        # is the right one and failing here would break a concurrent enrolment.
        async with tenant_connection(principal) as conn:
            row = await _live_key(conn, principal)
        if row is None:
            raise MfaKeyUnavailable("could not mint or read an MFA key")
        return _open_key(row, principal, env)


async def seal_totp_secret(principal, secret: str, *, env: Optional[dict] = None):
    """Seal a TOTP seed under this user's MFA DEK.

    Returns `(ciphertext, nonce, key_version)` — exactly the three columns
    `totp_secrets` stores.
    """
    _assert_principal(principal)
    if not secret:
        raise SecretsError("refusing to seal an empty TOTP secret")
    dek, version = await active_mfa_dek(principal, env=env)
    try:
        sealed = seal_with(
            dek, secret,
            aad=mfa_aad(principal.tenant_id, principal.user_id, version),
            key_id=dek_key_id(version))
        return sealed.ciphertext, sealed.nonce, version
    finally:
        del dek


async def use_totp_secret(
    principal, ciphertext: bytes, nonce: bytes, version: int,
    fn: Callable[[str], Any], *, env: Optional[dict] = None,
):
    """Open a sealed seed and hand the plaintext to `fn`, and only to `fn`.

    The plaintext exists for the duration of the callback and is dropped
    afterwards — `use_secret_async`'s contract. Nothing here returns it, so a
    caller cannot accidentally put it in a response body or a log line.
    """
    _assert_principal(principal)
    # `mint=False`: opening a seed must never create key material. See
    # `active_mfa_dek` — a mint here would silently undo a crypto-shred.
    dek, live_version = await active_mfa_dek(principal, env=env, mint=False)
    try:
        if int(version) != int(live_version):
            # The seed was sealed under a version this user no longer has live.
            # Re-wrapping preserves the DEK across rotation, so this means the
            # key was rotated by minting a NEW dek — recoverable only by
            # re-enrolment, and saying so beats a confusing decryption error.
            raise MfaKeyUnavailable(
                f"the seed was sealed under MFA key version {version} and the "
                f"live version is {live_version}")
        sealed = SealedSecret(ciphertext=bytes(ciphertext),
                              key_id=dek_key_id(int(version)),
                              nonce=bytes(nonce))
        return await use_secret_async(
            sealed, fn,
            aad=mfa_aad(principal.tenant_id, principal.user_id, int(version)),
            key=dek)
    except MfaKeyUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        # Never log the exception body: a decryption failure can carry
        # ciphertext, and an AAD mismatch carries the identifiers.
        logger.warning("MFA seed could not be opened: %s", type(exc).__name__)
        raise MfaKeyUnavailable("the MFA secret could not be decrypted") from exc
    finally:
        del dek


async def rewrap_user_mfa_key(
    principal, *, old_env: Optional[dict] = None, new_env: Optional[dict] = None,
) -> bool:
    """Re-wrap this user's MFA DEK under a new KEK. **No seed is touched.**

    The operation tenant-root rotation needs. The DEK and its version are
    unchanged, so every sealed seed stays readable and nothing has to be
    decrypted — which is the whole reason the DEK layer exists.

    Two envs rather than one, because `_unwrap_dek` REFUSES outright when the
    row's `kek_id` is not the configured one. That refusal is correct (it turns
    a misconfigured key into a clear message instead of a forged-ciphertext
    alarm) but it means a single-env re-wrap can only ever re-wrap under the
    same KEK — i.e. exactly not the case rotation exists for. `old_env` names
    the KEK the row is wrapped under now; `new_env` names the one it should be
    wrapped under next. Both default to the process environment, which makes
    the no-rotation call a nonce refresh.

    Returns False when the user has no live key, which is not an error: a user
    who never enrolled has nothing to re-wrap.
    """
    _assert_principal(principal)
    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal) as conn:
        row = await conn.fetchrow(
            "SELECT key_version, wrapped_dek, wrap_nonce, kek_id FROM user_mfa_keys "
            "WHERE tenant_id = $1 AND user_id = $2 "
            "AND retired_at IS NULL AND destroyed_at IS NULL",
            principal.tenant_id, principal.user_id)
        if row is None:
            return False

        version = int(row["key_version"])
        aad = mfa_wrap_aad(principal.tenant_id, principal.user_id, version)
        dek = _unwrap_dek(
            WrappedKey(material=bytes(row["wrapped_dek"]),
                       nonce=bytes(row["wrap_nonce"]),
                       kek_id=row["kek_id"]),
            aad=aad, env=old_env)
        try:
            rewrapped = wrap_dek(dek, aad=aad, env=new_env)
        finally:
            del dek
        # The AAD is unchanged because the version is unchanged: re-wrapping is
        # not a new key version. A row that gained a version here would strand
        # every seed sealed under the old one.
        await conn.execute(
            "UPDATE user_mfa_keys SET wrapped_dek=$3, wrap_nonce=$4, kek_id=$5 "
            "WHERE tenant_id=$1 AND user_id=$2 AND key_version=$6",
            principal.tenant_id, principal.user_id,
            rewrapped.material, rewrapped.nonce, rewrapped.kek_id, version)
        return True


async def rewrap_tenant_mfa_keys(
    principal, *, old_env: Optional[dict] = None, new_env: Optional[dict] = None,
) -> tuple[int, list[str]]:
    """Re-wrap every member's MFA DEK in this tenant. Returns `(count, failures)`.

    Called by `rotate_tenant`. Members are enumerated from `tenant_memberships`
    — which a tenant-bound connection may read — and each user's key is then
    re-wrapped on a connection bound to *that* user, because `user_mfa_keys`
    policies require `app.user_id` to match and there is deliberately no
    tenant-wide read.

    ── On the impersonation this performs ──────────────────────────────────

    Binding `app.user_id` to each member in turn is a real capability, so it is
    worth being precise about why it is not the thing `052` and this table
    exist to prevent. The capability that must never follow from tenant
    administration is *decrypting a member's second factor*. Re-wrapping
    decrypts a DEK, never a seed, and returns a count — no key material and no
    plaintext crosses back to the caller. And the ability to unwrap at all
    comes from holding the KEK, which is a deployment secret in the process
    environment, not from any tenant role. A tenant administrator who calls
    rotation still cannot read one second factor.

    Failures are collected rather than raised. One member with an unopenable
    key must not abort a rotation for everybody else, and the rotation result
    has to be able to say which users still need attention.
    """
    _assert_principal(principal)
    import dataclasses

    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal) as conn:
        members = await conn.fetch(
            "SELECT user_id FROM tenant_memberships WHERE tenant_id = $1",
            principal.tenant_id)

    rewrapped = 0
    failures: list[str] = []
    for m in members:
        member = dataclasses.replace(principal, user_id=m["user_id"])
        try:
            if await rewrap_user_mfa_key(
                    member, old_env=old_env, new_env=new_env):
                rewrapped += 1
        except Exception as exc:  # noqa: BLE001 — never log key material
            logger.error("MFA key re-wrap failed for user %s: %s",
                         m["user_id"], type(exc).__name__)
            failures.append(str(m["user_id"]))
    return rewrapped, failures


async def destroy_mfa_keys(principal, *, reason: str = "account erasure") -> int:
    """Crypto-shred this user's MFA key material.

    The row survives with `destroyed_at` set and the material NULLed, so "a key
    existed and was destroyed, when and why" stays answerable. A deleted row
    cannot say that — and for a credential destruction, being able to say it is
    the point.

    Called by account erasure. Erasure also deletes the ciphertext, so this is
    belt and braces; but the shred is what makes any copy of the ciphertext
    that escaped a backup unreadable.
    """
    _assert_principal(principal)
    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal) as conn:
        result = await conn.execute(
            "UPDATE user_mfa_keys SET wrapped_dek=NULL, wrap_nonce=NULL, "
            "destroyed_at=NOW(), destruction_reason=$3 "
            "WHERE tenant_id=$1 AND user_id=$2 AND destroyed_at IS NULL",
            principal.tenant_id, principal.user_id, reason)
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except (ValueError, IndexError):  # pragma: no cover - defensive
        return 0


__all__ = [
    "MfaKeyUnavailable",
    "active_mfa_dek",
    "seal_totp_secret",
    "use_totp_secret",
    "rewrap_user_mfa_key",
    "rewrap_tenant_mfa_keys",
    "destroy_mfa_keys",
    "mfa_aad",
    "mfa_wrap_aad",
]
