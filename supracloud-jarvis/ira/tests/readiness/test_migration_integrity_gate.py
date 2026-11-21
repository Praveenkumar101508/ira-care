"""The migration integrity gate, and the fact that something runs it.

`scripts/check_migration_integrity.py` says applied migration files are
immutable. It was written in Phase 3B, was correct, was documented, and was
**never wired into any workflow**. By the end of Phase 2 it failed for fifteen
migrations — `047` through `061` — and nothing reported it, because nothing ran
it. The script was not broken. It had no callers.

That is a named shape in this repository: *the correct mechanism existed,
documented, with no callers*. It has now cost a control twice, and the reason it
survives so long is that a checker with no runner is indistinguishable from a
passing one — both produce silence.

So this file tests two different things, and the second is the point:

  * the gate WORKS — a modified applied migration is detected, an unrecorded
    file is detected, and `--update` cannot be used to bless a change;
  * the gate is INVOKED — `.github/workflows/test.yml` runs it, and runs its
    self-test first.

A test of the first kind alone would have passed happily throughout the entire
window in which the control did nothing.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# tests/readiness/ -> tests/ -> ira/
IRA_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = IRA_ROOT.parents[1]
POSTGRES_DIR = REPO_ROOT / "supracloud-jarvis" / "postgres"
RECORD = REPO_ROOT / "docs" / "cloud" / "evidence" / "MIGRATION_FILE_HASHES.json"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"

GATE = "check_migration_integrity"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "scripts.check_migration_integrity", *args],
        cwd=IRA_ROOT, capture_output=True, text=True,
    )


@pytest.fixture
def record() -> dict:
    assert RECORD.is_file(), f"missing integrity record at {RECORD}"
    return json.loads(RECORD.read_text(encoding="utf-8"))


# ── The gate is invoked ──────────────────────────────────────────────────────



# ── The gate works ───────────────────────────────────────────────────────────


def test_the_record_covers_every_migration_in_the_chain(record):
    """The state the gate was in for the whole of Phase 2, asserted directly.

    `verify()` reports this too, and the test below runs it. This one names the
    files, so a failure says WHICH migration went unrecorded rather than only
    that something did.
    """
    on_disk = {p.name for p in POSTGRES_DIR.glob("*.sql")}
    recorded = set(record["migrations"])

    unrecorded = sorted(on_disk - recorded)
    assert not unrecorded, (
        f"migration file(s) not in the integrity record: {unrecorded}. A stale "
        f"record is worse than no record, because it is trusted. Run "
        f"`python -m scripts.check_migration_integrity --update` and commit it "
        f"WITH the migration.")

    phantom = sorted(recorded - on_disk)
    assert not phantom, (
        f"the record names migration(s) that no longer exist: {phantom}. "
        f"Deleting an applied migration leaves databases that ran it with a "
        f"schema nothing in this repository describes.")


def test_the_gate_passes_on_the_current_tree():
    result = _run()
    assert result.returncode == 0, (
        f"the migration integrity gate fails on an unmodified tree:\n"
        f"{result.stdout}\n{result.stderr}")


def test_the_gate_detects_a_modified_applied_migration(tmp_path):
    """The mildest possible edit — an appended comment, changing no SQL. A gate
    that let this through would let a changed policy through too."""
    target = POSTGRES_DIR / "023_tenancy.sql"
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n-- planted by the test suite\n")
        result = _run()
        assert result.returncode != 0, (
            "the gate passed with a modified applied migration. A file that "
            "some databases have applied and others have not is two different "
            "schemas wearing one filename.")
        assert "023_tenancy.sql" in result.stderr
    finally:
        target.write_bytes(original)

    assert _run().returncode == 0, "the tree was not restored"


def test_the_gate_detects_an_unrecorded_migration():
    """The failure mode that actually happened. A changed hash is the obvious
    one to guard; going stale is the one that is silent."""
    planted = POSTGRES_DIR / "999_planted_by_the_test_suite.sql"
    assert not planted.exists()
    try:
        planted.write_text("-- planted\nSELECT 1;\n", encoding="utf-8")
        result = _run()
        assert result.returncode != 0, (
            "a migration file absent from the record passed the gate. That is "
            "exactly how the record went stale for fifteen files.")
        assert "999_planted_by_the_test_suite.sql" in result.stderr
    finally:
        planted.unlink(missing_ok=True)

    assert _run().returncode == 0, "the tree was not restored"


def test_update_refuses_to_rewrite_a_recorded_hash():
    """The property that stops the control being one command away from defeat.

    `--update` records NEW files and nothing else. If it re-blessed a changed
    file, the answer to "the gate is failing" would be "run --update", and the
    gate would mean nothing.
    """
    target = POSTGRES_DIR / "023_tenancy.sql"
    original = target.read_bytes()
    before = RECORD.read_bytes()
    try:
        target.write_bytes(original + b"\n-- planted by the test suite\n")
        result = _run("--update")
        assert result.returncode != 0, (
            "--update rewrote the hash of a changed migration. The gate is then "
            "one command away from being defeated by the person it exists to "
            "stop.")
        assert "REFUSING" in result.stderr
    finally:
        target.write_bytes(original)

    assert RECORD.read_bytes() == before, (
        "the refused --update still wrote to the record")


# ── The exception mechanism ──────────────────────────────────────────────────


def test_every_exception_names_both_hashes_and_a_written_reason(record):
    """An exception is the escape hatch, so it is the thing most worth pinning.

    "Approved" must mean somebody wrote down what changed and why a later
    migration could not repair it — not that somebody re-ran a command.
    """
    required = ("filename", "previous_sha256", "current_sha256", "approved",
                "approved_by", "defect", "change",
                "why_a_later_migration_could_not_repair_it",
                "production_use_of_previous_version")
    for exc in record.get("exceptions", []):
        missing = [f for f in required if not exc.get(f)]
        assert not missing, (
            f"exception for {exc.get('filename')!r} is missing {missing}. An "
            f"exception without a written reason is a hash update with extra "
            f"steps.")
        assert exc["previous_sha256"] != exc["current_sha256"]
        assert len(exc["why_a_later_migration_could_not_repair_it"].strip()) > 40, (
            f"exception for {exc['filename']!r} does not say why a NEW numbered "
            f"migration could not have fixed this, which is the only thing that "
            f"justifies editing an applied file")


def test_a_file_with_an_exception_has_the_same_hash_in_both_places(record):
    """The two halves of the record must agree, and this test exists because a
    planted defect showed what happens when they do not.

    `verify()` checks `migrations[name].sha256` FIRST and falls through to the
    exception on a mismatch. So a corrupted `migrations` entry for a file that
    happens to have an exception is caught by the exception instead — the gate
    reports OK, and the entry that is supposed to be the authority is quietly
    wrong. Two of the sixty-one files are in that position.

    The exception's `current_sha256` and the migration's recorded hash describe
    the same bytes and must be the same string. Anything else means one of them
    is stale, and the gate cannot tell you which.
    """
    for exc in record.get("exceptions", []):
        name = exc["filename"]
        entry = record["migrations"].get(name)
        assert entry is not None, (
            f"{name} has an approved exception but no entry in `migrations`. "
            f"The exception is then the ONLY thing holding the hash, and it is "
            f"the escape hatch rather than the record.")
        assert entry["sha256"] == exc["current_sha256"], (
            f"{name}: `migrations` records {entry['sha256'][:12]}… and its "
            f"exception names {exc['current_sha256'][:12]}… as current. They "
            f"describe the same bytes. While they disagree, a corrupted "
            f"`migrations` entry is masked by the exception and the gate "
            f"reports OK.")


def test_an_exception_cannot_be_claimed_for_a_hash_it_does_not_name(record):
    """`verify()` accepts a mismatch only when the exception names the CURRENT
    hash. So an approved exception for one edit does not license the next one to
    the same file — which is what "approved" would otherwise drift into."""
    target_name = "023_tenancy.sql"
    exceptions = {e["filename"]: e for e in record.get("exceptions", [])}
    assert target_name in exceptions, (
        "this test is pinned to a file with an approved exception; if that "
        "changed, repoint it rather than deleting it")

    target = POSTGRES_DIR / target_name
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n-- a SECOND edit, not the approved one\n")
        result = _run()
        assert result.returncode != 0, (
            f"{target_name} has an approved exception, and a FURTHER edit to it "
            f"was accepted. An exception licenses one specific before/after "
            f"pair, not the file.")
    finally:
        target.write_bytes(original)
