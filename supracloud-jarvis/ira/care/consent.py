"""Who may read a person's transcripts.

Family see alerts and summaries by default. The full text of what the person
said is readable only when a consent recorded in `care_consents` says so, and
the consent that applies depends on who is asking:

    care-circle role      consent kind
    family                transcripts_family
    carer, clinician      transcripts_carer
    care_organisation     transcripts_organisation

The asker's role is their row in the person's care circle (matched on the
account id). No row, no transcripts: an administrator who is not in the circle
is not thereby a reader of the person's conversations. The deployment owner in
the private edition is treated as a carer, because in a single-household
install the owner is the person who set the watch up.

Consent is granted by the person, or by their legal representative, and may be
revoked; the latest record of each kind wins.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional

CONSENT_FOR_ROLE: Mapping[str, str] = {
    "family": "transcripts_family",
    "carer": "transcripts_carer",
    "clinician": "transcripts_carer",
    "care_organisation": "transcripts_organisation",
}

CONSENT_KINDS = frozenset({
    "transcripts_family", "transcripts_carer", "transcripts_organisation",
    "health_data_family", "data_retention",
    # IRA may use the familiar topics and topics to avoid in the communication profile (110).
    "topics",
})

#: Months of history kept when no consent record names a retention period.
DEFAULT_RETENTION_MONTHS = 12


def effective_consents(records: Iterable[Mapping]) -> dict[str, Mapping]:
    """The latest record per kind, by `recorded_at`.

    `records` are rows of `care_consents`. A later record replaces an earlier
    one, and a record with `revoked_at` set counts as not granted whatever its
    `granted` flag says.
    """
    latest: dict[str, Mapping] = {}
    for row in records:
        kind = row["kind"]
        current = latest.get(kind)
        if current is None or row["recorded_at"] > current["recorded_at"]:
            latest[kind] = row
    return latest


def consent_granted(records: Iterable[Mapping], kind: str) -> bool:
    row = effective_consents(records).get(kind)
    if row is None:
        return False
    return bool(row["granted"]) and row.get("revoked_at") is None


def transcript_consent_kind(circle_role: Optional[str]) -> Optional[str]:
    """The consent kind that would let somebody with `circle_role` read transcripts."""
    if circle_role is None:
        return None
    return CONSENT_FOR_ROLE.get(circle_role)


def may_read_transcripts(circle_role: Optional[str], records: Iterable[Mapping]) -> bool:
    kind = transcript_consent_kind(circle_role)
    if kind is None:
        return False
    return consent_granted(records, kind)


def retention_months(records: Iterable[Mapping]) -> int:
    row = effective_consents(records).get("data_retention")
    if row is None or row.get("revoked_at") is not None or not row.get("retention_months"):
        return DEFAULT_RETENTION_MONTHS
    return int(row["retention_months"])
