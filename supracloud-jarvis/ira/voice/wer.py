"""Word error rate, for measuring how well speech is recognised (requirement F8.b).

WER = (substitutions + deletions + insertions) / words in the reference, counted on the
minimum-edit alignment of the two word sequences after both are normalised the same way.
Pure Python, no dependencies, so the same scoring is used for every recogniser: the server's
faster-whisper (`voice/stt.py`), and later the watch's and phone's platform recognisers,
whose transcripts can be scored from a file without running anything here.

Normalisation is deliberately simple and stated, because a WER figure means nothing without
it: lower case, digits kept, every character that is not a letter, digit or apostrophe becomes
a space, apostrophes are dropped ("don't" and "dont" are the same word), whitespace collapses.
Numbers are NOT spelled out, so "3" against "three" counts as an error; that is reported, not
hidden.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_NOT_WORD = re.compile(r"[^\w']+", re.UNICODE)


def normalise(text: str) -> list[str]:
    """The words of `text` as scored: see the module docstring."""
    text = unicodedata.normalize("NFKC", text or "").lower().replace("_", " ")
    text = _NOT_WORD.sub(" ", text).replace("'", "")
    return text.split()


@dataclass(frozen=True)
class Counts:
    """Edit counts for one or more utterances. `words` is the reference length."""

    words: int = 0
    substitutions: int = 0
    deletions: int = 0
    insertions: int = 0

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def wer(self) -> float | None:
        """None when there is no reference word: a rate over nothing is not zero."""
        return None if self.words == 0 else self.errors / self.words

    def __add__(self, other: "Counts") -> "Counts":
        return Counts(self.words + other.words, self.substitutions + other.substitutions,
                      self.deletions + other.deletions, self.insertions + other.insertions)


def align(reference: list[str], hypothesis: list[str]) -> Counts:
    """Minimum-edit alignment of two word lists (Levenshtein over words).

    Ties are broken towards substitutions, then deletions, then insertions, which is the usual
    convention; the total error count is the same whichever way a tie goes.
    """
    n, m = len(reference), len(hypothesis)
    # cost[i][j] = (errors, subs, dels, ins) aligning reference[:i] with hypothesis[:j]
    prev = [(j, 0, 0, j) for j in range(m + 1)]
    for i in range(1, n + 1):
        cur = [(i, 0, i, 0)] + [None] * m
        for j in range(1, m + 1):
            if reference[i - 1] == hypothesis[j - 1]:
                cur[j] = prev[j - 1]
                continue
            e, s, d, ins = prev[j - 1]
            sub = (e + 1, s + 1, d, ins)
            e, s, d, ins = prev[j]
            dele = (e + 1, s, d + 1, ins)
            e, s, d, ins = cur[j - 1]
            inse = (e + 1, s, d, ins + 1)
            cur[j] = min(sub, dele, inse, key=lambda t: t[0])
        prev = cur
    _, s, d, ins = prev[m]
    return Counts(words=n, substitutions=s, deletions=d, insertions=ins)


def score(reference: str, hypothesis: str) -> Counts:
    """Edit counts for one utterance, after normalising both sides."""
    return align(normalise(reference), normalise(hypothesis))


def corpus(pairs) -> Counts:
    """Pooled counts over (reference, hypothesis) pairs: the corpus WER is errors over all
    reference words, not the mean of per-utterance rates (which over-weights short ones)."""
    total = Counts()
    for reference, hypothesis in pairs:
        total = total + score(reference, hypothesis)
    return total


def by_group(rows) -> dict[str, Counts]:
    """Pooled counts per group from rows of (group, reference, hypothesis)."""
    out: dict[str, Counts] = {}
    for group, reference, hypothesis in rows:
        out[group] = out.get(group, Counts()) + score(reference, hypothesis)
    return out
