"""A generated PowerPoint deck keeps everything the model wrote.

The slide parser started a slide only at lines beginning "##". The model is
asked for "## Slide N: Title", and a local model often writes "# Slide N:",
"**Slide N: Title**" or plain "Slide N: Title" instead: each of those produced a
deck with a title slide and nothing else. Text before the first slide was
dropped, and a slide with more than eight bullets lost the rest. The parser is
a pure function now, so it is tested here without python-pptx installed.
"""
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import io

import pytest

from api.routes.document_create import _PPTX_BULLETS_PER_SLIDE, _generate_pptx, _parse_slides


def _titles(slides):
    return [t for t, _ in slides]


def _all_bullets(slides):
    return [b for _, bs in slides for b in bs]


def test_the_instructed_format():
    slides = _parse_slides("## Slide 1: Intro\n- point A\n- point B\n## Slide 2: Plan\n- step one", "Deck")
    assert slides == [("Intro", ["point A", "point B"]), ("Plan", ["step one"])]


@pytest.mark.parametrize("content", [
    "# Slide 1: Intro\n- point A\n- point B\n# Slide 2: Plan\n- step one",
    "**Slide 1: Intro**\n- point A\n- point B\n\n**Slide 2: Plan**\n- step one",
    "Slide 1: Intro\n- point A\n- point B\n\nSlide 2: Plan\n- step one",
    "Slide 1 - Intro\n- point A\n- point B\nSlide 2 - Plan\n- step one",
], ids=["H1 headings", "bold titles", "plain titles", "dash separator"])
def test_the_formats_a_local_model_writes_instead(content):
    slides = _parse_slides(content, "Deck")
    assert _titles(slides) == ["Intro", "Plan"]
    assert _all_bullets(slides) == ["point A", "point B", "step one"]


def test_text_before_the_first_slide_opens_the_deck():
    slides = _parse_slides("An overview the model wrote first.\n## Slide 1: Intro\n- point A", "Deck")
    assert slides[0] == ("Deck", ["An overview the model wrote first."])
    assert slides[1] == ("Intro", ["point A"])


def test_content_with_no_slide_markers_is_kept():
    slides = _parse_slides("- one\n- two\nA closing line.", "Deck")
    assert slides == [("Deck", ["one", "two", "A closing line."])]


def test_a_long_slide_continues_instead_of_dropping_bullets():
    n = _PPTX_BULLETS_PER_SLIDE + 3
    slides = _parse_slides("## Slide 1: Many\n" + "\n".join(f"- item {i}" for i in range(1, n + 1)), "Deck")
    assert _titles(slides) == ["Many", "Many (cont.)"]
    assert _all_bullets(slides) == [f"item {i}" for i in range(1, n + 1)]
    assert all(len(bs) <= _PPTX_BULLETS_PER_SLIDE for _, bs in slides)


def test_a_slide_with_only_a_title_is_kept():
    """A section divider and a closing "Thank you" are content. `main` kept a
    title-only slide only when it was the last one; the first version of this
    parser dropped every one, including that last slide."""
    slides = _parse_slides("## Part 1\n## Slide 1: Intro\n- point A\n## Thank you", "Deck")
    assert slides == [("Part 1", []), ("Intro", ["point A"]), ("Thank you", [])]


def test_a_bare_heading_repeating_the_deck_title_is_not_a_second_title_slide():
    """The deck already opens with a title slide; a model that restates the
    title as a heading should not produce a blank copy of it."""
    slides = _parse_slides("# Quarterly Review\n## Slide 1: Intro\n- point A", "Quarterly review")
    assert slides == [("Intro", ["point A"])]


def test_bold_markers_and_rules_do_not_reach_the_slides():
    slides = _parse_slides("## Slide 1: **Key** facts\n- **Revenue** grew\n---\n- costs fell", "Deck")
    assert slides == [("Key facts", ["Revenue grew", "costs fell"])]


def test_no_content_line_is_lost():
    content = ("Opening remark.\n# Slide 1: A\n- a1\n1. a2\n**Slide 2: B**\nb1\n"
               "Slide 3: C\n" + "\n".join(f"- c{i}" for i in range(12)))
    words = {"Opening remark.", "a1", "1. a2", "b1", *(f"c{i}" for i in range(12))}
    assert words <= set(_all_bullets(_parse_slides(content, "Deck")))


def test_the_rendered_deck_carries_every_slide():
    pptx = pytest.importorskip("pptx")
    content = "Overview first.\n# Slide 1: Intro\n- point A\n**Slide 2: Plan**\n- step one"
    deck = pptx.Presentation(io.BytesIO(_generate_pptx(content, "Deck")))
    texts = [" | ".join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame) for s in deck.slides]
    assert len(texts) == 4                       # the title slide, then three
    assert "Overview first." in texts[1]
    assert "Intro" in texts[2] and "point A" in texts[2]
    assert "Plan" in texts[3] and "step one" in texts[3]
