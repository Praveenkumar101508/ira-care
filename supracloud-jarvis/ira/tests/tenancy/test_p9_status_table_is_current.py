"""P9-05 — the phase status table may not contradict the evidence beside it.

`docs/cloud/CLOUD_PHASE_STATUS.md` carried, until this batch, six rows that
described a repository which had stopped existing several phases earlier:

    | 2  | Identity              | BLOCKED     | blocked until Phase 3C is VERIFIED |
    | 3B | Tenancy migration     | NOT_STARTED | **next.** 35 tables need schema change |
    | 3C | Isolation verification| NOT_STARTED | **the gate on registration** |
    | 8  | Memory and RAG        | IMPLEMENTED | single-tenant-shaped |

Phase 2 shipped eleven batches. 3B ran seven. 3C found ten things and fixed
ten. Phase 8 was the live inference path, which the `## Phase 8` section of the
*same file* said correctly while the table said something else. The evidence
that contradicted every one of those rows was in `docs/cloud/evidence/` the
whole time.

That is worse than an out-of-date file, because the table is the thing people
quote. A reader deciding whether registration may ship reads the row, not the
eleven batch reports. **Two control documents disagreeing is worse than either
being wrong**, and here the two were the same document.

── What this guard checks, and what it deliberately does not ──────────────

It reads STRUCTURE, not prose. Three properties, each mechanical:

  1. A phase with a closure report under `evidence/` may not be `NOT_STARTED`.
     That is the exact contradiction this batch found, generalised — and it
     stays true as later phases add reports, so it is not a guard pinned to
     today's defect.
  2. The table and the per-phase sections of the same file must not name
     different scopes for the same number.
  3. Every non-`NOT_STARTED` row must cite something: a file under `evidence/`,
     a migration number, a test path or a module. A status with no artefact
     behind it is an opinion.

It does NOT check that a status is *correct* — no test can. `VERIFIED` versus
`IMPLEMENTED` is a judgement, and a guard that tried to make it would be
asserting its own opinion in a file whose whole purpose is to record somebody
else's. What it can do is make a row that has drifted from the artefacts
impossible to leave lying there.
"""
from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[4]
STATUS = REPO / "docs" / "cloud" / "CLOUD_PHASE_STATUS.md"
EVIDENCE = REPO / "docs" / "cloud" / "evidence"

if not STATUS.exists():
    pytest.skip(f"{STATUS} not found", allow_module_level=True)

STATUSES = {"NOT_STARTED", "IN_PROGRESS", "BLOCKED", "IMPLEMENTED", "VERIFIED",
            "DEFERRED", "ENGINEERING COMPLETE"}

#: `| 3B | Tenancy migration | **`VERIFIED`** | ... |`
ROW = re.compile(r"^\|\s*(\d+[A-Za-z]?)\s*\|([^|]+)\|([^|]+)\|(.*)\|\s*$")


def _rows() -> list[tuple[str, str, str, str]]:
    out = []
    for line in STATUS.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if not match:
            continue
        number, name, status, notes = (g.strip() for g in match.groups())
        cleaned = status.replace("*", "").replace("`", "").strip()
        if cleaned.split(" (")[0] in STATUSES:
            out.append((number, name, cleaned, notes))
    return out


def test_the_table_was_found_and_parsed():
    """A regex that matches nothing makes every test below vacuous — the
    failure mode Phase 3B's own cross-tenant sweep had, where a search whose
    purpose was finding unprotected data missed half of it by spelling."""
    rows = _rows()
    assert len(rows) >= 15, (
        f"parsed only {len(rows)} status rows; the table format changed and "
        f"these guards stopped reading it")
    assert {r[0] for r in rows} >= {"1", "2", "3A", "3B", "3C", "9", "10"}


def test_no_phase_with_a_closure_report_is_marked_not_started():
    """The contradiction this batch found, generalised.

    A closure report is written at the end of a phase. A phase cannot both have
    one and not have begun, so this needs no judgement about which status is
    right — only that `NOT_STARTED` is not it.
    """
    reported = set()
    for report in EVIDENCE.glob("PHASE_*CLOSURE*.md"):
        found = re.match(r"PHASE_(\d+[A-Za-z]?)_", report.name)
        if found:
            reported.add(found.group(1))
    assert reported, "no closure reports found — this guard would be vacuous"

    offenders = [(n, s) for n, _, s, _ in _rows()
                 if n in reported and s == "NOT_STARTED"]
    assert offenders == [], (
        f"{offenders} are marked NOT_STARTED and have a closure report under "
        f"docs/cloud/evidence/. The table is the thing people quote; the "
        f"reports are the thing that is true.")


def test_every_started_phase_cites_an_artefact():
    """A status with nothing behind it is an opinion.

    Accepted as a citation: a file under `evidence/`, a migration number, a
    test path, any `.py` / `.md` / `.sql` / `.sh` file, a module path, or a
    batch identifier. Deliberately generous — the check is that SOMETHING was
    named, not that a particular form was used.

    The first version of this pattern accepted `.py` and not `.md`, and flagged
    a row that cited a document by name. The guard was wrong and the row was
    right, so the guard changed: a check that rejects a real citation trains
    people to weaken it.
    """
    citation = re.compile(
        r"evidence/|\bP\d+-\d+|\b0\d\d\b|tests?/|\.(?:py|md|sql|sh)\b"
        r"|`[\w.-]+/[\w./-]+`|batch",
        re.IGNORECASE)
    missing = [(n, s) for n, _, s, notes in _rows()
               if s != "NOT_STARTED" and not citation.search(notes)]
    assert missing == [], (
        f"{missing} claim progress and name nothing that could be checked")


def test_the_table_and_the_sections_agree_about_phase_8():
    """The specific disagreement, kept as a regression.

    Row 8 said "Memory and RAG" while `## Phase 8` in the same file said "The
    live inference path". Whichever were right, a reader had no way to tell —
    and both were in front of them.
    """
    text = STATUS.read_text(encoding="utf-8")
    sections = dict(re.findall(r"^## Phase (\d+[A-Za-z]?) — (.+?)$", text,
                               re.MULTILINE))
    if "8" not in sections:
        pytest.skip("no '## Phase 8' section to compare the row against")

    row = next((r for r in _rows() if r[0] == "8"), None)
    assert row is not None
    section_words = set(re.findall(r"[a-z]{4,}", sections["8"].lower()))
    row_words = set(re.findall(r"[a-z]{4,}", row[1].lower()))
    assert section_words & row_words, (
        f"the Phase 8 row calls it {row[1]!r} and the Phase 8 section calls it "
        f"{sections['8']!r}. Two control documents disagreeing is worse than "
        f"either being wrong, and these two are the same document.")


def test_a_displaced_scope_was_not_deleted():
    """When Phase 8's number was reused, "Memory and RAG" had to go somewhere.

    Overwriting the row would have made an outstanding piece of work vanish
    from the only place that lists it — which is how scope disappears without
    anyone deciding to drop it.
    """
    text = STATUS.read_text(encoding="utf-8").lower()
    assert "memory and rag" in text, (
        "the roadmap's Memory and RAG scope is no longer named anywhere in the "
        "status document. It is still outstanding; losing it from the table is "
        "not the same as finishing it.")
