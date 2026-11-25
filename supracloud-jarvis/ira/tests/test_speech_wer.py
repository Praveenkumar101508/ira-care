"""Word error rate scoring (voice/wer.py, requirement F8.b): known cases worked by hand."""
from __future__ import annotations

import pytest

from voice.wer import Counts, align, by_group, corpus, normalise, score


def test_identical_text_has_no_errors():
    c = score("Did you take your tablets this morning?", "did you take your tablets this morning")
    assert c == Counts(words=7) and c.wer == 0.0


@pytest.mark.parametrize("ref, hyp, subs, dels, ins", [
    ("i am fine thank you", "i am find thank you", 1, 0, 0),
    ("i am fine thank you", "i am thank you", 0, 1, 0),
    ("i am fine thank you", "i am very fine thank you", 0, 0, 1),
    ("help me please", "", 0, 3, 0),
    ("yes", "yes yes yes", 0, 0, 2),
])
def test_known_alignments(ref, hyp, subs, dels, ins):
    c = score(ref, hyp)
    assert (c.substitutions, c.deletions, c.insertions) == (subs, dels, ins)
    assert c.wer == pytest.approx((subs + dels + ins) / len(ref.split()))


def test_a_tie_between_alignments_changes_the_split_never_the_total():
    """"the cat sat on the mat" -> "a cat sat on mat now" costs 3 either way: the->a, a deleted
    "the", an inserted "now"; or three substitutions. Only the total is defined, so the WER is
    the same whichever split the tie-break picks (substitutions first, as documented)."""
    c = score("the cat sat on the mat", "a cat sat on mat now")
    assert c.errors == 3 and c.wer == pytest.approx(0.5)
    assert c.substitutions == 3


def test_an_insertion_does_not_count_every_later_word_wrong():
    """The defect a position-by-position comparison has: one extra word at the start would
    shift every later word and score them all as errors. The alignment counts one insertion."""
    ref = "i took the blue tablet after breakfast"
    hyp = "well i took the blue tablet after breakfast"
    assert score(ref, hyp) == Counts(words=7, insertions=1)
    positional_errors = sum(r != h for r, h in zip(normalise(ref), normalise(hyp))) + 1
    assert positional_errors == 8  # what the naive method would report


def test_no_reference_words_gives_no_rate_not_zero():
    assert score("", "").wer is None
    assert score("", "hello").wer is None


def test_normalisation_is_case_punctuation_and_apostrophe_blind_but_keeps_numbers():
    assert normalise("Don't  WORRY, I'm OK!") == ["dont", "worry", "im", "ok"]
    assert score("I'm OK.", "im ok").wer == 0.0
    # Numbers are not spelled out: stated in the module, counted as an error here.
    assert score("three tablets", "3 tablets").substitutions == 1


def test_corpus_wer_pools_words_rather_than_averaging_rates():
    pairs = [("yes", "no"), ("i had porridge and tea for breakfast", "i had porridge and tea for breakfast")]
    pooled = corpus(pairs)
    assert pooled == Counts(words=8, substitutions=1)
    assert pooled.wer == pytest.approx(1 / 8)  # the mean of per-utterance rates would be 0.5


def test_groups_are_scored_separately():
    groups = by_group([("older", "yes", "yes"), ("younger", "no", "yes"), ("older", "help", "")])
    assert groups["older"] == Counts(words=2, deletions=1)
    assert groups["younger"] == Counts(words=1, substitutions=1)


def test_align_is_symmetric_in_total_errors():
    a, b = "a b c d e".split(), "a x c e f".split()
    assert align(a, b).errors == align(b, a).errors
