"""The lock files name their recipe's input, not one machine's scratch directory.

`requirements.lock` is resolved from a filtered copy of `requirements.txt`, and
uv writes that copy's path into every `# via -r <path>` annotation. The lock's
header gives the recipe, which puts the copy at `/tmp/req-pypi.txt`. Earlier
regenerations put it in a private scratch directory instead, so 56 annotations
named that directory, and every later regeneration carried them forward.

pip ignores comments, so nothing installed differently. But the lock no longer
matched its own recipe: running the recipe exactly as written changed 56 lines.
These tests read the input from each lock's own recipe line, so they follow
the recipe if it changes.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

IRA_ROOT = Path(__file__).resolve().parents[1]

#: The two committed locks, and the file whose text carries each one's recipe.
#: `requirements.lock`'s header is kept in `requirements.lock.header` and
#: re-applied on every regeneration; the torch lock carries its own.
LOCKS = {
    "requirements.lock": "requirements.lock.header",
    "requirements-torch.lock": "requirements-torch.lock",
}

_RECIPE_INPUT = re.compile(r"uv pip compile\s+(\S+)")
#: `# via -r <path>` and the multi-parent form `#   -r <path>`.
_ANNOTATED_SOURCE = re.compile(r"^\s*#\s+(?:via\s+)?-r\s+(\S+)\s*$", re.M)
#: A path that belongs to one machine or one user.
_PRIVATE_PATH = re.compile(r"/home/|/root/|/Users/|scratchpad|[A-Za-z]:\\")


def _recipe_input(recipe_file: str) -> str:
    found = set(_RECIPE_INPUT.findall((IRA_ROOT / recipe_file).read_text(encoding="utf-8")))
    assert len(found) == 1, f"{recipe_file}: expected one `uv pip compile <input>`, found {found}"
    return found.pop()


def _comment_lines(lock: str) -> list[str]:
    text = (IRA_ROOT / lock).read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.lstrip().startswith("#")]


def test_the_annotation_pattern_reads_both_forms_and_nothing_else():
    """The pattern is checked here first, so the tests below cannot pass by matching nothing."""
    sample = "\n".join([
        "    # via -r /tmp/req-pypi.txt",
        "    #   -r /tmp/req-pypi.txt",
        "    # via aiohttp",
        "    #   httpx",
        "#   uv pip compile /tmp/req-pypi.txt --generate-hashes",
    ])
    assert _ANNOTATED_SOURCE.findall(sample) == ["/tmp/req-pypi.txt", "/tmp/req-pypi.txt"]
    assert _PRIVATE_PATH.search("    # via -r /home/someone/scratchpad/req-pypi.txt")
    assert not _PRIVATE_PATH.search("    # via -r /tmp/req-pypi.txt")


@pytest.mark.parametrize("lock,recipe_file", sorted(LOCKS.items()))
def test_every_source_annotation_names_the_recipes_input(lock, recipe_file):
    expected = _recipe_input(recipe_file)
    sources = _ANNOTATED_SOURCE.findall("\n".join(_comment_lines(lock)))
    assert sources, f"{lock}: no `-r` annotations found; the pattern or the lock changed"
    wrong = sorted(set(sources) - {expected})
    assert not wrong, (
        f"{lock}: {sum(s != expected for s in sources)} annotation(s) name {wrong}, "
        f"but the recipe resolves {expected!r}. Regenerate with the recipe exactly as "
        f"the header gives it.")


@pytest.mark.parametrize("lock", sorted(LOCKS))
def test_no_comment_names_a_private_directory(lock):
    hits = [line.strip() for line in _comment_lines(lock) if _PRIVATE_PATH.search(line)]
    assert not hits, f"{lock}: {len(hits)} comment line(s) name a private path, e.g. {hits[0]!r}"
