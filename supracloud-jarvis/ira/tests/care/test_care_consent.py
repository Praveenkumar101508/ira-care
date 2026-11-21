"""Who may read transcripts (care/consent.py)."""
from __future__ import annotations

import datetime as dt

import pytest

from care import consent


def _row(kind, granted, at, revoked=None, months=None):
    return {"kind": kind, "granted": granted,
            "recorded_at": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=at),
            "revoked_at": revoked, "retention_months": months}


def test_no_consent_means_no_transcripts_for_anyone():
    for role in ("family", "carer", "clinician", "care_organisation"):
        assert not consent.may_read_transcripts(role, [])


def test_not_in_the_circle_means_no_transcripts_even_with_every_consent():
    rows = [_row(k, True, 1) for k in consent.CONSENT_KINDS]
    assert not consent.may_read_transcripts(None, rows)
    assert not consent.may_read_transcripts("administrator", rows)


@pytest.mark.parametrize("role,kind", [
    ("family", "transcripts_family"),
    ("carer", "transcripts_carer"),
    ("clinician", "transcripts_carer"),
    ("care_organisation", "transcripts_organisation"),
])
def test_each_role_needs_its_own_consent_kind(role, kind):
    assert consent.may_read_transcripts(role, [_row(kind, True, 1)])
    others = [_row(k, True, 1) for k in consent.CONSENT_KINDS if k != kind]
    assert not consent.may_read_transcripts(role, others)


def test_the_latest_record_wins():
    rows = [_row("transcripts_family", True, 1), _row("transcripts_family", False, 5)]
    assert not consent.may_read_transcripts("family", rows)
    rows.append(_row("transcripts_family", True, 9))
    assert consent.may_read_transcripts("family", rows)


def test_a_revoked_grant_is_not_a_grant():
    now = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    assert not consent.may_read_transcripts("family", [_row("transcripts_family", True, 1, revoked=now)])


def test_retention_defaults_to_twelve_months_and_follows_the_latest_record():
    assert consent.retention_months([]) == 12
    rows = [_row("data_retention", True, 1, months=24)]
    assert consent.retention_months(rows) == 24
    rows.append(_row("data_retention", True, 2, months=6))
    assert consent.retention_months(rows) == 6
    rows.append(_row("data_retention", True, 3, revoked=dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc), months=36))
    assert consent.retention_months(rows) == 12
