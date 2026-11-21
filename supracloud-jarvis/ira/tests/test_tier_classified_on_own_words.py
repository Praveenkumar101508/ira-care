"""The model tier and the answer policy are chosen on the user's own words.

Every pasted document is over the router's 120-word line
(``reasoning.model_router.classify_task``), so classifying the whole message
sent a one-sentence question above a 2,000-word document to the reasoning
tier, and ``_compose_quality_layer`` asked the model for a "FINAL reasoning
trail" nobody wanted (27-49 s of generation on the document benchmark). The
layer now classifies ``strip_pasted_material(message)`` behind
``IRA_TIER_CLASSIFY_OWN_WORDS`` (default on).

Negative control, planted and seen to fail on 11 November 2026: with
``classify_task(message, ...)`` in place of the stripped text, the two
"not a reasoning task" cases and the two "identical to the question alone"
cases failed (4 failed, 4 passed), the composed prompt carrying
"step-by-step FINAL reasoning trail". The file was restored with
``git checkout``; the commit after the change records the run.
"""
from __future__ import annotations

import pytest

from config import get_settings
from reasoning.model_profiles import ModelMode
from reasoning.model_router import classify_task
from utils.prompt_safety import strip_pasted_material

QUESTION = (
    "Who prepared this document and what is the total amount? "
    "Answer in one sentence. Do not add anything else."
)
# 2,000 words of a plain business letter: no reasoning or coding vocabulary,
# so the only reason it could reach the reasoning tier is its length.
DOCUMENT = " ".join(
    "The quarterly invoice lists the items delivered to the warehouse"
    .split() * 200
)
assert len(DOCUMENT.split()) == 2000

PASTED_AFTER_RULE = f"{QUESTION}\n\n---\n{DOCUMENT}"
PASTED_IN_FENCE = f"{QUESTION}\n\n```\n{DOCUMENT}\n```"
LONG_PLAIN_QUESTION = " ".join(["please"] * 130)
TRAIL = "FINAL reasoning trail"
BASE = "You are IRA."


@pytest.fixture
def quality_layer(monkeypatch):
    """`_compose_quality_layer` with the router on and the flag at its default."""
    monkeypatch.delenv("IRA_TIER_CLASSIFY_OWN_WORDS", raising=False)
    monkeypatch.setenv("IRA_USE_MODEL_ROUTER", "true")
    get_settings.cache_clear()
    from api.routes import chat as chatmod
    yield chatmod._compose_quality_layer
    get_settings.cache_clear()


def _tier(message: str, **kw) -> ModelMode:
    return classify_task(message, **kw).mode


@pytest.mark.parametrize("message", [PASTED_AFTER_RULE, PASTED_IN_FENCE], ids=["rule", "fence"])
def test_a_short_question_over_a_pasted_document_is_not_a_reasoning_task(quality_layer, message):
    assert len(message.split()) > 120  # the whole message would trip the length rule
    own_words = strip_pasted_material(message)
    assert own_words.strip() == QUESTION
    assert _tier(own_words) in (ModelMode.LOCAL_MAIN, ModelMode.LOCAL_FAST)
    composed = quality_layer(BASE, message)
    assert composed.startswith(BASE) and "## Model-tier guidance" in composed
    assert TRAIL not in composed


@pytest.mark.parametrize("message", [PASTED_AFTER_RULE, PASTED_IN_FENCE], ids=["rule", "fence"])
def test_the_same_question_alone_classifies_identically(quality_layer, message):
    with_doc = quality_layer(BASE, message)
    alone = quality_layer(BASE, QUESTION)
    assert with_doc == alone
    assert _tier(strip_pasted_material(message)) == _tier(QUESTION)


def test_a_long_plain_question_still_reaches_the_reasoning_tier(quality_layer):
    assert _tier(LONG_PLAIN_QUESTION) == ModelMode.LOCAL_REASONING
    assert TRAIL in quality_layer(BASE, LONG_PLAIN_QUESTION)


def test_think_mode_still_forces_the_reasoning_tier(quality_layer):
    assert _tier(QUESTION, think_mode=True) == ModelMode.LOCAL_REASONING
    assert TRAIL in quality_layer(BASE, PASTED_AFTER_RULE, think_mode=True)
    assert TRAIL in quality_layer(BASE, QUESTION, think_mode=True)


def test_the_flag_off_restores_classification_on_the_whole_message(monkeypatch):
    monkeypatch.setenv("IRA_USE_MODEL_ROUTER", "true")
    monkeypatch.setenv("IRA_TIER_CLASSIFY_OWN_WORDS", "false")
    get_settings.cache_clear()
    try:
        assert get_settings().ira_tier_classify_own_words is False
        from api.routes import chat as chatmod
        assert TRAIL in chatmod._compose_quality_layer(BASE, PASTED_AFTER_RULE)
        assert _tier(PASTED_AFTER_RULE) == ModelMode.LOCAL_REASONING
    finally:
        get_settings.cache_clear()


def test_the_flag_defaults_on():
    from config import Settings

    s = Settings(ira_secret_key="x", ira_admin_password="x", postgres_password="x", redis_password="x")
    assert s.ira_tier_classify_own_words is True
