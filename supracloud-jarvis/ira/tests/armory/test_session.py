"""Unit tests for the in-RAM unlock session state machine (auto-lock)."""
import pytest

from armory.manifest import load_manifest
from armory.session import VaultContents, VaultSession
from armory.whitelist import parse_whitelist

_MANIFEST = '{"tools":[{"id":"x","image":"kali","cmd":"nmap {target}","risk":"low"}]}'


def _contents():
    return VaultContents(manifest=load_manifest(_MANIFEST),
                         whitelist=parse_whitelist("localhost\n"))


def test_starts_locked():
    s = VaultSession(ttl_seconds=10)
    assert s.is_unlocked() is False
    with pytest.raises(RuntimeError):
        s.contents()


def test_unlock_then_lock():
    s = VaultSession(ttl_seconds=10)
    s.unlock(_contents())
    assert s.is_unlocked() is True
    assert len(s.contents().manifest) == 1
    s.lock()
    assert s.is_unlocked() is False


def test_idle_auto_lock_with_injected_clock():
    now = {"t": 1000.0}
    s = VaultSession(ttl_seconds=300, _clock=lambda: now["t"])
    s.unlock(_contents())
    now["t"] += 299
    assert s.is_unlocked() is True          # still within TTL
    assert s.seconds_remaining() == 1
    now["t"] += 2                            # cross the TTL boundary
    assert s.is_unlocked() is False         # auto-locked
    assert s.seconds_remaining() == 0


def test_touch_resets_idle_timer():
    now = {"t": 0.0}
    s = VaultSession(ttl_seconds=100, _clock=lambda: now["t"])
    s.unlock(_contents())
    now["t"] += 90
    s.touch()                                # activity resets the clock
    now["t"] += 90
    assert s.is_unlocked() is True           # 90s since last activity < 100
