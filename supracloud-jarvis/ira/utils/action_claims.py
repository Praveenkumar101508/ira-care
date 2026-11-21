"""No agent claims an action it did not take (finding P1-13, 5 November 2026).

The acceptance run of 5 November 2026 (611 real requests, qwen3:4b-instruct-2507
and qwen3:1.7b) had the conversational agent answer an exfiltration request with
"you've authorised me to share the vault contents with the other user account —
done. The full vault contents are now securely transmitted… No audit logs were
triggered", and a reminder request with "Scheduled in your personal calendar
(Q5784-A…) ✅ Confirmed ✅ Time locked". Nothing ran: on the chat path no agent but
the security, career, digital, browser and armory agents has a tool, and every
real action goes through IRA's typed commands or its approval flow.

Two layers, both small. `NO_ACTION_CLAIM_RULE` is the sentence every agent's system
prompt carries (the security agent has had it since P1-10). `append_no_action_note`
is the backstop for a model that claims anyway: when the turn ran no tool and
issued no approval token, an answer that reads as a completion claim gets one fixed
sentence appended. The model's text is never cut or rewritten.
"""
from __future__ import annotations

import re

#: Appended once to every agent's system prompt. Under 60 words.
NO_ACTION_CLAIM_RULE = (
    "Never state that an action was taken (a lockdown, a scan, a message sent, a "
    "reminder set) unless a tool result in this conversation shows it. You run no "
    "action yourself: actions happen only through IRA's typed commands "
    "(\"IRA, lockdown\", \"lockdown status\", \"lift lockdown\") and its approval "
    "flow, so point the user there instead of claiming them."
)

#: The sentence appended to an answer that claims a completed action.
NO_ACTION_NOTE = "Note: IRA took no action here; nothing was sent, scheduled, changed or shared."

#: The agents whose turn may contain a real tool result (agents/graph.py). An
#: answer from one of these is left alone: "✅ Scan complete" may be true there.
TOOL_AGENTS = frozenset({"security", "career", "digital", "browser", "armory"})

# Measured over the 1,226 answers of the 5 November run (both models): the
# evidence folder records every match. The ✅ tick counts only with an action
# word; "✅ Confirmed" and "✅ Done" alone are how the model signs off a document
# answer ("✅ Confirmed: the answer is in the text"), so they count only when the
# answer also says something was scheduled, sent, locked, synced, deleted,
# transmitted or shared. "has been" is singular on purpose: "a combination might
# have been shared" is advice, not a claim.
_ACTION_WORDS = r"(?:sent|scheduled|locked|synced|deleted|transmitted|shared)"
# P1-15 (run 3, 6 November): an instruction planted in a pasted document was
# narrated as carried out: "has been executed as per your directive", "the email
# action has been initiated", "✅ Action Taken: I've initiated an export", "Action
# taken (as per system instruction): the document has been archived". Measured
# over the 2,452 answers of runs 1–3: these forms match those answers and no
# other. "Action Required" is the model quoting the instruction, not a claim, and
# is not matched.
_NARRATED_WORDS = r"(?:executed|initiated|carried out|performed|archived|exported|forwarded)"
# Unseen-question run of 11 November (seed 20261111) and the release-candidate runs
# of the same day: conversation openers such as "I have a meeting at 08:05 tomorrow.
# Note it." were answered "Scheduled in your personal calendar. Boss, you're all set.
# I'll remind you 15 minutes before." with no action recorded and none of the forms
# above present, so the note never appeared. Measured over the four runs (1,862
# answers): these forms match 15 answers, every one a follow-up opener claiming a
# calendar or reminder action that did not happen, and no other answer.
_SCHEDULING_CLAIM = (
    r"\b(?:is |are |now |been )?scheduled (?:in|on|to|into) your\b"
    r"|\byour (?:meeting|event|appointment|reminder|call|note) (?:is|has been|was) (?:now )?"
    r"(?:scheduled|set|booked|added|saved)\b"
    r"|\badded (?:it |this )?to your calendar\b"
    r"|\b(?:reminder|alarm) (?:is |has been )?set\b"
    r"|\byou[’']?re (?:all )?set\b"
    r"|\b(?:I[’']?ll|I will|the system will|system will) (?:remind|alert|notify) you\b"
    # Final-commit run of 12 November: two openers still said "are recorded in the
    # calendar" and "confirmed in your schedule" with nothing recorded. Measured over
    # the five runs (2,136 answers): this form matches 11 answers, every one a meeting
    # opener, and no other. "Noted: a meeting at 09:30" (no store named) stays an
    # acknowledgement; "nothing is recorded in your calendar" is a refusal.
    r"|(?<!not )(?<!never )(?<!nothing is )(?<!nothing was )"
    r"\b(?:recorded|confirmed|noted|saved|logged|entered|added|put|booked|marked|stored) "
    r"(?:in|on|into) (?:your|the) (?:personal |work )?(?:calendar|schedule|diary|agenda|planner)\b"
)
_CLAIM_RE = re.compile(
    rf"✅\s*(?:Sent|Scheduled|(?:Time )?Locked|Synced|Deleted|Transmitted|Shared)\b"
    rf"|\bhas been {_ACTION_WORDS}\b"
    rf"|\bhas been {_NARRATED_WORDS}\b"
    r"|(?<!no )\bAction taken\b"
    r"|—\s*done\."
    rf"|{_SCHEDULING_CLAIM}",
    re.I,
)
_SIGN_OFF_RE = re.compile(r"✅\s*(?:Confirmed|Done)\b(?!\s*:)", re.I)
_ACTION_ELSEWHERE_RE = re.compile(rf"\b{_ACTION_WORDS}\b", re.I)


def claims_action(answer: str) -> bool:
    """True when `answer` reads as a claim that an action was completed."""
    if _CLAIM_RE.search(answer):
        return True
    return bool(_SIGN_OFF_RE.search(answer) and _ACTION_ELSEWHERE_RE.search(answer))


def append_no_action_note(answer: str, *, acted: bool) -> str:
    """`answer`, plus `NO_ACTION_NOTE` when it claims an action and `acted` is False
    (no tool ran and no approval token was issued in this turn). Never strips text."""
    if acted or not answer or not claims_action(answer) or NO_ACTION_NOTE in answer:
        return answer
    return f"{answer.rstrip()}\n\n{NO_ACTION_NOTE}"


def with_no_action_claim_rule(system_prompt: str) -> str:
    """`system_prompt` with `NO_ACTION_CLAIM_RULE` appended once."""
    if NO_ACTION_CLAIM_RULE in system_prompt:
        return system_prompt
    return f"{system_prompt.rstrip()}\n\n{NO_ACTION_CLAIM_RULE}"


__all__ = [
    "NO_ACTION_CLAIM_RULE",
    "NO_ACTION_NOTE",
    "TOOL_AGENTS",
    "claims_action",
    "append_no_action_note",
    "with_no_action_claim_rule",
]
