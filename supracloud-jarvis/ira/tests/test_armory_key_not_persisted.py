"""The Armory master key typed into the chat is never stored or replayed.

Security review of 10 November 2026: `agents/armory.py` read the key from the live
message (correct) and `store_interaction` / the stream route then saved that message
verbatim, embedded it, and replayed it as history. Every route persists through
`memory/store.py::save_message`, which now redacts the key for user messages.
"""
from __future__ import annotations

import contextlib
import os

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from utils.secret_redaction import MASTER_KEY_PLACEHOLDER, extract_master_key, redact_master_key  # noqa: E402

PHRASE = "SYNTHETIC-correct-horse-battery-7781"
UNLOCKS = [
    f"unlock the armory with key {PHRASE}",
    f"Open the vault using the master password: {PHRASE}",
    f"master key: {PHRASE}",
    f"unlock = {PHRASE}",
    f"please unlock the armory, key '{PHRASE}'",
]


@pytest.mark.parametrize("message", UNLOCKS)
def test_the_key_is_read_from_the_live_message_and_redacted_for_storage(message):
    assert extract_master_key(message) == PHRASE
    stored = redact_master_key(message)
    assert PHRASE not in stored and MASTER_KEY_PLACEHOLDER in stored, stored
    assert stored.lower().startswith(message.strip().split()[0].lower()), "the rest of the message is kept"


@pytest.mark.parametrize("message", ["what is the armory status?", "lock the vault", "hello", "",
                                     "remind me to unlock the front door at 8"])
def test_ordinary_messages_are_stored_unchanged(message):
    assert extract_master_key(message) is None
    assert redact_master_key(message) == message


def test_the_agent_and_the_store_share_the_same_patterns():
    import agents.armory as armory
    from utils import secret_redaction
    assert armory._UNLOCK_RE is secret_redaction.UNLOCK_RE
    assert armory._UNLOCK_KEY_ONLY_RE is secret_redaction.UNLOCK_KEY_ONLY_RE
    assert armory._extract_master_key(UNLOCKS[0]) == PHRASE


@pytest.mark.asyncio
async def test_save_message_persists_and_embeds_the_redacted_text(monkeypatch):
    import memory.store as store

    recorded = {}

    class _Conn:
        async def execute(self, sql, *args):
            recorded["sql_args"] = args

    @contextlib.asynccontextmanager
    async def _conn(principal):
        yield _Conn()

    async def _embed(principal, msg_id, content, source_type, *, user_id):
        recorded["embedded"] = content

    monkeypatch.setattr("core.identity.db_context.workspace_connection", _conn)
    monkeypatch.setattr(store, "_store_embedding", _embed)

    await store.save_message(object(), "00000000-0000-0000-0000-000000000001", "user", UNLOCKS[0], user_id="owner")
    stored_content = recorded["sql_args"][3]
    assert PHRASE not in stored_content and MASTER_KEY_PLACEHOLDER in stored_content
    for _ in range(20):  # let the background embedding task run
        import asyncio
        await asyncio.sleep(0)
    assert PHRASE not in recorded.get("embedded", "") and MASTER_KEY_PLACEHOLDER in recorded.get("embedded", "")

    # An assistant message is not touched, even if it quotes the word "key".
    await store.save_message(object(), "00000000-0000-0000-0000-000000000001", "assistant", "Armory unlocked. 14 tools available.", user_id="owner")
    assert recorded["sql_args"][3] == "Armory unlocked. 14 tools available."
