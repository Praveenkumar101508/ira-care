"""
armory/session.py — the in-RAM unlock state of the Armory.

The vault's decrypted contents (manifest, whitelist, compose file) exist ONLY
inside a live :class:`VaultSession`, never on disk. The session also enforces the
two auto-lock rules from the spec:

  * idle timeout — locked after ``ttl_seconds`` of no activity;
  * task completion — the gatekeeper may lock immediately after a tool run.

``lock()`` drops every reference to decrypted material so a locked session holds
nothing sensitive. Time is injected (``now``) so the state machine is testable
without sleeping. This module is pure stdlib.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from armory.manifest import Manifest
from armory.whitelist import Whitelist

DEFAULT_TTL_SECONDS = 300  # 5 minutes of inactivity, per spec


@dataclass
class VaultContents:
    """The decrypted vault, held in RAM only. Never serialised to disk."""

    manifest: Manifest
    whitelist: Whitelist
    compose_yaml: str = ""


@dataclass
class VaultSession:
    """Tracks whether the Armory is unlocked and for how long.

    Not a security boundary on its own — the master key and the encrypted blob
    are. This is the runtime latch the gatekeeper consults, and it fails closed:
    an expired or never-unlocked session reports ``locked``.
    """

    ttl_seconds: int = DEFAULT_TTL_SECONDS
    _contents: Optional[VaultContents] = field(default=None, repr=False)
    _unlocked_at: float = 0.0
    _last_activity: float = 0.0
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    # ── state transitions ─────────────────────────────────────────────────────

    def unlock(self, contents: VaultContents) -> None:
        """Enter the unlocked state holding the decrypted contents."""
        now = self._clock()
        self._contents = contents
        self._unlocked_at = now
        self._last_activity = now

    def lock(self) -> None:
        """Drop all decrypted material and return to the locked state."""
        self._contents = None
        self._unlocked_at = 0.0
        self._last_activity = 0.0

    def touch(self) -> None:
        """Record activity, resetting the idle timer (no-op when locked)."""
        if self._contents is not None:
            self._last_activity = self._clock()

    # ── queries ───────────────────────────────────────────────────────────────

    def is_expired(self) -> bool:
        if self._contents is None:
            return True
        return (self._clock() - self._last_activity) >= self.ttl_seconds

    def is_unlocked(self) -> bool:
        """True only if unlocked AND not idle-expired. Auto-locks on expiry."""
        if self._contents is None:
            return False
        if self.is_expired():
            self.lock()
            return False
        return True

    def seconds_remaining(self) -> int:
        if not self.is_unlocked():
            return 0
        return max(0, int(self.ttl_seconds - (self._clock() - self._last_activity)))

    def contents(self) -> VaultContents:
        """Return the decrypted contents, or raise if not currently unlocked."""
        if not self.is_unlocked():
            raise RuntimeError("armory is locked")
        assert self._contents is not None
        return self._contents


__all__ = ["VaultSession", "VaultContents", "DEFAULT_TTL_SECONDS"]
