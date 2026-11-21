"""Per-tenant data-encryption keys: minting, resolving, rotating, destroying.

Blocker **B3**, in the words of `RELEASE_READINESS.md` R11:

    there is no KMS and no crypto-shredding. The key is an environment
    variable, `rewrap` exists with no caller, so rotating the key re-encrypts
    nothing, and a leaked key compromises every stored credential at once.

`secrets.py` holds the cryptography and does no I/O. This module holds the
lifecycle and does nothing but I/O and ordering. The split matters: every
statement here runs inside `tenant_connection`, so RLS scopes the key rows to
the authenticated Principal exactly as it scopes everything else, and the crypto
below stays testable without a database.

── The one-sentence version ─────────────────────────────────────────────────

`IRA_CONNECTOR_ENCRYPTION_KEY` stops encrypting credentials and starts
encrypting *keys*; credentials are sealed under a key belonging to one tenant
and one version, which can be rotated, and which can be destroyed.

── Destroying a key is the point ────────────────────────────────────────────

`destroy_keys` sets `wrapped_dek` to NULL. Afterwards nothing in the deployment
can open a credential sealed under that version — not a restored replica, not an
unvacuumed page, not a row a DELETE missed. The schema makes it irreversible
(`tenant_data_keys_shred_only` refuses resurrection), and the row survives as a
tombstone carrying the reason, so "your Gmail stopped working" has an answer.

The bound, stated wherever this is described: **a backup taken before the
destruction still contains the wrapped DEK.** Shredding removes the key from the
present, not from the past. That limitation is real, it is not fixed here, and
`docs/operations/DISASTER_RECOVERY.md` carries what an operator must do about it.

── Fail closed, and the four ways this refuses ──────────────────────────────

The brief lists "missing encryption key" among the conditions that must fail
closed. Four distinct refusals, each with its own message because each needs a
different action from whoever reads it:

    no KEK configured       MissingEncryptionKey — set the variable
    key version destroyed   KeyDestroyed — the credential is gone; reconnect
    key version unknown     KeyNotFound — the row refers to a key that never
                            existed here, which usually means a partial restore
    no tenant context       RLS returns nothing, which surfaces as KeyNotFound

None of them falls back to the KEK, mints a replacement key, or re-seals under a
fresh key. Each of those would turn a lost key into silent data access under a
key nobody chose — see `KeyNotResolved` in `secrets.py` for the same argument.

── Not cached ───────────────────────────────────────────────────────────────

`dek_for` reads and unwraps on every call. A process-lifetime cache of unwrapped
DEKs would mean a destroyed key keeps working until the process restarts, which
is precisely the guarantee this module exists to provide. If that ever becomes a
measured problem the fix is a cache with explicit invalidation on destroy, not a
quiet TTL — but it is not a problem yet and an unmeasured optimisation that
weakens crypto-shredding would be a poor trade.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, TypeVar

from core.connectors.secrets import (
    SealedSecret,
    SecretsError,
    WrappedKey,
    _unwrap_dek,
    configured_kek_id,
    dek_key_id,
    dek_version_of,
    generate_dek,
    rewrap,
    seal_with,
    wrap_dek,
)
from core.identity.db_context import tenant_connection
from core.identity.principal import Principal

logger = logging.getLogger("ira.connectors.keyring")

T = TypeVar("T")

#: The only purpose today. Named rather than inlined so the schema CHECK and the
#: code cannot drift into disagreeing about the vocabulary.
PURPOSE_CONNECTOR_CREDENTIALS: str = "connector_credentials"


class KeyringError(SecretsError):
    """Base for key-lifecycle failures."""


class KeyDestroyed(KeyringError):
    """The key that would open this credential was deliberately destroyed.

    Its own type because it is the one failure that is *working as intended*.
    An operator seeing it should reconnect the account, not go looking for a
    corrupted database — and a caller must never respond to it by re-sealing
    under a new key, which would be inventing a credential nobody has.
    """


class KeyNotFound(KeyringError):
    """No key row for that version, or no tenant context to look under.

    The two are deliberately one error. Under RLS "belongs to another tenant"
    and "does not exist" produce the same empty result, and that is the answer
    that leaks nothing — reporting them differently would turn the key registry
    into an oracle for which tenants exist.
    """


# ── What the registry hands back ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class KeyRecord:
    """A key row, without the key. Safe to log, return from an API, or show a
    user in the Privacy Centre.

    There is deliberately no field holding material, wrapped or otherwise: a
    record type that *could* carry a key is one that eventually does, in a log
    line somebody added in a hurry.
    """

    tenant_id: str
    purpose: str
    version: int
    kek_id: str
    created_at: Optional[str]
    retired_at: Optional[str]
    destroyed_at: Optional[str]
    destruction_reason: str = ""

    @property
    def destroyed(self) -> bool:
        return self.destroyed_at is not None

    @property
    def active(self) -> bool:
        return self.retired_at is None and self.destroyed_at is None


@dataclass(frozen=True, slots=True)
class RotationResult:
    """What a rotation actually did, counted rather than assumed."""

    tenant_id: str
    from_version: Optional[int]
    to_version: int
    credentials_rewrapped: int
    old_key_destroyed: bool
    note: str = ""
    #: P2-05. MFA DEKs are RE-WRAPPED, never re-minted and never destroyed, and
    #: no TOTP seed is decrypted to do it. Counted separately from credentials
    #: because the two are different operations on different key material.
    mfa_keys_rewrapped: int = 0
    #: Users whose MFA key could not be re-wrapped. Non-empty means those
    #: members' second factors need attention, not that the rotation failed.
    mfa_keys_failed: tuple[str, ...] = ()
    #: Phase 4. Voice record keys, RE-WRAPPED under the new KEK and never
    #: re-minted, with no template decrypted. Counted separately again, because
    #: these are per-ENROLMENT rather than per-user and a tenant may hold more
    #: of them than it has members.
    voice_keys_rewrapped: int = 0
    #: Enrolments whose record key could not be re-wrapped. This one is the most
    #: serious of the three: after the contract stage there is no plaintext to
    #: fall back to, so an entry here is a voiceprint that will not open again.
    voice_keys_failed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ShredResult:
    """What a destruction actually destroyed."""

    tenant_id: str
    versions_destroyed: tuple[int, ...] = ()
    already_destroyed: tuple[int, ...] = ()
    reason: str = ""

    @property
    def anything_destroyed(self) -> bool:
        return bool(self.versions_destroyed)


# ── AAD: what binds a key to its row ─────────────────────────────────────────


def wrap_aad(tenant_id: Any, purpose: str, version: int, kek_id: str) -> str:
    """The additional authenticated data a wrapped DEK is bound to.

    Every component is load-bearing. Drop `tenant_id` and a wrapped key can be
    moved between tenants by anyone who can write to the table; drop `version`
    and it can be moved between versions, which is the same attack aimed at a
    rotation; drop `kek_id` and a re-wrap under a second KEK is
    indistinguishable from the original.

    Built here and nowhere else, because two call sites that disagree about the
    format is how a key stops unwrapping six months later.
    """
    return f"dek:{tenant_id}:{purpose}:v{int(version)}:{kek_id}"


def credential_aad(tenant_id: Any, connector: str) -> str:
    """The AAD a stored connector credential is sealed under.

    Unchanged from Phase 7 — `f"{tenant_id}:{connector}"` — because changing it
    would make every existing credential fail to open, and a migration that
    silently breaks every connected account is not a migration.
    """
    return f"{tenant_id}:{connector}"


# ── Reading ──────────────────────────────────────────────────────────────────


def _record(row: Any) -> KeyRecord:
    def _iso(value: Any) -> Optional[str]:
        return value.isoformat() if value is not None else None

    return KeyRecord(
        tenant_id=str(row["tenant_id"]),
        purpose=row["purpose"],
        version=int(row["key_version"]),
        kek_id=row["kek_id"],
        created_at=_iso(row["created_at"]),
        retired_at=_iso(row["retired_at"]),
        destroyed_at=_iso(row["destroyed_at"]),
        destruction_reason=row["destruction_reason"] or "",
    )


async def inventory(
    principal: Principal, *, purpose: str = PURPOSE_CONNECTOR_CREDENTIALS
) -> list[KeyRecord]:
    """Every key row for this tenant, newest first. Never any material.

    Feeds the Privacy Centre: a user who asked for deletion can see that the key
    protecting their connector credentials was destroyed, and when. "We deleted
    it" is a claim; "the key was destroyed at 14:02 and here is the record" is
    evidence.
    """
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT tenant_id, purpose, key_version, kek_id, created_at, "
            "retired_at, destroyed_at, destruction_reason "
            "FROM tenant_data_keys WHERE tenant_id = $1 AND purpose = $2 "
            "ORDER BY key_version DESC",
            principal.tenant_id, purpose,
        )
    return [_record(r) for r in rows]


async def _wrapped_for(
    conn: Any, tenant_id: Any, purpose: str, version: int
) -> tuple[WrappedKey, str]:
    """The wrapped DEK for one version, or the reason there is not one."""
    row = await conn.fetchrow(
        "SELECT wrapped_dek, wrap_nonce, kek_id, destroyed_at, destruction_reason "
        "FROM tenant_data_keys "
        "WHERE tenant_id = $1 AND purpose = $2 AND key_version = $3",
        tenant_id, purpose, version,
    )
    if row is None:
        raise KeyNotFound(
            f"No data-encryption key version {version} exists for this tenant. "
            f"A credential referring to it cannot be opened. This usually means "
            f"a partial restore — the credential rows came back and the key "
            f"rows did not."
        )
    if row["destroyed_at"] is not None:
        raise KeyDestroyed(
            f"Data-encryption key version {version} was destroyed at "
            f"{row['destroyed_at']} ({row['destruction_reason'] or 'no reason recorded'}). "
            f"Every credential sealed under it is permanently unopenable, which "
            f"is what destroying it meant. Reconnect the account to store a new "
            f"credential; do NOT re-seal anything under a new key, because "
            f"there is no plaintext to re-seal."
        )
    return (
        WrappedKey(
            material=bytes(row["wrapped_dek"]),
            nonce=bytes(row["wrap_nonce"]),
            kek_id=row["kek_id"],
        ),
        row["kek_id"],
    )


async def dek_for(
    principal: Principal,
    version: int,
    *,
    purpose: str = PURPOSE_CONNECTOR_CREDENTIALS,
    env: Optional[dict[str, str]] = None,
) -> bytes:
    """The unwrapped DEK for one version. **In the architecture test's
    forbidden set** — a caller holding this can open every credential of that
    tenant and version, so it is as sensitive as `_open_secret` itself.

    Not cached: see the module docstring. A destroyed key must stop working
    immediately, not at the next process restart.
    """
    async with tenant_connection(principal) as conn:
        wrapped, kek_id = await _wrapped_for(
            conn, principal.tenant_id, purpose, version
        )
    return _unwrap_dek(
        wrapped,
        aad=wrap_aad(principal.tenant_id, purpose, version, kek_id),
        env=env,
    )


async def use_credential(
    principal: Principal,
    sealed: SealedSecret,
    fn: Callable[[str], Any],
    *,
    connector: str,
    env: Optional[dict[str, str]] = None,
) -> Any:
    """Run `fn` with the decrypted credential, resolving whichever key it needs.

    The same callback shape as `secrets.use_secret_async`, and for the same
    reason: no credential is ever a return value. What this adds is the key
    lookup, so a caller never has to hold a DEK to use a token.

    Handles both schemes. A Phase 7 row (`key_id` not `dek:`) opens under the
    KEK; a `dek:` row resolves its version first. Callers do not branch on which
    — a branch at every call site is a branch somebody eventually gets wrong.
    """
    from core.connectors.secrets import use_secret_async

    aad = credential_aad(principal.tenant_id, connector)
    version = dek_version_of(sealed.key_id)
    if version is None:
        return await use_secret_async(sealed, fn, aad=aad, env=env)

    key = await dek_for(principal, version, env=env)
    try:
        return await use_secret_async(sealed, fn, aad=aad, env=env, key=key)
    finally:
        del key


# ── Minting ──────────────────────────────────────────────────────────────────


async def _mint(
    conn: Any,
    principal: Principal,
    *,
    purpose: str,
    env: Optional[dict[str, str]],
) -> tuple[bytes, int]:
    """Create the next key version and return it with its number.

    The version is `MAX(key_version) + 1` over every row including destroyed
    ones, so a destroyed version number is never reused. Reuse would let a
    credential row still pointing at the old number silently open under a
    different key — the one failure mode where crypto-shredding appears to work
    and does not.
    """
    next_version = await conn.fetchval(
        "SELECT COALESCE(MAX(key_version), 0) + 1 FROM tenant_data_keys "
        "WHERE tenant_id = $1 AND purpose = $2",
        principal.tenant_id, purpose,
    )
    next_version = int(next_version)

    dek = generate_dek()
    # Wrapped before the row exists, so a failure to wrap — a missing KEK, most
    # likely — leaves no row at all rather than one the schema would have to
    # call destroyed.
    kek_id = configured_kek_id(env)
    wrapped = wrap_dek(
        dek,
        aad=wrap_aad(principal.tenant_id, purpose, next_version, kek_id),
        env=env,
    )

    await conn.execute(
        "INSERT INTO tenant_data_keys "
        "(tenant_id, purpose, key_version, wrapped_dek, wrap_nonce, kek_id) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        principal.tenant_id, purpose, next_version,
        wrapped.material, wrapped.nonce, wrapped.kek_id,
    )
    return dek, next_version


async def active_dek(
    principal: Principal,
    *,
    purpose: str = PURPOSE_CONNECTOR_CREDENTIALS,
    env: Optional[dict[str, str]] = None,
) -> tuple[bytes, str]:
    """The tenant's live DEK and the `key_id` to record with anything sealed
    under it, minting one if this tenant has never had a key.

    **In the architecture test's forbidden set**, as `dek_for` is.

    Minting on first use rather than at signup is deliberate: a tenant who never
    connects an account never has a key, so there is nothing to rotate, nothing
    to destroy and nothing to explain. The unique index
    `one_active_key_per_tenant_purpose` makes the concurrent case safe — two
    simultaneous first connects race, one INSERT loses, and the loser re-reads.
    """
    async with tenant_connection(principal) as conn:
        row = await conn.fetchrow(
            "SELECT key_version, wrapped_dek, wrap_nonce, kek_id "
            "FROM tenant_data_keys "
            "WHERE tenant_id = $1 AND purpose = $2 "
            "AND retired_at IS NULL AND destroyed_at IS NULL",
            principal.tenant_id, purpose,
        )
        if row is not None:
            version = int(row["key_version"])
            dek = _unwrap_dek(
                WrappedKey(
                    material=bytes(row["wrapped_dek"]),
                    nonce=bytes(row["wrap_nonce"]),
                    kek_id=row["kek_id"],
                ),
                aad=wrap_aad(principal.tenant_id, purpose, version, row["kek_id"]),
                env=env,
            )
            return dek, dek_key_id(version)

        try:
            dek, version = await _mint(conn, principal, purpose=purpose, env=env)
        except Exception as exc:  # noqa: BLE001
            # A unique-index collision means another request minted first. Not
            # an error — re-read and use theirs. Anything else is real.
            if "one_active_key_per_tenant_purpose" not in str(exc):
                raise
            retry = await conn.fetchrow(
                "SELECT key_version, wrapped_dek, wrap_nonce, kek_id "
                "FROM tenant_data_keys WHERE tenant_id = $1 AND purpose = $2 "
                "AND retired_at IS NULL AND destroyed_at IS NULL",
                principal.tenant_id, purpose,
            )
            if retry is None:  # pragma: no cover - would mean the index lied
                raise
            version = int(retry["key_version"])
            dek = _unwrap_dek(
                WrappedKey(
                    material=bytes(retry["wrapped_dek"]),
                    nonce=bytes(retry["wrap_nonce"]),
                    kek_id=retry["kek_id"],
                ),
                aad=wrap_aad(principal.tenant_id, purpose, version, retry["kek_id"]),
                env=env,
            )
    return dek, dek_key_id(version)


async def seal_credential(
    principal: Principal,
    plaintext: str,
    *,
    connector: str,
    env: Optional[dict[str, str]] = None,
) -> SealedSecret:
    """Seal a credential under this tenant's active DEK.

    The path every newly granted refresh token takes. `store.upsert_account`
    stores the result unchanged, so the `key_id` it records is what
    `use_credential` later reads to find the key again.
    """
    dek, key_id = await active_dek(principal, env=env)
    try:
        return seal_with(
            dek,
            plaintext,
            aad=credential_aad(principal.tenant_id, connector),
            key_id=key_id,
        )
    finally:
        del dek


# ── Rotating ─────────────────────────────────────────────────────────────────


def _as_member(principal: Principal, user_id) -> Principal:
    """The same tenant-bound principal, acting as one member.

    `060` made `connector_accounts` user-scoped, so every read and write of a
    credential has to name whose it is. Nothing about authority changes: this is
    an operator task that already holds the tenant, and the user id only selects
    which rows RLS exposes.
    """
    from dataclasses import replace

    return replace(principal, user_id=user_id)


async def _tenant_members(principal: Principal) -> list:
    """Every member of the tenant, so a per-user pass can cover all of them.

    ── Why this refuses rather than returning what it found ────────────────

    An incomplete list is worse than an exception, and the reason is two
    hundred lines below: `rotate_tenant` counts rows still pointing at the old
    key version and destroys that key when the count is zero. That count now
    sums over THIS list. A list missing a member produces a count missing their
    stragglers, and the key is destroyed while their credentials still need it —
    silently, and with no way back.

    So a failure here raises. A rotation that cannot establish who the members
    are must not proceed to a decision that depends on knowing.

    `tenant_memberships` is tenant-scoped, not user-scoped, so this read works
    on the ordinary tenant connection. Revoked and suspended members are
    INCLUDED deliberately: their credentials are still rows in the table, still
    sealed under the old key, and still stragglers. Skipping them would be the
    incomplete list this docstring is about.
    """
    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT user_id FROM tenant_memberships WHERE tenant_id = $1",
            principal.tenant_id,
        )

    members = [r["user_id"] for r in rows]
    if principal.user_id is not None and principal.user_id not in members:
        # The operator's own derived id, in case it owns rows in a deployment
        # that predates memberships.
        members.append(principal.user_id)
    if not members:
        raise RuntimeError(
            "rotation could not establish the tenant's members, so it cannot "
            "safely decide whether the old key is still in use. Refusing.")
    return members


async def rotate_tenant(
    principal: Principal,
    *,
    purpose: str = PURPOSE_CONNECTOR_CREDENTIALS,
    destroy_old: bool = True,
    reason: str = "scheduled key rotation",
    env: Optional[dict[str, str]] = None,
    rewrap_mfa: bool = True,
    mfa_old_env: Optional[dict[str, str]] = None,
) -> RotationResult:
    """Mint a new key version, re-seal this tenant's credentials under it, and
    destroy the old one.

    This is `rewrap`'s caller — the thing R11 recorded as missing. The order is
    the whole design, and each step is chosen so an interruption leaves a state
    that still works:

      1. **Read and decrypt every credential under the old key.** If this fails,
         nothing has changed yet.
      2. **Retire the old key.** New seals now have to wait for the new one. The
         old key still *opens*, so anything in flight keeps working.
      3. **Mint the new key.** The unique index permits this only because step 2
         retired the previous holder of "active".
      4. **Write the re-sealed credentials.** Interrupted here, some rows point
         at the new version and some at the old, and both keys still open —
         so the deployment is fully working, just half-rotated. Running the
         rotation again finishes it.
      5. **Destroy the old key.** Only now, and only if every row moved. The
         count is checked rather than assumed: destroying a key while a row
         still refers to it is the one way this operation can lose data.

    `destroy_old=False` leaves the old key live, which is the right call while
    a rotation is being introduced somewhere it has never run — it makes the
    step reversible, at the cost of the old key still existing.

    ── Step 6: MFA key material (P2-05) ────────────────────────────────────

    MFA DEKs live in `user_mfa_keys`, wrapped under the KEK rather than under
    the tenant DEK this function rotates, so steps 1–5 do not strand them. What
    *would* strand them is a KEK rotation with nothing to follow it: every
    wrapped MFA DEK names the KEK it was wrapped under, and `_unwrap_dek`
    refuses a mismatch — so every enrolled user's second factor stops opening
    at once, and the seeds cannot be recovered because their plaintext was
    cleared on migration. Rotation is the operator-facing entry point, so it is
    where the follow-up belongs.

    Three properties of this step are the point of the whole DEK layer:

      * **Re-wrapped, not re-minted.** The DEK and its version do not change,
        so every sealed seed stays readable.
      * **No seed is decrypted.** Re-sealing seeds would mean holding every
        plaintext second factor in the tenant in memory during routine key
        maintenance.
      * **Nothing is destroyed.** `destroy_keys` below touches
        `tenant_data_keys` only. Destroying an MFA DEK destroys a second
        factor with no recovery path, so rotation never does it — that is
        `destroy_mfa_keys`, reached only by account erasure.

    It runs last, and its failures are collected rather than raised, so a
    member with an unopenable key cannot undo a rotation that has already
    re-sealed the tenant's credentials.

    `mfa_old_env` names the KEK the MFA DEKs are currently wrapped under, for
    the case where the KEK itself is what rotated; it defaults to `env`, which
    makes the step a nonce refresh under an unchanged KEK.
    """
    # (account_id, connector, sealed, owner_user_id)
    accounts: list[tuple[Any, str, SealedSecret, Any]] = []
    from_version: Optional[int] = None

    # `060` made `connector_accounts` user-scoped, so the tenant-wide read this
    # loop used to do no longer exists: a connection bound to this tenant and
    # the operator's derived user id matches ZERO rows. Every pass over
    # credentials now goes member by member, and `_tenant_members` is what makes
    # that safe — an incomplete member list means an incomplete rotation, and
    # further down, a destroyed key that rows still need.
    members = await _tenant_members(principal)

    for member in members:
        async with tenant_connection(_as_member(principal, member)) as conn:
            rows = await conn.fetch(
                "SELECT id, connector, credential_ciphertext, encryption_key_id, "
                "credential_nonce FROM connector_accounts "
                "WHERE tenant_id = $1 AND credential_ciphertext IS NOT NULL "
                "AND revoked_at IS NULL",
                principal.tenant_id,
            )
        for r in rows:
            accounts.append((
                r["id"], r["connector"],
                SealedSecret(
                    ciphertext=bytes(r["credential_ciphertext"]),
                    key_id=r["encryption_key_id"] or "",
                    nonce=bytes(r["credential_nonce"] or b""),
                ),
                member,
            ))

    # `tenant_data_keys` is tenant-scoped and stays a tenant-wide read.
    async with tenant_connection(principal) as conn:
        old_row = await conn.fetchrow(
            "SELECT key_version, wrapped_dek, wrap_nonce, kek_id "
            "FROM tenant_data_keys WHERE tenant_id = $1 AND purpose = $2 "
            "AND retired_at IS NULL AND destroyed_at IS NULL",
            principal.tenant_id, purpose,
        )

    old_dek: Optional[bytes] = None
    if old_row is not None:
        from_version = int(old_row["key_version"])
        old_dek = _unwrap_dek(
            WrappedKey(
                material=bytes(old_row["wrapped_dek"]),
                nonce=bytes(old_row["wrap_nonce"]),
                kek_id=old_row["kek_id"],
            ),
            aad=wrap_aad(principal.tenant_id, purpose, from_version, old_row["kek_id"]),
            env=env,
        )

    # ── Steps 2 and 3 COMMIT before step 4 begins ────────────────────────────
    #
    # This block used to enclose the rewrap loop as well, and that was a defect
    # severe enough to be worth spelling out, because the shape is subtle and
    # the consequence is permanent.
    #
    # `tenant_connection` opens a transaction. The rewrap loop below writes each
    # credential on a DIFFERENT connection — it has to, because `060` made
    # `connector_accounts` user-scoped and each row must be written by a
    # connection bound to its owner — and those writes commit one at a time.
    # So with the mint inside the same transaction as the loop, any failure
    # after the first credential was written rolled the NEW KEY ROW back while
    # leaving that credential committed and pointing at it. The row then named a
    # key version that had never existed, and `_wrapped_for` raised KeyNotFound
    # for it. Permanently: there is no plaintext to re-seal.
    #
    # P4-15 reproduced it with no mocking — a single corrupt credential is
    # enough — and observed exactly that: `gmail -> dek:2` committed, `dek:2`
    # absent from `tenant_data_keys`, and the credential unopenable.
    #
    # It also made this function's own docstring false. "Interrupted here, some
    # rows point at the new version and some at the old, and both keys still
    # open" is only true if the new key survives the interruption, which means
    # its transaction must end before the first credential is written. Retired
    # keys still open — `_wrapped_for` refuses only destroyed ones — so the
    # committed intermediate state is the working, half-rotated one the
    # docstring describes, and re-running finishes it.
    async with tenant_connection(principal) as conn:
        if old_row is not None:
            await conn.execute(
                "UPDATE tenant_data_keys SET retired_at = NOW() "
                "WHERE tenant_id = $1 AND purpose = $2 AND key_version = $3",
                principal.tenant_id, purpose, from_version,
            )
        new_dek, to_version = await _mint(
            conn, principal, purpose=purpose, env=env
        )
    new_key_id = dek_key_id(to_version)

    rewrapped = 0
    try:
        for account_id, connector, sealed, _owner in accounts:
            aad = credential_aad(principal.tenant_id, connector)
            version = dek_version_of(sealed.key_id)
            # A Phase 7 row opens under the KEK; a dek: row under the key it
            # names, which is not necessarily the one just retired — a
            # half-finished earlier rotation can leave a mixture, and
            # assuming otherwise would strand exactly those rows.
            if version is None:
                source_key = None
            elif version == from_version:
                source_key = old_dek
            else:
                source_key = await dek_for(
                    principal, version, purpose=purpose, env=env
                )

            resealed = rewrap(
                sealed,
                aad=aad,
                old_key=source_key,
                new_key=new_dek,
                new_key_id=new_key_id,
                old_env=env,
            )
            # Written on a connection bound to the row's OWNER, not to the
            # operator. `060` made this table user-scoped, so the operator's
            # connection matches zero rows — an UPDATE through it would
            # report success, change nothing, and leave the credential under
            # a key this rotation is about to destroy.
            #
            # `written` is checked rather than assumed for that exact
            # reason: asyncpg reports "UPDATE 0" without raising, and a
            # rotation that silently rewraps nothing is the failure this
            # whole rework exists to avoid.
            async with tenant_connection(
                    _as_member(principal, _owner)) as owner_conn:
                written = await owner_conn.execute(
                    "UPDATE connector_accounts SET credential_ciphertext = $2, "
                    "encryption_key_id = $3, credential_nonce = $4, "
                    "updated_at = NOW() WHERE id = $1",
                    account_id, resealed.ciphertext, resealed.key_id,
                    resealed.nonce,
                )
            if not str(written).endswith(" 1"):
                raise RuntimeError(
                    f"rotation could not rewrite connector account "
                    f"{account_id}: the UPDATE matched no row "
                    f"({written!r}). Refusing to continue — a rotation that "
                    f"reports success while leaving a credential under the "
                    f"old key is how the key gets destroyed underneath it."
                )
            rewrapped += 1
    finally:
        del new_dek

    destroyed = False
    if destroy_old and old_row is not None:
        # Checked, not assumed. If any row still points at the old version the
        # key stays — a rotation that destroys a key someone is still using has
        # turned a maintenance task into data loss.
        #
        # This count is the single most dangerous statement in the module, and
        # `060` is what made it dangerous. `connector_accounts` is user-scoped
        # now, so the operator's connection sees NOTHING: the count would come
        # back zero for a tenant with a hundred stragglers, and the key would be
        # destroyed under every one of them. Irreversibly, and without an error.
        #
        # So it sums per member, over the same list the rewrap pass used. If
        # that list were incomplete the count would be too — which is why
        # `_tenant_members` refuses rather than returning a partial answer.
        stragglers = 0
        for member in members:
            async with tenant_connection(_as_member(principal, member)) as conn:
                stragglers += int(await conn.fetchval(
                    "SELECT COUNT(*) FROM connector_accounts "
                    "WHERE tenant_id = $1 AND credential_ciphertext IS NOT NULL "
                    "AND encryption_key_id = $2",
                    principal.tenant_id, dek_key_id(from_version),
                ) or 0)
        if int(stragglers or 0) == 0:
            await destroy_keys(
                principal, reason=reason, purpose=purpose, versions=(from_version,)
            )
            destroyed = True
        else:
            logger.warning(
                "keeping key version %s for tenant %s: %s credential(s) still "
                "reference it", from_version, principal.tenant_id, stragglers,
            )

    mfa_rewrapped = 0
    mfa_failed: tuple[str, ...] = ()
    if rewrap_mfa:
        from security.mfa_crypto import rewrap_tenant_mfa_keys

        count, failures = await rewrap_tenant_mfa_keys(
            principal,
            old_env=mfa_old_env if mfa_old_env is not None else env,
            new_env=env,
        )
        mfa_rewrapped, mfa_failed = count, tuple(failures)

    # ── Step 7: voice record keys (Phase 4 batch 02, wired in batch 05) ────
    #
    # `voice_profiles` holds one KEK-wrapped key PER ENROLMENT (062). They are
    # in exactly the position `user_mfa_keys` is in — wrapped under the KEK, not
    # under the tenant DEK — so steps 1-5 do not strand them and a KEK rotation
    # with no follow-up destroys every one.
    #
    # This step did not exist when 062 shipped, and the omission would have been
    # unrecoverable after the contract stage: 063 drops the plaintext, so the
    # ciphertext is the only copy and its key stops unwrapping. A routine
    # rotation would have destroyed every enrolled voiceprint and reported
    # success. `test_p4_rotation_reaches_everything` is the guard that now makes
    # forgetting the next one impossible rather than merely unlikely.
    voice_rewrapped = 0
    voice_failed: tuple[str, ...] = ()
    if rewrap_mfa:
        from security.voice_crypto import rewrap_tenant_voice_keys

        v_count, v_failures = await rewrap_tenant_voice_keys(
            principal,
            old_env=mfa_old_env if mfa_old_env is not None else env,
            new_env=env,
        )
        voice_rewrapped, voice_failed = v_count, tuple(v_failures)

    note = ""
    if old_row is None:
        note = "this tenant had no previous key; nothing was re-sealed"
    elif destroy_old and not destroyed:
        note = (
            "the old key was NOT destroyed because credentials still reference "
            "it — re-run the rotation"
        )
    if mfa_failed:
        note = (note + "; " if note else "") + (
            f"{len(mfa_failed)} MFA key(s) could not be re-wrapped — those "
            f"users' second factors will not open under the current KEK"
        )
    if voice_failed:
        note = (note + "; " if note else "") + (
            f"{len(voice_failed)} voice enrolment(s) could not be re-wrapped — "
            f"those templates will not open under the current KEK, and after "
            f"the contract stage there is no plaintext to fall back to"
        )
    return RotationResult(
        tenant_id=str(principal.tenant_id),
        from_version=from_version,
        to_version=to_version,
        credentials_rewrapped=rewrapped,
        old_key_destroyed=destroyed,
        note=note,
        mfa_keys_rewrapped=mfa_rewrapped,
        mfa_keys_failed=mfa_failed,
        voice_keys_rewrapped=voice_rewrapped,
        voice_keys_failed=voice_failed,
    )


# ── Destroying ───────────────────────────────────────────────────────────────


async def destroy_keys(
    principal: Principal,
    *,
    reason: str,
    purpose: str = PURPOSE_CONNECTOR_CREDENTIALS,
    versions: Optional[tuple[int, ...]] = None,
) -> ShredResult:
    """Crypto-shredding. Destroy this tenant's key material.

    After this, every credential sealed under the destroyed versions is
    permanently unopenable — including copies this process never knew about.
    That is the property `core/privacy/erasure.py` uses: a DELETE has to find
    every row, and destroying the key does not.

    `reason` is required and the schema enforces a minimum length, because a
    destruction nobody can explain is indistinguishable from a bug. It is shown
    to the user in the Privacy Centre.

    `versions=None` means every live version. Erasure passes None; a rotation
    passes the single version it just finished replacing.
    """
    reason = (reason or "").strip()
    if len(reason) < 8:
        raise KeyringError(
            "destroying a key needs a reason of at least 8 characters. The "
            "reason is what the affected user is shown when their connected "
            "accounts stop working, and the schema refuses a row without one."
        )

    async with tenant_connection(principal) as conn:
        if versions is None:
            rows = await conn.fetch(
                "SELECT key_version, destroyed_at FROM tenant_data_keys "
                "WHERE tenant_id = $1 AND purpose = $2",
                principal.tenant_id, purpose,
            )
        else:
            rows = await conn.fetch(
                "SELECT key_version, destroyed_at FROM tenant_data_keys "
                "WHERE tenant_id = $1 AND purpose = $2 "
                "AND key_version = ANY($3::int[])",
                principal.tenant_id, purpose, list(versions),
            )

        already = tuple(
            int(r["key_version"]) for r in rows if r["destroyed_at"] is not None
        )
        live = tuple(
            int(r["key_version"]) for r in rows if r["destroyed_at"] is None
        )

        destroyed: list[int] = []
        for version in live:
            # The CHECK constraint makes "destroyed" and "holds material"
            # mutually exclusive, so this single statement cannot half-succeed
            # into a row claiming to be shredded with the key still in it.
            await conn.execute(
                "UPDATE tenant_data_keys SET destroyed_at = NOW(), "
                "destruction_reason = $4, wrapped_dek = NULL, wrap_nonce = NULL "
                "WHERE tenant_id = $1 AND purpose = $2 AND key_version = $3",
                principal.tenant_id, purpose, version, reason,
            )
            destroyed.append(version)

    if destroyed:
        logger.info(
            "destroyed data-encryption key version(s) %s for tenant %s: %s",
            destroyed, principal.tenant_id, reason,
        )
    return ShredResult(
        tenant_id=str(principal.tenant_id),
        versions_destroyed=tuple(destroyed),
        already_destroyed=already,
        reason=reason,
    )


__all__ = [
    "PURPOSE_CONNECTOR_CREDENTIALS",
    "KeyDestroyed",
    "KeyNotFound",
    "KeyRecord",
    "KeyringError",
    "RotationResult",
    "ShredResult",
    "active_dek",
    "credential_aad",
    "dek_for",
    "destroy_keys",
    "inventory",
    "rotate_tenant",
    "seal_credential",
    "use_credential",
    "wrap_aad",
]
