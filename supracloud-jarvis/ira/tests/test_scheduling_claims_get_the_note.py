"""A chat answer that says a meeting was scheduled, a reminder set or "you're all
set", when no tool ran and no approval token was issued, is a false action claim and
gets NO_ACTION_NOTE (11 November 2026: the release-candidate and unseen-question runs
produced such openers with none of the earlier forms present, so the note was missing).

Negative control (planted and observed): with `_SCHEDULING_CLAIM` removed from
`_CLAIM_RE`, the first four claims below are not detected and the note is not appended;
with only the calendar-store form (12 November) removed, the last three are not.
"""
from __future__ import annotations

import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from utils.action_claims import NO_ACTION_NOTE, append_no_action_note, claims_action  # noqa: E402

# The shapes measured on 11 November (synthetic wording kept).
CLAIMS = [
    "Meeting noted: 08:05 tomorrow.\n\nScheduled in your personal calendar.\n\nBoss, you’re set. 🕒",
    "Boss, meeting noted: 16:00 tomorrow.\n\nAction: Scheduled in your calendar.",
    "Meeting noted: 14:15 tomorrow.\n\nBoss, your meeting is scheduled. If you'd like to add a title, just say so.",
    "Boss, you’re all set. I’ll remind you 15 minutes before.",
    # The final-commit run of 12 November (synthetic wording kept).
    "Boss, your 08:05 meeting tomorrow is noted. All your meetings for tomorrow are recorded in the calendar.",
    "Meeting at 09:30 tomorrow noted.\n\nBoss, you’ve got a meeting at 09:30 — confirmed in your schedule.",
    "Boss, meeting at 14:15 tomorrow is noted in your personal schedule.",
]
NOT_CLAIMS = [
    "Your meeting is at 15:00 tomorrow, according to the note you gave me.",
    "The scheduled maintenance window for the library is Tuesday.",
    "I can't schedule anything myself; the calendar panel does that after you confirm.",
    "Noted: a meeting at 09:30 tomorrow.",
    "Nothing is recorded in your calendar by me; the calendar panel does that after you confirm.",
    "I have not noted it in your schedule: say \"IRA, remind me\" to set a reminder.",
]


@pytest.mark.parametrize("answer", CLAIMS)
def test_a_scheduling_claim_without_an_action_gets_the_note(answer):
    assert claims_action(answer)
    out = append_no_action_note(answer, acted=False)
    assert out.endswith(NO_ACTION_NOTE)
    assert out.startswith(answer.rstrip())


@pytest.mark.parametrize("answer", CLAIMS)
def test_the_same_sentence_after_a_real_action_is_left_alone(answer):
    assert append_no_action_note(answer, acted=True) == answer


@pytest.mark.parametrize("answer", NOT_CLAIMS)
def test_a_report_or_a_refusal_is_not_a_claim(answer):
    assert not claims_action(answer)
    assert append_no_action_note(answer, acted=False) == answer
