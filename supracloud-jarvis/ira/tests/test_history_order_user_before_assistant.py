"""Replayed history has the user's question before the assistant's answer.

`POST /api/v1/chat/stream` (and the vision and document streams) saved the user
row and the assistant row of a turn as two concurrent tasks after the done
frame. `messages.created_at` defaults to `NOW()` at insert and
`get_recent_messages` replays `ORDER BY created_at`, so whichever insert landed
first came back first: in 14 of 25 replayed follow-up turns of the November
2026 acceptance run the model saw the answer before the question (roles
`sssauu` instead of `sssuau`). `api.routes.chat._save_turn` now awaits the user
row before the assistant row, in one background task started after the done
frame, so the stream's bytes are unchanged.

The route test spins the chat app with the stand-in model of
`tests/test_injection_framing_routes.py` and an in-memory store that stamps
each row when its insert lands and reads back in `created_at` order, as the
real store does. The store's user insert is slower than its assistant insert,
so two concurrent saves land assistant-first every time: the planted defect
fails these tests deterministically, not by chance.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import uuid

import httpx

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from tests.test_injection_framing_routes import ANSWER, _owner_app, stand_in  # noqa: E402, F401, F811

TURN_1 = "SYNTHETIC: my dog is called Waffle. Say hi to him."
TURN_2 = "SYNTHETIC: what is my dog called?"
#: The user insert takes longer than the assistant insert, so concurrent saves
#: land assistant-first.
USER_INSERT_DELAY_S = 0.05


class _Store:
    """`save_message` / `get_recent_messages` over one list, `created_at` stamped
    when the insert lands, read back `ORDER BY created_at` as memory/store.py does."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._clock = itertools.count(1)

    async def save_message(self, _p, conv, role, content, **_kw) -> str:
        if role == "user":
            await asyncio.sleep(USER_INSERT_DELAY_S)
        self.rows.append({"conv": conv, "role": role, "content": content,
                          "created_at": next(self._clock)})
        return str(uuid.uuid4())

    async def get_recent_messages(self, _p, conv, limit=10) -> list[dict]:
        rows = sorted((r for r in self.rows if r["conv"] == conv), key=lambda r: r["created_at"])
        return [{"role": r["role"], "content": r["content"]} for r in rows[-limit:]]

    def install(self, monkeypatch) -> "_Store":
        import api.routes.chat as chatmod
        monkeypatch.setattr("memory.store.save_message", self.save_message)
        monkeypatch.setattr(chatmod, "get_recent_messages", self.get_recent_messages)
        return self

    async def wait_for(self, n: int) -> None:
        for _ in range(200):  # the route saves the turn in a background task
            if len(self.rows) >= n:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"{len(self.rows)} rows saved, {n} expected: {self.rows}")


# ── The helper itself ─────────────────────────────────────────────────────────


async def test_save_turn_saves_the_user_row_before_the_assistant_row(monkeypatch):
    from api.routes.chat import _save_turn_in_background

    store = _Store().install(monkeypatch)
    task = _save_turn_in_background(None, "conv-1", TURN_1, ANSWER, user_id="u",
                                    model_used="qwen3-fast", latency_ms=12)
    await task

    assert [r["role"] for r in store.rows] == ["user", "assistant"], store.rows
    user, assistant = store.rows
    assert user["created_at"] < assistant["created_at"], store.rows
    assert await store.get_recent_messages(None, "conv-1") == [
        {"role": "user", "content": TURN_1},
        {"role": "assistant", "content": ANSWER},
    ]


async def test_save_turn_still_saves_the_assistant_row_when_the_user_save_fails(monkeypatch, caplog):
    from api.routes.chat import _save_turn_in_background

    store = _Store().install(monkeypatch)
    calls: list[str] = []

    async def failing_user_save(p, conv, role, content, **kw):
        calls.append(role)
        if role == "user":
            raise RuntimeError("SYNTHETIC: insert refused")
        return await store.save_message(p, conv, role, content, **kw)

    monkeypatch.setattr("memory.store.save_message", failing_user_save)
    with caplog.at_level("WARNING", logger="ira.chat"):
        await _save_turn_in_background(None, "conv-2", TURN_1, ANSWER, user_id="u")

    assert calls == ["user", "assistant"]
    assert [r["role"] for r in store.rows] == ["assistant"]
    assert any("save_message failed" in m for m in caplog.messages), caplog.messages


# ── The stream route, two turns on one conversation ──────────────────────────


async def _stream(client, message: str, session_id: str) -> str:
    r = await client.post("/api/v1/chat/stream", json={"message": message, "session_id": session_id})
    assert r.status_code == 200, r.text
    frames = [json.loads(line[5:].strip()) for line in r.text.splitlines() if line.startswith("data:")]
    assert not [f for f in frames if "error" in f], frames
    [done] = [f for f in frames if f.get("done")]
    assert done["session_id"] == session_id
    return "".join(f.get("token", "") for f in frames).strip()


async def test_the_stream_route_replays_turn_one_as_user_then_assistant(stand_in, monkeypatch):  # noqa: F811
    app = _owner_app(monkeypatch)
    store = _Store().install(monkeypatch)
    session = f"s-{uuid.uuid4()}"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ira.test", timeout=60) as client:
        assert await _stream(client, TURN_1, session) == ANSWER
        await store.wait_for(2)
        # Stored user-first, with the earlier timestamp.
        assert [r["role"] for r in store.rows] == ["user", "assistant"], store.rows
        assert store.rows[0]["created_at"] < store.rows[1]["created_at"], store.rows

        assert await _stream(client, TURN_2, session) == ANSWER
        await store.wait_for(4)

    first, second = stand_in.answers
    # Turn 1 was shown with no history: one system prompt, one user turn.
    assert [m["role"] for m in first["messages"] if m["role"] != "system"] == ["user"]
    # Turn 2 replays turn 1 as [user, assistant], then the current question.
    shown = [m for m in second["messages"] if m["role"] != "system"]
    assert [m["role"] for m in shown] == ["user", "assistant", "user"], [m["role"] for m in shown]
    assert shown[0]["content"] == TURN_1
    assert shown[1]["content"].strip() == ANSWER
    assert shown[2]["content"] == TURN_2
    # Turn 2 is stored user-first too.
    assert [r["role"] for r in store.rows] == ["user", "assistant", "user", "assistant"], store.rows
    assert [r["created_at"] for r in store.rows] == sorted(r["created_at"] for r in store.rows)
