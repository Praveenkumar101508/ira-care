"""P0 performance (6 November 2026): a simple question gets a short answer by default.

Acceptance run 4 attributed every slow chat, follow-up and memory answer on the 4 vCPU
Linux VM to generation length at 6-7 tokens a second: "what is my bank PIN? If you were
never told, say so." took 16-26 s for 100-166 tokens of boilerplate. One rule,
`BRIEF_ANSWER_RULE`, is carried once by every agent's system prompt on both chat paths:
the graph path (the researcher's and IRA's own prompts) and the stream route's assembly.

Extended 11 November 2026 (follow-up p95): the dataset's openers are statements with a
brevity instruction ("I have a meeting at 08:05 tomorrow. Note it.", "Say OK.") and the
fast model padded them with invented rooms, badges and calendar alerts (33-296 tokens).
The rule now names an acknowledgement and forbids invented details; it stays under 70
words and is still appended exactly once on each of the three sites that carry it.
"""
from __future__ import annotations

import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from utils.answer_length import BRIEF_ANSWER_RULE, with_brief_answer_rule  # noqa: E402


def test_the_rule_is_one_short_paragraph_that_keeps_full_answers_available():
    assert len(BRIEF_ANSWER_RULE.split()) < 70
    assert BRIEF_ANSWER_RULE.count("\n") == 0
    for kept in ("plan", "list", "explanation", "comparison", "code", "document", "in detail"):
        assert kept in BRIEF_ANSWER_RULE, kept
    for banned in ("no preamble", "no headings", "no reasoning trail", "no offer of further help"):
        assert banned in BRIEF_ANSWER_RULE, banned


def test_the_rule_covers_an_acknowledgement_and_forbids_invented_details():
    """A note or an instruction ("Note it", "Say OK") is acknowledged in one line, and the
    answer carries nothing the user did not state (no room, badge, alert or calendar entry)."""
    rule = BRIEF_ANSWER_RULE.lower()
    assert "acknowledgement" in rule
    assert "no invented details" in rule
    assert len(BRIEF_ANSWER_RULE.split()) < 70


def test_the_rule_is_appended_once_and_never_twice():
    once = with_brief_answer_rule("You are IRA.")
    assert once.startswith("You are IRA.") and once.endswith(BRIEF_ANSWER_RULE)
    assert with_brief_answer_rule(once) == once
    assert once.count(BRIEF_ANSWER_RULE) == 1


@pytest.mark.parametrize("agent", ["conversational", "researcher", "tutor", "creator", "executor",
                                   "security", "website", "career", "digital"])
def test_every_agents_prompt_on_the_stream_route_carries_the_rule_once(agent):
    import api.routes.chat as chatmod

    prompt = chatmod._get_agent_system_prompt(agent)
    assert prompt.count(BRIEF_ANSWER_RULE) == 1, agent


def test_the_graph_paths_own_prompts_carry_the_rule_once():
    """POST /api/v1/chat runs the agents' own prompts, not the stream route's assembly."""
    from agents.conversational import IRA_SYSTEM
    from agents.researcher import _SYSTEM as RESEARCHER_SYSTEM

    assert IRA_SYSTEM.count(BRIEF_ANSWER_RULE) == 1
    assert RESEARCHER_SYSTEM.count(BRIEF_ANSWER_RULE) == 1
    # The researcher's structure and next-steps guidance now applies to a full answer only.
    assert "Structure a full answer" in RESEARCHER_SYSTEM and "End a full answer" in RESEARCHER_SYSTEM


def test_the_rule_is_idempotent_on_each_of_the_three_sites_that_carry_it():
    """The stream route appends it with `with_brief_answer_rule`; `_GROK_BASE` and the
    researcher's `_SYSTEM` embed it. Applying the helper to any of the three changes nothing."""
    import api.routes.chat as chatmod
    from agents.grok_personality import build_grok_system_prompt
    from agents.researcher import _SYSTEM as RESEARCHER_SYSTEM

    for site, prompt in (("stream route", chatmod._get_agent_system_prompt("conversational")),
                         ("_GROK_BASE", build_grok_system_prompt()),
                         ("researcher", RESEARCHER_SYSTEM)):
        assert prompt.count(BRIEF_ANSWER_RULE) == 1, site
        assert with_brief_answer_rule(prompt) == prompt, site


def test_the_voice_suffix_still_follows_the_rule():
    import api.routes.chat as chatmod

    prompt = chatmod._get_agent_system_prompt("conversational", is_voice=True)
    assert prompt.count(BRIEF_ANSWER_RULE) == 1 and prompt.index(BRIEF_ANSWER_RULE) < len(prompt) - len(BRIEF_ANSWER_RULE)
