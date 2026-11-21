"""Sealing and opening a person's access notes for IRA CARE.

Lives in this package because the architecture rule is that nothing outside
`core/connectors/` reaches key material, and this is the module that seals
and opens these notes. It is the same envelope as a voice template: the notes
are encrypted under a freshly minted per-row data key, and that key is wrapped
under the configured key-encryption key with the tenant and person id as the
binding, so a row cannot be lifted onto another person or tenant. The
plaintext is never returned: it is handed to a callback and released.

Who may open the notes, and the access-log row for each opening, are the care
package's business (`care/accessnotes.py`, `care/repository.py`).
"""
from __future__ import annotations

from typing import Any, Callable

from core.connectors.secrets import (
    SealedSecret,
    SecretsError,
    WrappedKey,
    _unwrap_dek,
    dek_key_id,
    generate_dek,
    is_configured,
    seal_with,
    use_secret,
    wrap_dek,
)

MAX_LENGTH = 2000
KEY_VERSION = 1
#: The six columns on care_people that hold the sealed notes.
COLUMNS = ("access_notes_ciphertext", "access_notes_nonce", "access_notes_wrapped_dek",
           "access_notes_wrap_nonce", "access_notes_kek_id", "access_notes_key_version")


class AccessNotesUnavailable(RuntimeError):
    """The notes could not be opened; the message names no key material."""


def _wrap_aad(tenant_id, person_id) -> str:
    return f"care_access_notes:wrap:{tenant_id}:{person_id}"


def _aad(tenant_id, person_id) -> str:
    return f"care_access_notes:{tenant_id}:{person_id}"


def configured() -> bool:
    return is_configured()


def seal_notes(text: str, *, tenant_id, person_id) -> dict[str, Any]:
    """The six column values for the row; all None for an empty text."""
    text = (text or "").strip()
    if not text:
        return {c: None for c in COLUMNS}
    if len(text) > MAX_LENGTH:
        raise ValueError(f"access notes are limited to {MAX_LENGTH} characters")
    if person_id is None or tenant_id is None:
        raise SecretsError("refusing to seal access notes without the row they belong to")
    dek = generate_dek()
    try:
        wrapped: WrappedKey = wrap_dek(dek, aad=_wrap_aad(tenant_id, person_id))
        sealed: SealedSecret = seal_with(dek, text, aad=_aad(tenant_id, person_id), key_id=dek_key_id(KEY_VERSION))
    finally:
        del dek
    return {
        "access_notes_ciphertext": sealed.ciphertext, "access_notes_nonce": sealed.nonce,
        "access_notes_wrapped_dek": wrapped.material, "access_notes_wrap_nonce": wrapped.nonce,
        "access_notes_kek_id": wrapped.kek_id, "access_notes_key_version": KEY_VERSION,
    }


def use_notes(row, fn: Callable[[str], Any], *, tenant_id):
    """Open the notes and hand them to `fn`; `fn("")` when none are stored."""
    if row.get("access_notes_ciphertext") is None:
        return fn("")
    try:
        dek = _unwrap_dek(
            WrappedKey(material=bytes(row["access_notes_wrapped_dek"]), nonce=bytes(row["access_notes_wrap_nonce"]),
                       kek_id=row["access_notes_kek_id"]),
            aad=_wrap_aad(tenant_id, row["id"]))
    except Exception as exc:  # noqa: BLE001 - one message, no oracle
        raise AccessNotesUnavailable("the access notes could not be opened") from exc
    try:
        return use_secret(
            SealedSecret(ciphertext=bytes(row["access_notes_ciphertext"]),
                         key_id=dek_key_id(int(row.get("access_notes_key_version") or KEY_VERSION)),
                         nonce=bytes(row["access_notes_nonce"])),
            fn, aad=_aad(tenant_id, row["id"]), key=dek)
    except Exception as exc:  # noqa: BLE001
        raise AccessNotesUnavailable("the access notes could not be opened") from exc
    finally:
        del dek
