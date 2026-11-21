"""
trust/journal.py — what may run when the Trust Ledger cannot be written, and
the fallback journal that keeps the record when it may (P1-09, 1 November 2026).

── The policy, by class ────────────────────────────────────────────────────

Every owner-confirmed action writes three Trust Ledger records: `action` (the
intent) and `approval` before the side effect, `execution` after it. Until
P1-09 a failed write did not stop the action; the response said
`ledger_recorded: false` and nothing showed it. The owner approved this
classification in writing on 1 November 2026:

* **Class A — fail closed.** Memory forget, file delete, remember or forget a
  person, access grant and role change, Android taps, the mobile email task,
  Cal.com book and cancel, the Command Center, and everything confirmed through
  `actions/gated_execution.confirm_action` (email send, note delete, CalDAV
  events, scheduling). When the `action` or `approval` record cannot be written
  to the Trust Ledger, the action is **refused**: nothing runs, the
  confirmation token is not spent, so the same confirmation works again once
  the ledger is back, and the owner is told so. Class A never writes its
  before-the-side-effect records to this journal. A journal entry is never
  permission to run a Class A action.

* **Class B — controlled emergency recovery.** Backup restore and access
  revoke: how the owner repairs a broken database and contains a compromised
  account, which are exactly the moments the ledger may be down. When the
  ledger write fails, the records go to this journal, and the action runs
  **only once the journal write is durably on disk** (written and fsync'd).
  A journal write that fails is a refusal, never permission. The list lives in
  `utils/approval.RECOVERY_ACTIONS`, a protected file: a caller cannot opt
  itself into Class B.

* **Both stores unavailable — the explicit safety policy.** When neither the
  ledger nor this journal can take the record, **every** gated action refuses,
  Class B included: nothing runs without a durable record. The refusal names
  the out-of-band break-glass, which needs a shell on the owner's computer and
  does not go through IRA, so it does not depend on either store:

      bash scripts/ira-backup.sh restore <archive> --yes    recover (INSTALL.md)
      ./stop-ira.sh                                        contain: stop all access

  (`scripts\\ira-backup.ps1 restore <archive> -Yes` and `stop-ira.ps1` on
  Windows.) A Class A refusal names them too when a read-only probe finds the
  journal unusable as well.

* **The `execution` record**, written after the side effect, cannot be made
  fail-closed: the action has already happened. It goes to the ledger, then to
  this journal; when both fail it is logged at CRITICAL, counted in
  `/api/v1/trust/ledger/health`, and the response says `ledger_recorded: false`.

── The journal ─────────────────────────────────────────────────────────────

**Where.** `~/.ira/trust-journal/ledger-journal.jsonl`, beside `~/.ira/backups`
and outside the repository, or `$IRA_LEDGER_JOURNAL_DIR/ledger-journal.jsonl`.
A configured directory inside the source tree is refused.

**Owner-only.** The directory must be 0700 and the file 0600, both owned by the
uid IRA runs as; they are created so. Looser permissions, another owner or a
symbolic link are refused with a message saying which and how to fix it, never
repaired silently: a journal someone else could read or write is not one IRA
can vouch for.

**Append-only.** Every write is an `O_APPEND` write of whole lines. Nothing is
rewritten: replaying an entry into the ledger appends a `reconciled` line
naming it rather than editing it.

**Durable.** Each append is fsync'd (`F_FULLFSYNC` where the OS has it, as
macOS does) before it returns; creating the file also fsyncs the directory.
Nothing is buffered in user space.

**Tamper-evident.** Each line carries `prev`, the MAC of the line before it,
and `mac`, an HMAC-SHA256 over its own fields. The key is derived from
`IRA_SECRET_KEY` with HKDF-SHA256 under a label used for nothing else, and is
never stored. A bare hash would not do: anyone who can write the file can
recompute one. Rotating `IRA_SECRET_KEY` makes earlier lines fail to verify, so
rotate it only while `/api/v1/trust/ledger/health` shows no backlog.

**Crash-safe.** A write cut short leaves an incomplete last line. It is
detected and flagged (`torn_tail` in the health report), never trusted, and the
lines before it still verify. The next append covers it with a `torn` line that
records the fragment's SHA-256, so the fragment stays in the file as evidence
and the chain continues past it.

**One writer at a time.** An exclusive `flock` is held from reading the tail to
the fsync, across threads and processes.

**Nothing secret.** An entry holds the record the ledger would have received,
which follows the same rules: `ledger_preview` instead of a preview that
quotes memory text, names, typed text or an email body, the result's shape
instead of the result, and never the confirmation token.

**Reconciliation.** At startup, and whenever the ledger next accepts a write,
the journal's chain is verified and its unreconciled `record` entries are
appended to `trust_ledger` in journal order, each under the tenant it was
written for. Each carries its `journal_entry_id`, and `append_record` returns
the existing row for an id it already holds, so a replay interrupted at any
point and run again adds nothing twice. A chain that does not verify is not
replayed: it is logged at CRITICAL, recorded as a `security_event`, and shown
in the health report and the Trust Ledger panel. To recover from one, stop IRA,
keep the file for inspection (`mv ledger-journal.jsonl ledger-journal.broken-<date>`),
and start IRA, which then begins a new journal.

── What someone with write access to the journal can and cannot do ─────────

They can delete the file, cut lines off its end, or append bytes that are not
a valid entry. The last is flagged and never trusted. The first two are
detected only by the process that wrote the lost entries, until it restarts;
an external anchor would be needed to do better. They can also break the
chain, which stops replay and, during a ledger outage, makes Class B refuse.
Without `IRA_SECRET_KEY` they cannot forge, edit, insert or reorder an entry
without detection, and so cannot make IRA replay an entry into the ledger that
IRA did not write.

Windows has neither `flock` nor POSIX modes, so the journal is unavailable
there: when the ledger is down every gated action refuses and names the
break-glass.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator, Optional

_fcntl: Optional[ModuleType]
try:
    import fcntl as _fcntl
except ImportError:  # Windows: see the module docstring
    _fcntl = None

logger = logging.getLogger("ira.trust.journal")

#: Environment variable naming the journal's directory.
JOURNAL_DIR_ENV = "IRA_LEDGER_JOURNAL_DIR"
JOURNAL_FILENAME = "ledger-journal.jsonl"
FORMAT_VERSION = 1
GENESIS = "0" * 64
#: How long an append waits for another writer before it gives up (and, for
#: Class B, refuses). A writer holds the lock for one read, one write and one
#: fsync.
LOCK_TIMEOUT_SECONDS = 5.0
#: How long the advisory probe (`probe`) waits for the journal's shared lock
#: before answering "busy" (#250): it only chooses a refusal's wording, so it
#: must not hold a worker thread for the writer's full wait.
PROBE_LOCK_TIMEOUT_SECONDS = 0.5

ENTRY_RECORD = "record"
ENTRY_RECONCILED = "reconciled"
ENTRY_TORN = "torn"
_ENTRY_TYPES = frozenset({ENTRY_RECORD, ENTRY_RECONCILED, ENTRY_TORN})
_FIELDS = ("v", "seq", "entry_id", "type", "at", "prev", "kid", "body")

_KDF_SALT = b"ira/trust-ledger-journal/salt/v1"
_KDF_INFO = b"ira/trust-ledger-journal/hmac-sha256/v1"
_PLACEHOLDER_SECRETS = frozenset({"", "CHANGE_ME_ira_secret_key_here"})

#: The out-of-band break-glass. Both run from a shell on the owner's computer,
#: in the `supracloud-jarvis` folder, and neither goes through IRA.
BREAK_GLASS_RECOVER = "bash scripts/ira-backup.sh restore <archive> --yes"
BREAK_GLASS_CONTAIN = "./stop-ira.sh"
BREAK_GLASS = (BREAK_GLASS_RECOVER, BREAK_GLASS_CONTAIN)

#: `user_id` of the security event a broken chain records.
SYSTEM_USER = "system:ledger-journal"


class JournalUnavailable(RuntimeError):
    """The journal cannot durably take an entry, or cannot be read and trusted.

    Every failure of the journal is this one type, so a caller has one thing to
    refuse on. Its message says what is wrong and, where there is one, the fix.
    """


@dataclass(frozen=True)
class JournalEntry:
    line: int
    seq: int
    entry_id: str
    type: str
    at: str
    prev: str
    kid: str
    body: dict[str, Any]
    mac: str


@dataclass
class JournalState:
    """One read of the journal: its verified entries and what is wrong, if anything."""

    path: Path
    exists: bool = False
    entries: list[JournalEntry] = field(default_factory=list)
    chain_ok: bool = True
    error: Optional[str] = None
    #: Bytes after the last verified line that no entry covers: a write cut
    #: short. Flagged, never trusted.
    torn_tail: bool = False
    torn_tail_bytes: int = 0
    #: Earlier torn writes, each covered by a `torn` line.
    torn_markers: int = 0
    #: Byte offset just past the last verified line.
    good_end: int = 0
    reconciled: dict[str, int] = field(default_factory=dict)

    @property
    def records(self) -> list[JournalEntry]:
        return [e for e in self.entries if e.type == ENTRY_RECORD]

    @property
    def pending(self) -> list[JournalEntry]:
        """`record` entries not yet replayed into the ledger, in journal order."""
        return [e for e in self.records if e.entry_id not in self.reconciled]

    @property
    def head(self) -> tuple[int, str]:
        if not self.entries:
            return 0, GENESIS
        last = self.entries[-1]
        return last.seq, last.mac


# ── Paths and the key ─────────────────────────────────────────────────────────

def journal_dir() -> Path:
    configured = os.getenv(JOURNAL_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".ira" / "trust-journal"


def journal_path() -> Path:
    return journal_dir() / JOURNAL_FILENAME


def _source_root() -> Optional[Path]:
    """The IRA checkout this module runs from, or None (an installed image).

    Only a git root that holds `supracloud-jarvis/` counts: a home directory
    kept in git (dotfiles) above an unpacked copy of IRA is not the source
    tree, and treating it as one would refuse the default `~/.ira` journal.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists() and (parent / "supracloud-jarvis").is_dir():
            return parent
    return None


def _show(path: Path) -> str:
    """The path with the home directory shortened, for messages and logs."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def derive_key(secret: str) -> bytes:
    """HKDF-SHA256 (RFC 5869, one block) of the server secret, for this purpose only."""
    prk = hmac.new(_KDF_SALT, secret.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(prk, _KDF_INFO + b"\x01", hashlib.sha256).digest()


def key_id(key: bytes) -> str:
    """A fingerprint of the key, so a line written under another key says so."""
    return hmac.new(key, b"ira/trust-ledger-journal/key-id", hashlib.sha256).hexdigest()[:16]


def _server_key() -> bytes:
    try:
        from config import get_settings

        secret = get_settings().ira_secret_key or ""
    except Exception as exc:  # noqa: BLE001 — no key, no journal
        raise JournalUnavailable(
            f"the server secret could not be read ({type(exc).__name__}), so the "
            "journal cannot be keyed") from exc
    if secret.strip() in _PLACEHOLDER_SECRETS:
        raise JournalUnavailable(
            "IRA_SECRET_KEY is not set, so the journal cannot be keyed and its "
            "entries could not be told apart from forgeries")
    return derive_key(secret)


# ── Low-level file operations ─────────────────────────────────────────────────

def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def _mac(key: bytes, fields: dict[str, Any]) -> str:
    return hmac.new(key, _canonical(fields), hashlib.sha256).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sync(fd: int) -> None:
    """Make what was written to `fd` durable before returning."""
    full = getattr(_fcntl, "F_FULLFSYNC", None) if _fcntl is not None else None
    if full is not None and _fcntl is not None:
        try:
            _fcntl.fcntl(fd, full)
            return
        except OSError:
            pass  # not every filesystem supports it; fsync is the floor
    os.fsync(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _read_all(fd: int) -> bytes:
    size = os.fstat(fd).st_size
    chunks: list[bytes] = []
    offset = 0
    while offset < size:
        chunk = os.pread(fd, min(1 << 20, size - offset), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _lock(fd: int, *, exclusive: bool, timeout: Optional[float] = None) -> None:
    if _fcntl is None:
        raise JournalUnavailable("the fallback journal needs POSIX file locking")
    op = _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH
    wait = LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    deadline = time.monotonic() + wait
    while True:
        try:
            _fcntl.flock(fd, op | _fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise JournalUnavailable(
                    f"another writer has held the journal lock for "
                    f"{wait:g} s") from None
            time.sleep(0.005)


def _check_private(st: os.stat_result, *, what: str, path: Path, wanted: int) -> None:
    euid = os.geteuid()
    if st.st_uid != euid:
        raise JournalUnavailable(
            f"refusing the Trust Ledger journal {what} {_show(path)}: it belongs to "
            f"uid {st.st_uid}, and IRA runs as uid {euid}")
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise JournalUnavailable(
            f"refusing the Trust Ledger journal {what} {_show(path)}: its permissions "
            f"are {mode:04o}, and it must be private to its owner ({wanted:04o}). "
            f"Check nothing else has read or changed it, then run: "
            f"chmod {wanted:o} {_show(path)}")


def _os_error(exc: OSError, doing: str, path: Path) -> JournalUnavailable:
    import errno

    name = errno.errorcode.get(exc.errno or 0, type(exc).__name__)
    return JournalUnavailable(f"could not {doing} {_show(path)}: {name} ({exc.strerror or exc})")


_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def _make_private_dir(directory: Path) -> None:
    """Create the journal directory 0700 and make its creation durable."""
    parent = directory.parent
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            return  # someone else made it; it is checked like any existing one
        os.chmod(directory, 0o700)  # the umask may have narrowed the mode, never widened it
        pfd = os.open(parent, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
        try:
            _sync(pfd)
        finally:
            os.close(pfd)
    except OSError as exc:
        raise _os_error(exc, "create the journal directory", directory) from exc


# ── The journal ───────────────────────────────────────────────────────────────

#: The newest entry each journal file is known to have held, in this process.
#: A file that later ends earlier, or holds a different entry there, has been
#: truncated or replaced. Without an external anchor this is the only check on
#: lines cut off the end, so it lasts as long as the process does.
_HIGH_WATER: dict[str, tuple[int, str]] = {}


class LedgerJournal:
    """One journal file: verified reads and durable, chained, locked appends."""

    def __init__(self, path: Path, key: bytes) -> None:
        self.path = path
        self.directory = path.parent
        self.key = key
        self.kid = key_id(key)

    # -- opening ---------------------------------------------------------

    def _check_platform(self) -> None:
        if _fcntl is None or not hasattr(os, "geteuid"):
            raise JournalUnavailable(
                "the fallback journal needs POSIX file locking and permissions, "
                "which this platform does not have")
        root = _source_root()
        if root is not None:
            try:
                self.directory.resolve().relative_to(root)
            except ValueError:
                pass
            else:
                raise JournalUnavailable(
                    f"refusing a Trust Ledger journal inside the source tree "
                    f"({_show(self.directory)}): set {JOURNAL_DIR_ENV} outside it")

    def _open_dir(self, *, create: bool) -> Optional[int]:
        try:
            st = os.lstat(self.directory)
        except FileNotFoundError:
            if not create:
                return None
            _make_private_dir(self.directory)
            try:
                st = os.lstat(self.directory)
            except OSError as exc:
                raise _os_error(exc, "read", self.directory) from exc
        except OSError as exc:
            raise _os_error(exc, "read", self.directory) from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise JournalUnavailable(
                f"refusing the Trust Ledger journal directory {_show(self.directory)}: "
                "it is not a plain directory (a symbolic link or a file)")
        _check_private(st, what="directory", path=self.directory, wanted=0o700)
        try:
            dfd = os.open(self.directory, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
        except OSError as exc:
            raise _os_error(exc, "open", self.directory) from exc
        opened = os.fstat(dfd)
        if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
            os.close(dfd)
            raise JournalUnavailable(
                f"the journal directory {_show(self.directory)} changed while it was opened")
        return dfd

    def _open_file(self, dfd: int, *, create: bool, write: bool) -> Optional[int]:
        flags = (os.O_RDWR | os.O_APPEND if write else os.O_RDONLY) | _O_NOFOLLOW | _O_CLOEXEC
        created = False
        try:
            try:
                fd = os.open(JOURNAL_FILENAME, flags, dir_fd=dfd)
            except FileNotFoundError:
                if not create:
                    return None
                try:
                    fd = os.open(JOURNAL_FILENAME, flags | os.O_CREAT | os.O_EXCL, 0o600,
                                 dir_fd=dfd)
                    created = True
                except FileExistsError:
                    fd = os.open(JOURNAL_FILENAME, flags, dir_fd=dfd)
        except OSError as exc:
            raise _os_error(exc, "open", self.path) from exc
        try:
            if created:
                os.fchmod(fd, 0o600)
                _sync(fd)
                _sync(dfd)   # the new name is durable, not only the file
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise JournalUnavailable(
                    f"refusing the Trust Ledger journal {_show(self.path)}: not a regular file")
            _check_private(st, what="file", path=self.path, wanted=0o600)
        except OSError as exc:
            os.close(fd)
            raise _os_error(exc, "prepare", self.path) from exc
        except BaseException:
            os.close(fd)
            raise
        return fd

    @contextmanager
    def _opened(self, *, create: bool, write: bool) -> Iterator[Optional[int]]:
        self._check_platform()
        dfd = self._open_dir(create=create)
        if dfd is None:
            yield None
            return
        try:
            fd = self._open_file(dfd, create=create, write=write)
            if fd is None:
                yield None
                return
            try:
                yield fd
            finally:
                os.close(fd)   # releases the flock too
        finally:
            os.close(dfd)

    # -- the chain ---------------------------------------------------------

    def _parse(self, raw: bytes) -> tuple[Optional[dict[str, Any]], str, bool]:
        """(fields, "", False) when the line is an entry whose MAC verifies.

        Otherwise (None, why, forged). `forged` is True for a line that is JSON
        but not a verified entry. A write cut short cannot produce one: every
        entry is one JSON object, and any prefix of it short of the closing
        brace does not parse. So only a line that is not JSON at all may be a
        torn write; anything else was written by someone without the key, or
        under another one, wherever in the file it sits.
        """
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, "not a complete journal line", False
        if not isinstance(obj, dict) or set(obj) != set(_FIELDS) | {"mac"}:
            return None, "a line that is not a journal entry", True
        mac = obj.pop("mac")
        if not isinstance(mac, str) or not hmac.compare_digest(_mac(self.key, obj), mac):
            if obj.get("kid") != self.kid:
                return None, (f"written under key id {obj.get('kid')!r}, and this server's "
                              f"is {self.kid!r}: IRA_SECRET_KEY changed, or the line was "
                              "forged"), True
            return None, "its MAC does not verify: it was edited, or forged without the key", True
        if (not isinstance(obj["seq"], int) or not isinstance(obj["body"], dict)
                or obj["type"] not in _ENTRY_TYPES or obj["v"] != FORMAT_VERSION):
            return None, "a verified line with an unknown shape", True
        obj["mac"] = mac
        return obj, "", False

    def _verify(self, content: bytes) -> JournalState:
        state = JournalState(path=self.path, exists=True)
        if not content:
            self._check_high_water(state)
            return state
        # Every complete line as a byte span; whatever follows the last newline
        # is a write cut short, never parsed and never trusted.
        spans: list[tuple[int, int]] = []
        start = 0
        while (newline := content.find(b"\n", start)) >= 0:
            spans.append((start, newline))
            start = newline + 1
        tail_start = start
        parsed = [self._parse(content[a:b]) for a, b in spans]
        # A `torn` line covers the bytes between the last entry before it and
        # itself. It is found by the place in the chain it claims, so that bytes
        # it covers are never read as entries, even a complete entry whose
        # newline was never written.
        markers: dict[tuple[int, str], int] = {}
        for index, (obj, _why, _forged) in enumerate(parsed):
            if obj is not None and obj["type"] == ENTRY_TORN:
                markers.setdefault((obj["seq"], obj["prev"]), index)

        def broken(reason: str) -> JournalState:
            state.chain_ok = False
            state.error = reason
            return state

        def accept(index: int) -> None:
            obj = parsed[index][0]
            if obj is None:  # only verified lines are accepted
                raise ValueError(f"line {index + 1} is not a verified entry")
            entry = JournalEntry(
                line=index + 1, seq=obj["seq"], entry_id=str(obj["entry_id"]),
                type=obj["type"], at=str(obj["at"]), prev=obj["prev"],
                kid=str(obj["kid"]), body=obj["body"], mac=obj["mac"])
            state.entries.append(entry)
            if entry.type == ENTRY_RECONCILED:
                body = entry.body
                state.reconciled[str(body.get("entry_id"))] = int(body.get("ledger_seq") or 0)
            state.good_end = spans[index][1] + 1

        prev, expected, i = GENESIS, 1, 0
        while i < len(spans):
            m = markers.get((expected, prev))
            if m is not None and m > i:
                marker = parsed[m][0]
                if marker is None:  # indexed from verified lines only
                    raise ValueError(f"line {m + 1} is not a verified entry")
                fragment = content[spans[i][0]:spans[m][0] - 1]
                if (marker["body"].get("fragment_sha256")
                        == hashlib.sha256(fragment).hexdigest()
                        and marker["body"].get("fragment_bytes") == len(fragment)):
                    accept(m)
                    state.torn_markers += 1
                    prev, expected, i = marker["mac"], expected + 1, m + 1
                    continue
            obj, why, forged = parsed[i]
            if obj is not None and obj["seq"] == expected and obj["prev"] == prev:
                if obj["type"] == ENTRY_TORN:
                    return broken(f"line {i + 1}: a torn-write marker that covers "
                                  "nothing before it")
                accept(i)
                prev, expected, i = obj["mac"], expected + 1, i + 1
                continue
            # Nothing verified starts here and no marker covers it. Only bytes
            # that are not JSON at all, running to the end of the file, can be
            # a write cut short; anything else is someone else's doing.
            if forged:
                return broken(f"line {i + 1}: {why}")
            if obj is not None:
                return broken(
                    f"line {i + 1}: entry {obj['seq']} does not follow entry "
                    f"{expected - 1}: a line was deleted, inserted or reordered")
            for k in range(i + 1, len(spans)):
                later, later_why, later_forged = parsed[k]
                if later_forged:
                    return broken(f"line {k + 1}: {later_why}")
                if later is not None:
                    return broken(f"line {i + 1}: {why}, and no torn-write marker "
                                  "covers it")
            state.torn_tail = True
            break
        if tail_start < len(content):
            state.torn_tail = True
        if state.torn_tail:
            state.torn_tail_bytes = len(content) - state.good_end
        self._check_high_water(state)
        return state

    def _check_high_water(self, state: JournalState) -> None:
        mark = _HIGH_WATER.get(str(self.path))
        if mark is None or not state.chain_ok:
            return
        seq, mac = mark
        held = {e.seq: e.mac for e in state.entries}
        if seq not in held:
            state.chain_ok = False
            state.error = (f"the journal ends at entry {state.head[0]}, but this process "
                           f"wrote entry {seq}: it was truncated, deleted or replaced")
        elif held[seq] != mac:
            state.chain_ok = False
            state.error = f"entry {seq} is not the one this process wrote: the journal was replaced"

    def _remember(self, seq: int, mac: str) -> None:
        current = _HIGH_WATER.get(str(self.path))
        if current is None or seq > current[0]:
            _HIGH_WATER[str(self.path)] = (seq, mac)

    # -- reading and appending ----------------------------------------------

    def read_state(self, *, lock_timeout: Optional[float] = None) -> JournalState:
        """Verify the journal as it is now. Never creates anything. `lock_timeout`
        shortens the wait for the shared lock (the probe passes its own)."""
        with self._opened(create=False, write=False) as fd:
            if fd is None:
                state = JournalState(path=self.path, exists=False)
                if str(self.path) in _HIGH_WATER:
                    state.chain_ok = False
                    state.error = ("the journal this process wrote is gone: it was "
                                   "deleted or moved")
                return state
            _lock(fd, exclusive=False, timeout=lock_timeout)
            try:
                content = _read_all(fd)
            except OSError as exc:
                raise _os_error(exc, "read", self.path) from exc
        state = self._verify(content)
        if state.chain_ok and state.entries:
            self._remember(*state.head)
        return state

    def _build(self, seq: int, entry_type: str, prev: str,
               body: dict[str, Any]) -> tuple[JournalEntry, bytes]:
        entry_id, at = uuid.uuid4().hex, _now()
        fields = {"v": FORMAT_VERSION, "seq": seq, "entry_id": entry_id,
                  "type": entry_type, "at": at, "prev": prev, "kid": self.kid,
                  "body": body}
        mac = _mac(self.key, fields)
        line = _canonical({**fields, "mac": mac}) + b"\n"
        entry = JournalEntry(line=0, seq=seq, entry_id=entry_id, type=entry_type,
                             at=at, prev=prev, kid=self.kid, body=body, mac=mac)
        return entry, line

    def append(self, entry_type: str, body: dict[str, Any]) -> JournalEntry:
        """Append one entry and return once it is on disk. Raises JournalUnavailable."""
        if entry_type not in _ENTRY_TYPES or entry_type == ENTRY_TORN:
            raise ValueError(f"cannot append a {entry_type!r} entry")
        # Normalised through JSON first, so what is MAC'd is exactly what a
        # reader will parse back.
        body = json.loads(_canonical(body))
        with self._opened(create=True, write=True) as fd:
            if fd is None:  # create=True always opens or raises
                raise JournalUnavailable(f"could not open {_show(self.path)}")
            _lock(fd, exclusive=True)
            try:
                content = _read_all(fd)
            except OSError as exc:
                raise _os_error(exc, "read", self.path) from exc
            state = self._verify(content)
            if not state.chain_ok:
                raise JournalUnavailable(
                    f"the journal {_show(self.path)} failed verification ({state.error}), "
                    "so nothing more is written to it")
            seq, prev = state.head
            out = b""
            if state.torn_tail:
                region = content[state.good_end:]
                terminator = b"" if region.endswith(b"\n") else b"\n"
                fragment = (region + terminator)[:-1]
                marker, marker_line = self._build(seq + 1, ENTRY_TORN, prev, {
                    "fragment_sha256": hashlib.sha256(fragment).hexdigest(),
                    "fragment_lines": len(fragment.split(b"\n")),
                    "fragment_bytes": len(fragment)})
                out += terminator + marker_line
                seq, prev = marker.seq, marker.mac
                logger.warning("ledger journal: an incomplete write of %d bytes after entry "
                               "%d is kept and marked, not trusted", len(fragment), seq - 1)
            entry, line = self._build(seq + 1, entry_type, prev, body)
            out += line
            try:
                _write_all(fd, out)
                _sync(fd)
            except OSError as exc:
                raise _os_error(exc, "write", self.path) from exc
            self._remember(entry.seq, entry.mac)
            return entry


def open_journal() -> LedgerJournal:
    """The journal at the configured path, keyed from the server secret."""
    return LedgerJournal(journal_path(), _server_key())


# ── What this process knows: failures, the backlog, replays ──────────────────

@dataclass
class _ProcessState:
    last_ledger_failure_at: Optional[str] = None
    last_ledger_failure: Optional[str] = None
    last_journal_failure_at: Optional[str] = None
    last_journal_failure: Optional[str] = None
    unrecorded_executions: int = 0
    last_unrecorded_at: Optional[str] = None
    backlog_hint: bool = False
    replaying: bool = False
    replay_task: Optional["asyncio.Task[Any]"] = None
    last_replay_at: Optional[str] = None
    last_replay_result: Optional[dict[str, Any]] = None
    reported_break: Optional[str] = None


_STATE = _ProcessState()


def _reset_for_tests() -> None:
    """Forget this process's state. Tests only; IRA never calls it."""
    global _STATE
    task = _STATE.replay_task
    if task is not None and not task.done():
        try:
            task.cancel()
        except RuntimeError:
            pass  # its loop is already closed
    _STATE = _ProcessState()
    _HIGH_WATER.clear()


def note_ledger_failure(kind: str, action_id: str, exc: BaseException) -> None:
    _STATE.last_ledger_failure_at = _now()
    _STATE.last_ledger_failure = f"{kind} record for {action_id}: {type(exc).__name__}"


def note_unrecorded_execution(action_id: str, why: str) -> None:
    """An action ran and neither store took its `execution` record."""
    _STATE.unrecorded_executions += 1
    _STATE.last_unrecorded_at = _now()
    logger.critical(
        "TRUST LEDGER AND JOURNAL BOTH FAILED: action %s RAN, and its execution "
        "record is not recorded anywhere durable (%s)", action_id, why)


def _tenant_for_entry() -> str:
    """Whose chain a journalled record belongs on, resolved as the ledger would."""
    from trust.ledger import LedgerNotBound, LedgerTenantMismatch, _binding_for

    try:
        return str(_binding_for(None, None).tenant_id)
    except (LedgerNotBound, LedgerTenantMismatch) as exc:
        raise JournalUnavailable(
            f"no tenant context to journal the record under ({type(exc).__name__})") from exc


def _journal_record_sync(body: dict[str, Any]) -> JournalEntry:
    return open_journal().append(ENTRY_RECORD, body)


async def journal_record(kind: str, *, user_id: str, action_id: str,
                         task_id: Optional[str], record: dict[str, Any],
                         reason: str) -> JournalEntry:
    """Durably journal one ledger record that the ledger did not take.

    Returns only after the entry is fsync'd. Any failure, expected or not, is
    raised as JournalUnavailable: a journal write that failed must never read
    as one that succeeded.
    """
    try:
        body = {"kind": kind, "user_id": user_id, "action_id": action_id,
                "task_id": task_id, "record": record, "tenant_id": _tenant_for_entry(),
                "reason": reason}
        entry = await asyncio.to_thread(_journal_record_sync, body)
    except JournalUnavailable as exc:
        _note_journal_failure(kind, action_id, exc)
        raise
    except Exception as exc:  # noqa: BLE001 — an unexpected failure is still a failure
        _note_journal_failure(kind, action_id, exc)
        raise JournalUnavailable(f"the journal write failed: {type(exc).__name__}") from exc
    _STATE.backlog_hint = True
    logger.warning("ledger journal: %s record for %s held as entry %d (%s); it is "
                   "replayed into the Trust Ledger when the ledger accepts writes",
                   kind, action_id, entry.seq, entry.entry_id)
    return entry


def _note_journal_failure(kind: str, action_id: str, exc: BaseException) -> None:
    _STATE.last_journal_failure_at = _now()
    _STATE.last_journal_failure = f"{kind} record for {action_id}: {exc}"
    logger.error("ledger journal could not take the %s record for %s: %s",
                 kind, action_id, exc)


def probe() -> Optional[str]:
    """Why the journal could not take an entry now, or None if nothing says so.

    Read-only: it creates nothing and writes nothing, so it can only find what
    a read can (permissions, a broken chain, a full disk). It decides which
    refusal message a Class A action gets, and never whether anything runs.
    """
    try:
        journal = open_journal()
        # Advisory: a writer holding the lock (a replay, a recovery action in
        # flight) is answered in PROBE_LOCK_TIMEOUT_SECONDS as "busy", not after
        # the writer's full wait (#250).
        state = journal.read_state(lock_timeout=PROBE_LOCK_TIMEOUT_SECONDS)
        if not state.chain_ok:
            return f"it failed verification ({state.error})"
        existing = journal.directory if journal.directory.exists() else journal.directory.parent
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        fs = os.statvfs(existing)
        if fs.f_bavail * fs.f_frsize < 64 * 1024:
            return f"the disk holding {_show(existing)} is full"
    except JournalUnavailable as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        return f"it could not be checked ({type(exc).__name__})"
    return None


def break_glass_text() -> str:
    return ("To act without IRA, use a shell on the computer IRA runs on, in the "
            f"supracloud-jarvis folder: `{BREAK_GLASS_RECOVER}` restores a backup "
            "(INSTALL.md, \"Moving IRA to another computer\"), and "
            f"`{BREAK_GLASS_CONTAIN}` stops IRA and with it all access. On Windows: "
            "`scripts\\ira-backup.ps1 restore <archive> -Yes` and `stop-ira.ps1`.")


def refusal_detail(*, recovery: bool, journal_error: Optional[str]) -> tuple[str, bool]:
    """The owner-facing refusal, and whether it names the break-glass."""
    if recovery:
        why = journal_error or "it could not be written"
        return ("Not done: neither the Trust Ledger nor its fallback journal could record "
                "this, and IRA does not run an action it cannot record. Nothing was "
                f"changed. The journal: {why}. " + break_glass_text(), True)
    journal_problem = probe()
    base = ("Not done: the Trust Ledger could not record this, so IRA did not run it. "
            "Nothing was changed, and your confirmation is still valid until it "
            "expires: try again once the database is reachable.")
    if journal_problem is None:
        return base, False
    return (base + f" The fallback journal is not usable either ({journal_problem}), so "
            "nothing that needs a record can run. " + break_glass_text(), True)


# ── Replay ────────────────────────────────────────────────────────────────────

def _read_state_sync() -> JournalState:
    return open_journal().read_state()


def _mark_reconciled_sync(entry_id: str, ledger_seq: int, tenant_id: str) -> JournalEntry:
    return open_journal().append(ENTRY_RECONCILED, {
        "entry_id": entry_id, "ledger_seq": ledger_seq, "tenant_id": tenant_id})


async def _report_broken(state: JournalState) -> None:
    """A chain that does not verify: CRITICAL, and once per reason a security event."""
    logger.critical("TRUST LEDGER JOURNAL FAILED VERIFICATION, replay refused: %s (%s)",
                    state.error, _show(state.path))
    if _STATE.reported_break == state.error:
        return
    try:
        from trust.ledger import append_record

        await append_record(
            "security_event", user_id=SYSTEM_USER,
            action_id=f"ledger-journal-{uuid.uuid4().hex[:12]}",
            record={"event": "ledger_journal_verification_failed",
                    "detail": (state.error or "")[:300],
                    "journal": JOURNAL_FILENAME,
                    "verified_entries": len(state.entries),
                    "unreplayed_records": len(state.pending)})
        _STATE.reported_break = state.error
    except Exception as exc:  # noqa: BLE001 — the CRITICAL line above stands
        logger.critical("the security event for the broken journal could not be "
                        "recorded either: %s", type(exc).__name__)


async def replay_pending() -> dict[str, Any]:
    """Verify the journal, then append its unreconciled records to the ledger.

    In journal order; it stops at the first record the ledger does not take,
    so a later record never lands before an earlier one. Safe to run again at
    any point: the ledger returns the row it already holds for an entry id.
    """
    if _STATE.replaying:
        return {"status": "already_running"}
    _STATE.replaying = True
    try:
        result = await _replay()
    finally:
        _STATE.replaying = False
    _STATE.last_replay_at = _now()
    _STATE.last_replay_result = result
    return result


async def _replay() -> dict[str, Any]:
    from core.editions import current_edition
    from core.identity.principal import system_principal
    from trust.ledger import append_record

    try:
        state = await asyncio.to_thread(_read_state_sync)
    except JournalUnavailable as exc:
        logger.error("ledger journal replay: the journal cannot be read: %s", exc)
        return {"status": "unavailable", "detail": str(exc), "replayed": 0}
    if not state.chain_ok:
        # Nothing can be replayed until the owner deals with it; asking again
        # on every ledger write would only re-read a file that will not change.
        _STATE.backlog_hint = False
        await _report_broken(state)
        return {"status": "refused", "detail": state.error, "replayed": 0,
                "pending": len(state.pending)}
    replayed = duplicates = 0
    pending = state.pending
    for entry in pending:
        body = entry.body
        tenant = str(body.get("tenant_id"))
        record = dict(body.get("record") or {})
        record["journalled_at"] = entry.at
        try:
            principal = system_principal(edition=current_edition(),
                                         tenant_id=uuid.UUID(tenant),
                                         reason="ledger-journal-replay")
            row = await append_record(
                str(body["kind"]), user_id=str(body["user_id"]),
                action_id=str(body["action_id"]), task_id=body.get("task_id"),
                record=record, principal=principal, journal_entry_id=entry.entry_id)
        except Exception as exc:  # noqa: BLE001 — stop here; order matters
            logger.warning("ledger journal replay stopped at entry %d (%s): %s",
                           entry.seq, entry.entry_id, type(exc).__name__)
            _STATE.backlog_hint = True
            return {"status": "incomplete", "detail": type(exc).__name__,
                    "replayed": replayed, "pending": len(pending) - replayed - duplicates}
        if row.get("duplicate"):
            duplicates += 1
        else:
            replayed += 1
        try:
            await asyncio.to_thread(_mark_reconciled_sync, entry.entry_id,
                                    int(row["seq"]), tenant)
        except JournalUnavailable as exc:
            # The ledger holds it; the next replay finds it there and marks it.
            logger.error("ledger journal: entry %s is in the ledger as seq %s but could "
                         "not be marked reconciled: %s", entry.entry_id, row["seq"], exc)
            _STATE.backlog_hint = True
            return {"status": "incomplete", "detail": str(exc), "replayed": replayed,
                    "pending": len(pending) - replayed - duplicates}
    _STATE.backlog_hint = False
    if replayed or duplicates:
        logger.warning("ledger journal: %d record(s) replayed into the Trust Ledger "
                       "(%d already there)", replayed, duplicates)
    return {"status": "ok", "replayed": replayed, "already_in_ledger": duplicates,
            "pending": 0}


def ledger_accepted_write() -> Optional["asyncio.Task[Any]"]:
    """The ledger just took a write: replay the backlog, if there is one.

    Called by `trust.ledger.append_record` after it commits. Cheap when there
    is nothing to do. The replay runs as its own task so the write that
    triggered it returns at once.
    """
    if not _STATE.backlog_hint or _STATE.replaying:
        return None
    task = _STATE.replay_task
    if task is not None and not task.done():
        return task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(replay_pending(), name="ledger-journal-replay")
    task.add_done_callback(_log_task_failure)
    _STATE.replay_task = task
    return task


def _log_task_failure(task: "asyncio.Task[Any]") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("ledger journal replay failed: %s", exc)


async def startup_reconcile() -> dict[str, Any]:
    """At startup: verify the journal and replay what it holds. Never raises."""
    try:
        state = await asyncio.to_thread(_read_state_sync)
    except JournalUnavailable as exc:
        logger.error("Trust Ledger journal unusable at startup: %s. While the ledger "
                     "is down, backup restore and access revoke will refuse.", exc)
        return {"status": "unavailable", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.error("Trust Ledger journal check failed at startup: %s", exc)
        return {"status": "error", "detail": type(exc).__name__}
    if not state.exists:
        return {"status": "ok", "replayed": 0, "pending": 0}
    if state.torn_tail:
        logger.warning("Trust Ledger journal ends with an incomplete write (%d bytes); "
                       "it is not trusted", state.torn_tail_bytes)
    if not state.chain_ok:
        await _report_broken(state)
        return {"status": "refused", "detail": state.error}
    if not state.pending:
        return {"status": "ok", "replayed": 0, "pending": 0}
    _STATE.backlog_hint = True
    try:
        return await replay_pending()
    except Exception as exc:  # noqa: BLE001
        logger.error("Trust Ledger journal replay failed at startup: %s", exc)
        return {"status": "error", "detail": type(exc).__name__}


# ── The health report ─────────────────────────────────────────────────────────

def health_snapshot() -> dict[str, Any]:
    """What `/api/v1/trust/ledger/health` returns. Reads; never writes."""
    out: dict[str, Any] = {
        "journal_path": _show(journal_path()),
        "journal_available": True,
        "journal_error": None,
        "journal_exists": False,
        "chain_verified": None,
        "chain_error": None,
        "torn_tail": False,
        "torn_tail_bytes": 0,
        "torn_writes_marked": 0,
        "entries": 0,
        "backlog": 0,
        "backlog_actions": 0,
        "last_journal_entry_at": None,
        "last_ledger_failure_at": _STATE.last_ledger_failure_at,
        "last_ledger_failure": _STATE.last_ledger_failure,
        "last_journal_failure_at": _STATE.last_journal_failure_at,
        "last_journal_failure": _STATE.last_journal_failure,
        "unrecorded_executions": _STATE.unrecorded_executions,
        "last_unrecorded_at": _STATE.last_unrecorded_at,
        "last_replay_at": _STATE.last_replay_at,
        "last_replay": _STATE.last_replay_result,
        "break_glass": list(BREAK_GLASS),
    }
    try:
        state = open_journal().read_state()
    except JournalUnavailable as exc:
        out["journal_available"] = False
        out["journal_error"] = str(exc)
        state = None
    if state is not None:
        records = state.records
        pending = state.pending
        out.update({
            "journal_exists": state.exists,
            "chain_verified": (state.chain_ok if (state.exists or not state.chain_ok)
                               else None),
            "chain_error": state.error,
            "torn_tail": state.torn_tail,
            "torn_tail_bytes": state.torn_tail_bytes,
            "torn_writes_marked": state.torn_markers,
            "entries": len(records),
            "backlog": len(pending),
            "backlog_actions": len({str(e.body.get("action_id")) for e in pending}),
            "last_journal_entry_at": records[-1].at if records else None,
        })
        if pending and state.chain_ok:
            _STATE.backlog_hint = True
    warning = bool(out["backlog"] or out["chain_verified"] is False or out["torn_tail"]
                   or out["unrecorded_executions"])
    out["warning"] = warning
    out["message"] = _health_message(out) if warning else None
    return out


def _health_message(h: dict[str, Any]) -> str:
    parts = []
    if h["backlog"]:
        n = h["backlog_actions"]
        parts.append(f"{n} confirmed action{'s are' if n != 1 else ' is'} not yet in the "
                     f"Trust Ledger ({h['backlog']} record{'s' if h['backlog'] != 1 else ''} "
                     "held in the fallback journal, added when the ledger accepts writes).")
    if h["chain_verified"] is False:
        parts.append(f"The fallback journal failed verification: {h['chain_error']}. Its "
                     "entries are not added to the ledger, and this is recorded as a "
                     "security event.")
    if h["torn_tail"]:
        parts.append("The fallback journal ends with an incomplete write: IRA stopped "
                     "while writing it. The entries before it verify; the incomplete "
                     "one is not trusted.")
    if h["unrecorded_executions"]:
        n = h["unrecorded_executions"]
        parts.append(f"{n} action{'s' if n != 1 else ''} ran since IRA started whose "
                     "result neither the ledger nor the journal could record. The server "
                     "log has the details (CRITICAL).")
    return " ".join(parts)


__all__ = [
    "JournalUnavailable", "JournalEntry", "JournalState", "LedgerJournal",
    "JOURNAL_DIR_ENV", "JOURNAL_FILENAME", "BREAK_GLASS", "BREAK_GLASS_RECOVER",
    "BREAK_GLASS_CONTAIN", "journal_dir", "journal_path", "derive_key", "key_id",
    "open_journal", "journal_record", "note_ledger_failure", "note_unrecorded_execution",
    "probe", "refusal_detail", "break_glass_text", "replay_pending",
    "ledger_accepted_write", "startup_reconcile", "health_snapshot",
]
