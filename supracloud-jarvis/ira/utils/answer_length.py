"""Answer length by default (P0 performance, 6 November 2026).

On the 4 vCPU Linux test VM the chat model generates 6-7 tokens a second. Acceptance
run 4 attributed every slow chat, follow-up and memory answer to generation length:
"what is my bank PIN? If you were never told, say so." was answered in 100-166 tokens
of boilerplate at 16-26 s, "summarise in one sentence" grew a "Critical Insight /
Final reasoning trail" of 400 tokens. Capping those answers near 40 tokens would put
chat (12.0 -> ~9 s p95) and memory (15.7 -> ~8 s p95) inside the owner's targets.

One sentence, appended once to every agent's system prompt the way the quoted-material
and no-action rules are. It changes the default, not the ceiling: a plan, a list, an
explanation, code or a document is still answered in full when asked for.

Extended on 11 November 2026 (follow-up p95): the acceptance dataset's openers are
statements with a brevity instruction ("I have a meeting at 08:05 tomorrow. Note it.",
"Say OK.") and the fast model answered them with 33-296 tokens of invented logistics
(a room, a badge, a calendar alert). An acknowledgement was not in the rule's list and
invented detail was not forbidden, so the rule now names both. Still under 70 words,
still one paragraph, still appended once.
"""

BRIEF_ANSWER_RULE = (
    "Answer length: a simple question, fact, recall or yes/no gets one to three "
    "sentences; a note or instruction gets a one-line acknowledgement; nothing else: "
    "no preamble, no headings, no reasoning trail, no restating the question, no offer "
    "of further help, no invented details the user did not state. Give full detail "
    "only when asked for a plan, a list, an explanation, a comparison, code, a "
    "document or \"in detail\"."
)


def with_brief_answer_rule(system_prompt: str) -> str:
    """`system_prompt` with `BRIEF_ANSWER_RULE` appended once."""
    if BRIEF_ANSWER_RULE in system_prompt:
        return system_prompt
    return f"{system_prompt.rstrip()}\n\n{BRIEF_ANSWER_RULE}"


__all__ = ["BRIEF_ANSWER_RULE", "with_brief_answer_rule"]
