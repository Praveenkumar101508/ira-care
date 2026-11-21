"""
Files that hold a person's data are created readable by the service account only.

A database dump, the restore's decompressed SQL and an uploaded archive are the
whole database in clear: memories, conversations, the lot. `open()` creates
files with the process umask, which on a native install is 022 — readable by
every account on the host. These helpers create at 0600 (and tighten a file that
already existed under the same name) and keep the directory at 0700, which is
what `scripts/ira-backup.sh` already does with `umask 077` and `chmod 600`.

On Windows the mode bits govern only the read-only flag; the directory's ACL is
a separate matter and is not claimed here.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("ira.private_files")


def private_opener(path: str | bytes | os.PathLike, flags: int) -> int:
    """An ``opener=`` for ``open()``: 0600 on creation, tightened if it existed."""
    fd = os.open(path, flags, 0o600)
    if hasattr(os, "fchmod"):
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            # A filesystem without POSIX modes (FAT, some mounts): the create
            # mode is all it offers, and the file is still ours.
            pass
    return fd


def ensure_private_dir(path: Path) -> Path:
    """Create *path* if needed and keep it 0700, whatever created it before."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError as e:
        # Owned by another account (a root-created volume): nothing better to
        # do, but say so rather than pretend the directory is private.
        logger.warning("could not make %s private: %s", path, e)
    return path
