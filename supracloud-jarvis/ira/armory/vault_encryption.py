"""
armory/vault_encryption.py — the cryptographic floor of the Armory Vault.

Two independent jobs, deliberately kept separate so the security-critical one has
no heavy dependency:

  1. Master-key VERIFICATION (stdlib only).
     A salted PBKDF2-HMAC-SHA256 verifier is what lands in ``.env`` — never the
     plaintext key. ``hash_master_key`` produces it; ``verify_master_key`` checks a
     candidate in constant time (``hmac.compare_digest``). This uses only
     ``hashlib``/``hmac``/``secrets`` so it works in every environment and is fully
     unit-testable without native crypto.

  2. Vault BODY encryption (AES-256-GCM, via ``cryptography``, lazy-imported).
     The vault body (manifest.json, the compose file, whitelist.txt — serialised
     together) is sealed with AES-256-GCM under a key derived from the master key
     and the vault's own random salt. GCM is authenticated: a wrong key or any
     tampering makes ``decrypt`` raise rather than return garbage — the vault stays
     locked. ``cryptography`` is imported only inside the functions that need it, so
     importing this module (and the whole armory package) never requires it.

Design choices that make this fail-closed:
  * verification salt ≠ encryption salt, so the ``.env`` verifier can never be used
    to derive the AES key.
  * every ``encrypt`` uses a fresh 96-bit nonce; nonces are stored with the blob.
  * KDF parameters travel in the (non-secret) blob header, so the vault can be
    opened after a parameter bump as long as the master key is right.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass

# ── KDF parameters ────────────────────────────────────────────────────────────
# PBKDF2-HMAC-SHA256. 600k iterations follows current OWASP guidance for SHA-256.
KDF_NAME = "pbkdf2_sha256"
KDF_ITERATIONS = 600_000
SALT_BYTES = 16
KEY_BYTES = 32          # AES-256
VERIFIER_BYTES = 32
NONCE_BYTES = 12        # AES-GCM standard nonce

#: Domain-separation labels appended to the password before derivation so the
#: verifier and the encryption key are independent even under the same salt.
_VERIFY_LABEL = b"ira-armory-verify-v1"
_ENCRYPT_LABEL = b"ira-armory-encrypt-v1"

_BLOB_MAGIC = "IRA-ARMORY-VAULT"
_BLOB_VERSION = 1


# ── 1. Master-key verification (stdlib only) ──────────────────────────────────

def _pbkdf2(password: str, salt: bytes, *, label: bytes,
            iterations: int = KDF_ITERATIONS, length: int = KEY_BYTES) -> bytes:
    """Derive `length` bytes from `password`+`label` with PBKDF2-HMAC-SHA256.

    The label is folded into the password so the same (password, salt) yields
    distinct outputs for verification vs. encryption — cheap domain separation.
    """
    material = password.encode("utf-8") + b"\x00" + label
    return hashlib.pbkdf2_hmac("sha256", material, salt, iterations, dklen=length)


def hash_master_key(master_key: str, *, iterations: int = KDF_ITERATIONS) -> str:
    """Return the ``.env``-safe verifier string for a master key.

    Format: ``pbkdf2_sha256$<iterations>$<salt_b64>$<verifier_b64>``. Contains no
    reversible secret — only a one-way PBKDF2 digest of the key.
    """
    if not master_key or not master_key.strip():
        raise ValueError("master key must not be empty")
    salt = secrets.token_bytes(SALT_BYTES)
    verifier = _pbkdf2(master_key, salt, label=_VERIFY_LABEL,
                       iterations=iterations, length=VERIFIER_BYTES)
    return "{}${}${}${}".format(
        KDF_NAME, iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(verifier).decode("ascii"),
    )


def verify_master_key(master_key: str, stored: str) -> bool:
    """Constant-time check of a candidate key against a stored verifier.

    Fail-closed: any malformed verifier, empty input, or mismatch returns False
    (never raises to the caller), so a broken/absent verifier can never be read
    as "unlocked".
    """
    try:
        if not master_key or not stored:
            return False
        name, iter_s, salt_b64, verifier_b64 = stored.split("$", 3)
        if name != KDF_NAME:
            return False
        iterations = int(iter_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(verifier_b64)
        candidate = _pbkdf2(master_key, salt, label=_VERIFY_LABEL,
                            iterations=iterations, length=len(expected))
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False


# ── 2. Vault body encryption (AES-256-GCM) ────────────────────────────────────

def _require_aesgcm():
    """Lazy-import AESGCM so the armory package imports without native crypto.

    A clear, actionable error is raised only when encryption is actually needed.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM
    except Exception as exc:  # noqa: BLE001 — surface a fix, not a stack trace
        raise RuntimeError(
            "AES-256-GCM requires the 'cryptography' package. Install it "
            "(pip install cryptography) to lock/unlock the Armory vault body."
        ) from exc


def derive_encryption_key(master_key: str, salt: bytes,
                          *, iterations: int = KDF_ITERATIONS) -> bytes:
    """Derive the 32-byte AES-256 key for the vault body from the master key."""
    return _pbkdf2(master_key, salt, label=_ENCRYPT_LABEL,
                   iterations=iterations, length=KEY_BYTES)


@dataclass(frozen=True)
class VaultBlob:
    """A sealed vault: a JSON header (KDF params, salt, nonce) + ciphertext.

    Serialised as ``<header_json>\\n<ciphertext_bytes>`` where the ciphertext is
    the raw AES-GCM output (tag appended by the cipher). The header is plaintext
    and carries nothing secret.
    """

    header: dict
    ciphertext: bytes

    def to_bytes(self) -> bytes:
        head = json.dumps(self.header, sort_keys=True, separators=(",", ":"))
        return head.encode("utf-8") + b"\n" + self.ciphertext

    @classmethod
    def from_bytes(cls, raw: bytes) -> "VaultBlob":
        newline = raw.find(b"\n")
        if newline < 0:
            raise ValueError("corrupt vault blob: no header terminator")
        header = json.loads(raw[:newline].decode("utf-8"))
        if header.get("magic") != _BLOB_MAGIC:
            raise ValueError("not an Armory vault blob")
        return cls(header=header, ciphertext=raw[newline + 1:])


def encrypt(plaintext: bytes, master_key: str,
            *, iterations: int = KDF_ITERATIONS) -> bytes:
    """Seal `plaintext` under the master key. Returns the serialised VaultBlob."""
    AESGCM = _require_aesgcm()
    salt = secrets.token_bytes(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    key = derive_encryption_key(master_key, salt, iterations=iterations)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    header = {
        "magic": _BLOB_MAGIC,
        "version": _BLOB_VERSION,
        "kdf": KDF_NAME,
        "iterations": iterations,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "cipher": "AES-256-GCM",
    }
    return VaultBlob(header=header, ciphertext=ciphertext).to_bytes()


def decrypt(blob: bytes, master_key: str) -> bytes:
    """Open a sealed VaultBlob. Raises on a wrong key or any tampering.

    AES-GCM authenticates: a bad key or altered ciphertext fails the tag check
    and raises, so a failed unlock can never yield readable bytes.
    """
    parsed = VaultBlob.from_bytes(blob)
    header = parsed.header
    salt = base64.b64decode(header["salt"])
    nonce = base64.b64decode(header["nonce"])
    iterations = int(header.get("iterations", KDF_ITERATIONS))
    key = derive_encryption_key(master_key, salt, iterations=iterations)
    AESGCM = _require_aesgcm()
    return AESGCM(key).decrypt(nonce, parsed.ciphertext, None)


def looks_encrypted(raw: bytes) -> bool:
    """True if `raw` is a well-formed Armory vault blob header.

    Used by tooling to confirm an at-rest vault is sealed (gibberish body under a
    small plaintext header) rather than accidentally left decrypted.
    """
    try:
        VaultBlob.from_bytes(raw)
        return True
    except Exception:
        return False


__all__ = [
    "KDF_NAME", "KDF_ITERATIONS", "KEY_BYTES",
    "hash_master_key", "verify_master_key", "derive_encryption_key",
    "encrypt", "decrypt", "looks_encrypted", "VaultBlob",
]
