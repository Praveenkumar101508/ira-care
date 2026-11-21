"""What IRA may truthfully say about where a user's data is.

The transformation brief is blunt about this, and it is the easiest rule in the
whole project to break by accident:

    Replace local-only wording in the cloud interface with truthful language.
    Do not claim local processing when cloud processing occurs.
    Do not claim data deletion before all relevant stores are handled.
    Do not claim immediate backup deletion unless implemented.

IRA Private's interface says things like "everything stays on your machine",
and for IRA Private that is **true** — it is one of the reasons people run it.
Ship the same string in a hosted product and it becomes a lie, told in a
reassuring tone, on the screen where the user decides how much to trust the
thing.

So the wording is not a string in a component. It is derived from the edition
and from what is actually configured, in one place, with tests asserting the
cloud edition never emits a local-only claim.

── Why this is code and not a style guide ──────────────────────────────────

A style guide is checked by whoever remembers. This is checked by
`tests/privacy/test_disclosure.py`, which asserts that every string this module
can produce for the cloud edition is free of local-only vocabulary — and that
the vocabulary list itself is non-empty, so the check cannot pass vacuously.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from core.editions import EDITION_CLOUD, EDITION_PRIVATE, current_edition

#: Phrases that assert data never leaves the user's own machine. Truthful for
#: IRA Private; false for a hosted deployment. The test suite scans every
#: cloud-edition string for these.
LOCAL_ONLY_CLAIMS: Final[tuple[str, ...]] = (
    "never leaves your device",
    "never leaves your machine",
    "stays on your device",
    "stays on your machine",
    "on your computer only",
    "entirely local",
    "fully local",
    "local-only",
    "local only",
    "nothing is uploaded",
    "no data leaves",
)

#: Claims nothing in this build implements. Never emitted in any edition.
UNIMPLEMENTED_CLAIMS: Final[tuple[str, ...]] = (
    "deleted from backups",
    "removed from all backups",
    "erased everywhere",
    "permanently erased",
    "end-to-end encrypted",
    "zero-knowledge",
    "gdpr certified",
    "hipaa compliant",
    "soc 2 certified",
)


@dataclass(frozen=True, slots=True)
class StorageDisclosure:
    """Where a user's data actually is, in words they can act on."""

    edition: str
    headline: str
    detail: str
    #: True only when the deployment genuinely processes everything locally.
    local_processing: bool


def storage_disclosure(edition: str | None = None) -> StorageDisclosure:
    """The truthful sentence about where this deployment keeps things."""
    name = edition or current_edition()

    if name == EDITION_PRIVATE:
        return StorageDisclosure(
            edition=name,
            headline="Stored on this machine",
            detail=(
                "IRA Private runs on hardware you control. Your conversations, "
                "memories and files are stored in the database on this machine "
                "and are not sent anywhere unless you switch on an integration "
                "that does so."
            ),
            local_processing=True,
        )

    # Cloud. Note what is NOT said: nothing about the data never leaving, and
    # nothing about encryption this build does not implement.
    return StorageDisclosure(
        edition=name,
        headline="Encrypted in your IRA cloud workspace",
        detail=(
            "IRA Cloud stores your conversations, memories and files in a "
            "workspace that only your account can reach. It runs on servers "
            "operated for you, not on your own device. You can export or delete "
            "your data at any time from the Privacy Centre."
        ),
        local_processing=False,
    )


def deletion_disclosure(
    *,
    stores_reached: int,
    stores_total: int,
    unredactable_records: int,
    backup_residue: bool = True,
) -> str:
    """What to tell a user after a deletion. Never more than was done.

    Three things this refuses to say:

      * that anything was deleted from backups — nothing here does that;
      * that the deletion was complete when a store could not be reached;
      * that audit records were erased when some could not be.

    A user who is told "all your data has been deleted" and later discovers a
    backup copy has been misled, even if every live store really was cleared.
    """
    parts: list[str] = []

    if stores_reached >= stores_total:
        parts.append(f"Removed from all {stores_total} live stores.")
    else:
        parts.append(
            f"Removed from {stores_reached} of {stores_total} live stores. "
            f"The remaining store(s) could not be reached and are listed below; "
            f"this deletion is incomplete until they are."
        )

    if unredactable_records > 0:
        parts.append(
            f"{unredactable_records} older audit record(s) could not have their "
            f"content erased: their integrity proof includes the content itself, "
            f"so erasing it would make the audit trail unverifiable. They record "
            f"that an action happened, not what it was about."
        )
    elif unredactable_records < 0:
        parts.append(
            "The number of audit records that could not be erased could not be "
            "determined. Treat it as unknown rather than zero."
        )

    if backup_residue:
        parts.append(
            "Backups are not edited by this operation. Copies in backup storage "
            "age out under the retention schedule rather than being deleted on "
            "request."
        )

    return " ".join(parts)


def voice_retention_disclosure(hours: int) -> str:
    if hours <= 0:
        return (
            "Voice recordings are discarded as soon as they have been "
            "transcribed. Only the text is kept."
        )
    return (
        f"Voice recordings are kept for {hours} hour(s) after transcription, "
        f"then discarded."
    )


def memory_mode_disclosure(mode: str) -> str:
    return {
        "off": "IRA keeps nothing between conversations.",
        "ask": "IRA asks before storing anything. You approve each memory.",
        "automatic": (
            "IRA stores what seems useful without asking. You can review, edit "
            "and delete anything it has kept."
        ),
    }.get(mode, "Memory behaviour is not configured.")


def contains_local_only_claim(text: str) -> list[str]:
    """Which local-only phrases appear in `text`. Empty is the good answer."""
    lowered = text.lower()
    return [phrase for phrase in LOCAL_ONLY_CLAIMS if phrase in lowered]


def contains_unimplemented_claim(text: str) -> list[str]:
    lowered = text.lower()
    return [phrase for phrase in UNIMPLEMENTED_CLAIMS if phrase in lowered]


#: Capabilities this module's text PROMISES, and the route each one requires.
#:
#: P12-05 finding K, and the reason it is a mapping rather than another entry on
#: `UNIMPLEMENTED_CLAIMS`. That list is a deny-list of nine substrings, and the
#: cloud disclosure said
#:
#:     "You can export or delete your data at any time from this page."
#:
#: while no export existed anywhere in the codebase. The sentence contains none
#: of the banned phrases, so the guard written to catch unimplemented claims did
#: not catch one — and could not have, because it can only forbid wordings
#: somebody already thought of. A promise made in new words is invisible to it.
#:
#: This asks the opposite question. Instead of "does the text contain a phrase
#: we banned", it asks "for every capability the text claims, is there a route
#: that provides it" — checked against the real application in
#: `tests/privacy/test_erasure_and_disclosure.py`. A claim can then only be
#: added by either building the thing or editing this map, and editing this map
#: is a visible act in a diff.
CAPABILITY_CLAIMS: Final[dict[str, str]] = {
    "export": "/api/v1/privacy/export",
    "delete": "/api/v1/privacy/delete",
}


def claimed_capabilities(text: str) -> list[str]:
    """Which capability verbs `text` promises the reader."""
    lowered = text.lower()
    return sorted(name for name in CAPABILITY_CLAIMS if name in lowered)


__all__ = [
    "CAPABILITY_CLAIMS",
    "LOCAL_ONLY_CLAIMS",
    "UNIMPLEMENTED_CLAIMS",
    "StorageDisclosure",
    "contains_local_only_claim",
    "claimed_capabilities",
    "contains_unimplemented_claim",
    "deletion_disclosure",
    "memory_mode_disclosure",
    "storage_disclosure",
    "voice_retention_disclosure",
]
