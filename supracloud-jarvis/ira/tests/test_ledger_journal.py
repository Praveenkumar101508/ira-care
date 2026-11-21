"""The Trust Ledger's fallback journal, on its own: permissions, durability,
tamper evidence, torn writes and concurrent writers (P1-09, 1 November 2026).

`trust/journal.py` holds the records of the two emergency recovery actions,
and any `execution` record, when the Trust Ledger cannot take them. It is a
second audit store, so it has to be as hard to fool as the first: owner-only,
fsync'd before an action relies on it, HMAC-chained so an edit, a deletion or
a reordering is caught, and honest about a write a crash cut short.

What runs when the ledger is down, and the replay into it, are in
`test_ledger_fail_closed.py`; the replay against real PostgreSQL is in
`tests/tenancy/test_ledger_fail_closed_pg.py`.

── Negative controls (planted and watched fail; see
   docs/testing/evidence/ledger-fail-closed-2026-11-01/) ────────────────────

* the file created 0644 / the directory 0755 → the creation test fails
* `_check_private` a no-op → the looser-permission and other-owner tests fail
* `_sync` a no-op → the fsync test fails
* `_write_all` through an unflushed buffered file → the killed-writer test fails
* the MAC check skipped → the edit and forged-line tests fail
* the seq/prev check skipped → the delete and reorder tests fail
* torn bytes trusted (parsed as entries) → the torn-tail tests fail
* `_lock` a no-op → the parallel-writer test fails
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

import pytest

from trust import journal as J

_IRA = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    J._fcntl is None, reason="the journal needs POSIX locking; Windows refuses it")


def _key() -> bytes:
    """The key IRA would use: derived from the server secret the suite runs with."""
    return J._server_key()


@pytest.fixture
def jdir(tmp_path, monkeypatch):
    d = tmp_path / "home" / ".ira" / "trust-journal"
    monkeypatch.setenv(J.JOURNAL_DIR_ENV, str(d))
    return d


def _journal(jdir: Path, key: bytes | None = None) -> J.LedgerJournal:
    return J.LedgerJournal(jdir / J.JOURNAL_FILENAME, key or _key())


def _body(n: int) -> dict:
    return {"kind": "action", "user_id": "owner", "action_id": f"backup_restore-{n:012x}",
            "task_id": None, "record": {"n": n}, "tenant_id": str(uuid.UUID(int=1)),
            "reason": "ledger_unavailable"}


def _written(jdir: Path, n: int = 4) -> J.LedgerJournal:
    j = _journal(jdir)
    for k in range(n):
        j.append(J.ENTRY_RECORD, _body(k))
    return j


def _lines(jdir: Path) -> list[bytes]:
    return (jdir / J.JOURNAL_FILENAME).read_bytes().split(b"\n")[:-1]


def _rewrite(jdir: Path, lines: list[bytes]) -> None:
    (jdir / J.JOURNAL_FILENAME).write_bytes(b"".join(line + b"\n" for line in lines))


# ── Owner-only ────────────────────────────────────────────────────────────────


def test_a_new_journal_is_private_to_its_owner_whatever_the_umask(jdir):
    """Failure mode 9: created 0700 / 0600 even under a umask that grants all."""
    previous = os.umask(0)
    try:
        _journal(jdir).append(J.ENTRY_RECORD, _body(1))
    finally:
        os.umask(previous)
    assert stat.S_IMODE(os.stat(jdir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(jdir / J.JOURNAL_FILENAME).st_mode) == 0o600
    assert os.stat(jdir / J.JOURNAL_FILENAME).st_uid == os.geteuid()


@pytest.mark.parametrize("target,mode", [
    ("file", 0o644), ("file", 0o640), ("file", 0o604),
    ("dir", 0o755), ("dir", 0o750), ("dir", 0o701),
])
def test_a_journal_with_looser_permissions_is_refused_and_says_so(jdir, target, mode):
    """Failure mode 9: a pre-existing 0644 journal is refused, not repaired."""
    j = _written(jdir, 1)
    path = jdir if target == "dir" else jdir / J.JOURNAL_FILENAME
    before = (jdir / J.JOURNAL_FILENAME).read_bytes()
    os.chmod(path, mode)
    try:
        with pytest.raises(J.JournalUnavailable, match=f"{mode:04o}.*chmod") as raised:
            j.append(J.ENTRY_RECORD, _body(2))
        assert "private to its owner" in str(raised.value)
        with pytest.raises(J.JournalUnavailable):
            j.read_state()
        assert stat.S_IMODE(os.stat(path).st_mode) == mode, "the permissions were changed"
    finally:
        os.chmod(path, 0o700 if target == "dir" else 0o600)
    assert (jdir / J.JOURNAL_FILENAME).read_bytes() == before, "a refused journal was written"


def test_a_journal_owned_by_another_account_is_refused(jdir, monkeypatch):
    j = _written(jdir, 1)
    me = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: me + 4242)
    with pytest.raises(J.JournalUnavailable, match=f"belongs to uid {me}"):
        j.append(J.ENTRY_RECORD, _body(2))


def test_a_symlinked_journal_is_refused_and_its_target_untouched(jdir, tmp_path):
    jdir.mkdir(parents=True, mode=0o700)
    os.chmod(jdir, 0o700)
    target = tmp_path / "elsewhere.jsonl"
    target.write_bytes(b"")
    os.chmod(target, 0o600)
    (jdir / J.JOURNAL_FILENAME).symlink_to(target)
    with pytest.raises(J.JournalUnavailable):
        _journal(jdir).append(J.ENTRY_RECORD, _body(1))
    assert target.read_bytes() == b""


def test_a_symlinked_journal_directory_is_refused(jdir, tmp_path):
    real = tmp_path / "real-dir"
    real.mkdir(mode=0o700)
    os.chmod(real, 0o700)
    jdir.parent.mkdir(parents=True)
    jdir.symlink_to(real, target_is_directory=True)
    with pytest.raises(J.JournalUnavailable, match="not a plain directory"):
        _journal(jdir).append(J.ENTRY_RECORD, _body(1))
    assert list(real.iterdir()) == []


def test_a_journal_inside_the_source_tree_is_refused(monkeypatch):
    root = J._source_root()
    if root is None:
        pytest.skip("not running from a checkout")
    inside = _IRA / "data" / f"ledger-journal-probe-{uuid.uuid4().hex[:8]}"
    with pytest.raises(J.JournalUnavailable, match="inside the source tree"):
        J.LedgerJournal(inside / J.JOURNAL_FILENAME, _key()).append(J.ENTRY_RECORD, _body(1))
    assert not inside.exists()


def test_the_default_journal_lives_beside_the_backups_outside_the_repository(monkeypatch):
    monkeypatch.delenv(J.JOURNAL_DIR_ENV, raising=False)
    assert J.journal_dir() == Path.home() / ".ira" / "trust-journal"


def test_without_posix_locking_there_is_no_journal(jdir, monkeypatch):
    monkeypatch.setattr(J, "_fcntl", None)
    with pytest.raises(J.JournalUnavailable, match="POSIX"):
        _journal(jdir).append(J.ENTRY_RECORD, _body(1))


# ── Durable ───────────────────────────────────────────────────────────────────


def test_every_append_is_fsynced_before_it_returns_and_creation_syncs_the_directories(
        jdir, monkeypatch):
    """Failure mode 8, the call: the file after each write, and on creation the
    directory and its parent, so the new names survive a power cut too."""
    synced: list[tuple[int, int]] = []
    real_fsync = os.fsync

    def spy(fd):
        st = os.fstat(fd)
        synced.append((st.st_ino, st.st_size))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    j = _journal(jdir)
    j.append(J.ENTRY_RECORD, _body(1))
    path = jdir / J.JOURNAL_FILENAME
    inodes = {ino for ino, _ in synced}
    assert os.stat(path).st_ino in inodes, "the journal file was never fsynced"
    assert os.stat(jdir).st_ino in inodes, "the new file's directory entry was never fsynced"
    assert os.stat(jdir.parent).st_ino in inodes, "the new directory was never fsynced"
    assert synced[-1] == (os.stat(path).st_ino, path.stat().st_size), (
        "the last fsync came before the entry was fully written")

    synced.clear()
    j.append(J.ENTRY_RECORD, _body(2))
    assert synced == [(os.stat(path).st_ino, path.stat().st_size)], (
        "an append must fsync the file once, after its write, and nothing else")


_KILLED_WRITER = textwrap.dedent("""
    import os, signal, sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    os.environ.setdefault("IRA_SECRET_KEY", sys.argv[3])
    from trust import journal as J
    j = J.LedgerJournal(Path(sys.argv[2]) / J.JOURNAL_FILENAME, J.derive_key(sys.argv[3]))
    for n in range(3):
        j.append(J.ENTRY_RECORD, {"n": n})
    print("appended", flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
""")


def test_an_entry_survives_its_writer_being_killed_the_moment_append_returns(jdir):
    """Failure mode 8, the run: a separate process appends three entries and is
    SIGKILLed as soon as the last append returns. Nothing it wrote may be
    sitting in a user-space buffer: all three are there, and they verify."""
    secret = os.environ["IRA_SECRET_KEY"]
    proc = subprocess.run([sys.executable, "-c", _KILLED_WRITER, str(_IRA), str(jdir), secret],
                          capture_output=True, timeout=60)
    assert proc.returncode == -signal.SIGKILL, proc.stderr.decode()[-500:]
    assert b"appended" in proc.stdout
    state = J.LedgerJournal(jdir / J.JOURNAL_FILENAME, J.derive_key(secret)).read_state()
    assert state.chain_ok, state.error
    assert [e.body["n"] for e in state.records] == [0, 1, 2]
    assert not state.torn_tail


# ── Tamper-evident ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("index", [0, 1, 3])
def test_an_edited_line_fails_verification_wherever_it_is(jdir, index):
    """Failure mode 6: edit. The JSON stays valid; only the MAC can tell."""
    j = _written(jdir)
    lines = _lines(jdir)
    obj = json.loads(lines[index])
    obj["body"]["record"]["n"] = 99
    lines[index] = J._canonical(obj)
    _rewrite(jdir, lines)
    state = j.read_state()
    assert not state.chain_ok
    assert f"line {index + 1}" in state.error and "MAC does not verify" in state.error


def test_a_deleted_line_fails_verification(jdir):
    """Failure mode 6: delete."""
    j = _written(jdir)
    lines = _lines(jdir)
    del lines[1]
    _rewrite(jdir, lines)
    state = j.read_state()
    assert not state.chain_ok
    assert "line 2: entry 3 does not follow entry 1" in state.error


def test_reordered_lines_fail_verification(jdir):
    """Failure mode 6: reorder."""
    j = _written(jdir)
    lines = _lines(jdir)
    lines[1], lines[2] = lines[2], lines[1]
    _rewrite(jdir, lines)
    state = j.read_state()
    assert not state.chain_ok and "does not follow" in state.error


def test_a_line_forged_without_the_key_fails_verification(jdir):
    """Someone who can write the file but does not hold IRA_SECRET_KEY appends
    an entry with the right seq and prev. A bare hash chain would accept it."""
    j = _written(jdir, 2)
    seq, prev = j.read_state().head
    forger = J.LedgerJournal(jdir / J.JOURNAL_FILENAME,
                             J.derive_key("not-the-server-secret-" + uuid.uuid4().hex))
    _entry, line = forger._build(seq + 1, J.ENTRY_RECORD, prev, _body(7))
    with open(jdir / J.JOURNAL_FILENAME, "ab") as fh:
        fh.write(line)
    state = j.read_state()
    assert not state.chain_ok
    assert "line 3" in state.error and "key id" in state.error


def test_a_journal_written_under_another_key_does_not_verify(jdir):
    """What rotating IRA_SECRET_KEY with a backlog looks like: refused, and the
    message says why."""
    _written(jdir, 2)
    other = J.LedgerJournal(jdir / J.JOURNAL_FILENAME,
                            J.derive_key("rotated-server-secret-" + uuid.uuid4().hex))
    state = other.read_state()
    assert not state.chain_ok and "IRA_SECRET_KEY changed" in state.error


def test_bytes_inserted_in_the_middle_fail_verification(jdir):
    j = _written(jdir)
    lines = _lines(jdir)
    lines.insert(2, b"{not json")
    _rewrite(jdir, lines)
    state = j.read_state()
    assert not state.chain_ok and "no torn-write marker covers it" in state.error


def test_lines_cut_off_the_end_are_caught_by_the_process_that_wrote_them(jdir):
    """Truncation, honestly: the process that wrote the lost entries notices.
    A fresh process cannot tell, which the module docstring says."""
    j = _written(jdir)
    _rewrite(jdir, _lines(jdir)[:2])
    state = j.read_state()
    assert not state.chain_ok and "truncated" in state.error
    J._HIGH_WATER.clear()   # as after a restart
    fresh = j.read_state()
    assert fresh.chain_ok and len(fresh.records) == 2


def test_a_journal_that_fails_verification_is_never_appended_to(jdir):
    j = _written(jdir)
    lines = _lines(jdir)
    del lines[1]
    _rewrite(jdir, lines)
    before = (jdir / J.JOURNAL_FILENAME).read_bytes()
    with pytest.raises(J.JournalUnavailable, match="failed verification"):
        j.append(J.ENTRY_RECORD, _body(9))
    assert (jdir / J.JOURNAL_FILENAME).read_bytes() == before


def test_the_key_is_derived_for_this_purpose_and_is_not_a_bare_hash_of_the_secret():
    secret = os.environ["IRA_SECRET_KEY"]
    key = J.derive_key(secret)
    assert len(key) == 32
    assert key != hashlib.sha256(secret.encode()).digest()
    assert key != J.derive_key(secret + "x")
    assert J.key_id(key) != J.key_id(J.derive_key(secret + "x"))


def test_without_a_server_secret_there_is_no_journal(monkeypatch):
    import config

    monkeypatch.setattr(config, "get_settings", lambda: type("S", (), {"ira_secret_key": ""})())
    with pytest.raises(J.JournalUnavailable, match="IRA_SECRET_KEY is not set"):
        J.open_journal()


# ── Crash-safe ────────────────────────────────────────────────────────────────


def test_a_torn_last_line_is_flagged_and_the_lines_before_it_still_verify(jdir):
    """Failure mode 7: a crash mid-write leaves half a line and no newline."""
    j = _written(jdir, 2)
    fragment = _lines(jdir)[0][:37]
    with open(jdir / J.JOURNAL_FILENAME, "ab") as fh:
        fh.write(fragment)
    state = j.read_state()
    assert state.chain_ok and state.torn_tail and state.torn_tail_bytes == 37
    assert [e.body["record"]["n"] for e in state.records] == [0, 1]

    j.append(J.ENTRY_RECORD, _body(9))
    after = j.read_state()
    assert after.chain_ok and not after.torn_tail and after.torn_markers == 1
    assert [e.type for e in after.entries] == ["record", "record", "torn", "record"]
    assert [e.body["record"]["n"] for e in after.records] == [0, 1, 9]
    marker = after.entries[2].body
    assert marker["fragment_sha256"] == hashlib.sha256(fragment).hexdigest()
    assert fragment in (jdir / J.JOURNAL_FILENAME).read_bytes(), (
        "the torn bytes were removed: they are evidence and are kept")


def test_a_complete_entry_whose_newline_never_landed_is_not_trusted(jdir):
    """A write cut short one byte before its end: a whole, correctly MAC'd
    entry with no newline. The append that wrote it never returned, so nothing
    relied on it, and it must not be counted once covered."""
    j = _written(jdir, 2)
    seq, prev = j.read_state().head
    _entry, line = j._build(seq + 1, J.ENTRY_RECORD, prev, _body(5))
    with open(jdir / J.JOURNAL_FILENAME, "ab") as fh:
        fh.write(line[:-1])
    state = j.read_state()
    assert state.chain_ok and state.torn_tail
    assert [e.body["record"]["n"] for e in state.records] == [0, 1]

    j.append(J.ENTRY_RECORD, _body(6))
    after = j.read_state()
    assert after.chain_ok, after.error
    assert [e.body["record"]["n"] for e in after.records] == [0, 1, 6]
    assert after.torn_markers == 1


def test_a_write_cut_short_by_a_full_disk_is_refused_and_the_next_append_recovers(
        jdir, monkeypatch):
    j = _written(jdir, 1)
    real = J._write_all

    def half_then_full(fd, data):
        os.write(fd, bytes(data[: len(data) // 2]))
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(J, "_write_all", half_then_full)
    with pytest.raises(J.JournalUnavailable, match="ENOSPC"):
        j.append(J.ENTRY_RECORD, _body(2))
    torn = j.read_state()
    assert torn.chain_ok and torn.torn_tail and len(torn.records) == 1

    monkeypatch.setattr(J, "_write_all", real)
    j.append(J.ENTRY_RECORD, _body(3))
    after = j.read_state()
    assert after.chain_ok and not after.torn_tail
    assert [e.body["record"]["n"] for e in after.records] == [0, 3]


def test_bytes_that_are_json_but_not_an_entry_at_the_end_are_tampering_not_a_crash(jdir):
    """A crash cannot leave a complete JSON line: only an un-parseable tail is
    treated as a torn write."""
    j = _written(jdir, 2)
    with open(jdir / J.JOURNAL_FILENAME, "ab") as fh:
        fh.write(b'{"n": 1}\n')
    state = j.read_state()
    assert not state.chain_ok and "line 3" in state.error


# ── One writer at a time ──────────────────────────────────────────────────────


def test_parallel_writer_threads_produce_one_valid_chain(jdir, monkeypatch):
    """Failure mode 11. Each writer pauses between reading the head and
    writing, the widest window two writers could interleave in. With the lock
    they cannot."""
    real_read = J._read_all

    def slow_read(fd):
        data = real_read(fd)
        time.sleep(0.002)
        return data

    monkeypatch.setattr(J, "_read_all", slow_read)
    errors: list[BaseException] = []

    def writer(w: int) -> None:
        try:
            for k in range(10):
                _journal(jdir).append(J.ENTRY_RECORD, {"w": w, "k": k})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert errors == []
    state = _journal(jdir).read_state()
    assert state.chain_ok, state.error
    assert [e.seq for e in state.entries] == list(range(1, 81))
    assert sorted((e.body["w"], e.body["k"]) for e in state.records) == [
        (w, k) for w in range(8) for k in range(10)]


_PROCESS_WRITER = textwrap.dedent("""
    import sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from trust import journal as J
    j = J.LedgerJournal(Path(sys.argv[2]) / J.JOURNAL_FILENAME, J.derive_key(sys.argv[3]))
    for k in range(15):
        j.append(J.ENTRY_RECORD, {"w": int(sys.argv[4]), "k": k})
""")


def test_parallel_writer_processes_produce_one_valid_chain(jdir):
    secret = os.environ["IRA_SECRET_KEY"]
    procs = [subprocess.Popen([sys.executable, "-c", _PROCESS_WRITER, str(_IRA), str(jdir),
                               secret, str(w)], stderr=subprocess.PIPE) for w in range(4)]
    for p in procs:
        _out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()[-500:]
    state = J.LedgerJournal(jdir / J.JOURNAL_FILENAME, J.derive_key(secret)).read_state()
    assert state.chain_ok, state.error
    assert [e.seq for e in state.entries] == list(range(1, 61))
    assert len({(e.body["w"], e.body["k"]) for e in state.records}) == 60
