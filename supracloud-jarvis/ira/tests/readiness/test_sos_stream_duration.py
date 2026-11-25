"""The SOS stream stays open for five minutes in production.

The database test of the stream route shortens `STREAM_MAX_SECONDS` so it does
not wait five minutes for the test client to finish buffering the body. This
file pins the production value and shows the stream really reads the setting
when it starts, so shortening it in a test changes only that test.
"""
from __future__ import annotations

import asyncio
import inspect

from care import sos


def test_production_keeps_the_stream_open_for_five_minutes():
    assert sos.STREAM_MAX_SECONDS == 300.0
    assert inspect.signature(sos.stream).parameters["max_seconds"].default is None


def test_the_stream_reads_the_duration_when_it_starts(monkeypatch):
    calls = []

    async def fake_current(principal, device):
        calls.append(1)
        return {"state": "idle"}

    monkeypatch.setattr(sos, "current_for_device", fake_current)
    monkeypatch.setattr(sos, "STREAM_MAX_SECONDS", 0.05)

    async def run():
        return [e async for e in sos.stream(None, {}, poll_s=0.01)]

    events = asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert events == [{"state": "idle"}], "the current state first, then nothing while it is unchanged"
    assert len(calls) >= 2, "it kept polling until the duration ran out"
