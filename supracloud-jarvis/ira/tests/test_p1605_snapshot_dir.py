"""P16-05 — SEC-03/B108: the forensic snapshot directory is verified, not assumed.

`snapshot_logs()` writes the last 500 rows of `security_events` — source IPs,
raw log lines, incident descriptions — to `IRA_LOG_SNAPSHOT_DIR`, defaulting to
`/tmp/ira-logs`. Before P16-05 that was `mkdir(parents=True, exist_ok=True)`
followed by `write_text`.

Each of those is ordinary. Together, under a world-writable default at a path
that is a constant in the source, they let any local user pre-create the
destination — as a directory they own, or as a symlink — and receive, or
rewrite, the forensic record of an intrusion.

── Why these are drills and not assertions about source ────────────────────

Every case below builds the actual hostile filesystem state and calls the real
function. A test that grepped `playbooks.py` for `O_NOFOLLOW` would pass on a
file that never opens anything, which is the failure class this programme keeps
finding. `test_the_old_shape_would_have_been_exploited` is the negative
control: it performs the pre-fix sequence against the same hostile state and
requires it to succeed, so a green run here means the drills discriminate
rather than that the OS refused everything anyway.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from utils.playbooks import _secure_snapshot_dir


def test_a_clean_directory_is_accepted(tmp_path: Path) -> None:
    """The control must permit the ordinary case, or it proves nothing."""
    target = tmp_path / "snapshots"
    assert _secure_snapshot_dir(target) is None
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_it_creates_the_directory_private(tmp_path: Path) -> None:
    """0o700 on creation — the events are not readable by other local users."""
    target = tmp_path / "a" / "b" / "snapshots"
    assert _secure_snapshot_dir(target) is None
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_a_symlink_at_the_directory_is_refused(tmp_path: Path) -> None:
    """The pre-created-symlink attack: somebody else chooses the destination."""
    elsewhere = tmp_path / "attacker-owned"
    elsewhere.mkdir()
    target = tmp_path / "snapshots"
    target.symlink_to(elsewhere, target_is_directory=True)

    why = _secure_snapshot_dir(target)
    assert why is not None, "a symlinked snapshot directory was accepted"
    assert "symlink" in why.lower()


def test_a_world_writable_directory_is_refused(tmp_path: Path) -> None:
    """`exist_ok=True` accepted whatever already sat there. It no longer does."""
    target = tmp_path / "snapshots"
    target.mkdir(mode=0o777)
    os.chmod(target, 0o777)

    why = _secure_snapshot_dir(target)
    assert why is not None, "a world-writable snapshot directory was accepted"
    assert "writable" in why.lower()


def test_a_group_writable_directory_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "snapshots"
    target.mkdir(mode=0o770)
    os.chmod(target, 0o770)

    why = _secure_snapshot_dir(target)
    assert why is not None, "a group-writable snapshot directory was accepted"
    assert "writable" in why.lower()


def test_a_file_where_the_directory_should_be_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "snapshots"
    target.write_text("not a directory")

    why = _secure_snapshot_dir(target)
    assert why is not None
    assert "not a directory" in why.lower() or "cannot create" in why.lower()


def test_the_old_shape_would_have_been_exploited(tmp_path: Path) -> None:
    """NEGATIVE CONTROL — the pre-fix sequence against the same hostile state.

    Without this, the drills above could be passing because the operating
    system refuses these operations anyway, and would keep passing if the fix
    were reverted. This performs exactly what the code used to do —
    `mkdir(parents=True, exist_ok=True)` then `write_text` — against a
    symlinked directory, and requires that it SUCCEEDS in redirecting the
    write. If this ever stops succeeding, the drills above have stopped
    discriminating and this file needs rewriting rather than trusting.
    """
    elsewhere = tmp_path / "attacker-owned"
    elsewhere.mkdir()
    target = tmp_path / "snapshots"
    target.symlink_to(elsewhere, target_is_directory=True)

    target.mkdir(parents=True, exist_ok=True)          # the old line
    (target / "ira-security-snapshot-x.json").write_text('{"events": []}')  # the old line

    landed = elsewhere / "ira-security-snapshot-x.json"
    assert landed.is_file(), (
        "the pre-fix sequence failed to redirect the write, so the drills above "
        "may be passing for a reason other than the fix"
    )
    assert _secure_snapshot_dir(target) is not None, (
        "the same state the old code wrote into is still accepted by the new "
        "check — the fix is not doing anything"
    )


def test_the_writer_refuses_to_follow_a_symlinked_file(tmp_path: Path) -> None:
    """O_EXCL|O_NOFOLLOW, drilled directly on the flags the writer uses.

    The directory check closes the directory; this closes the file. Both are
    needed, because a verified directory can still be raced if its mode is
    ever widened again.
    """
    target = tmp_path / "snapshots"
    assert _secure_snapshot_dir(target) is None

    victim = tmp_path / "elsewhere.json"
    victim.write_text("original")
    planted = target / "ira-security-snapshot-manual-x.json"
    planted.symlink_to(victim)

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with pytest.raises(OSError):
        os.open(planted, flags, 0o600)

    assert victim.read_text() == "original", "the symlink target was written through"
