"""The confirm ledger records the shape of an executor's result, never its payload.

`actions/gated_execution.py::confirm_action` writes three records to the trust
ledger — intent, approval, execution — and the execution record carried
`_summarize(res.result)`. That helper passed strings through **unbounded** and
copied every dict value, so an executor that echoes what it did (a mail sender
returning the body, a tool returning a token) put the payload into
`trust_ledger.record`, a JSONB column readable by every tenant admin and
security admin, and kept for as long as the ledger is. The governed path's
`core/governance/gateway.py::_summarise` already records only the shape, because
023 documents the field as "Result summary only. Never credentials, never raw
connector payloads." This brings the private path to the same rule.

DB-free, on the harness `tests/test_gated_execution.py` uses: the ledger append
is captured instead of hitting Postgres, and a fresh ApprovalGuardrail per test
isolates tokens.

Negative control (planted and watched fail): restore the old `_summarize`
(`return result` for strings, `{k: _summarize(v) ...}` for dicts) →
`test_an_executor_that_echoes_its_payload_is_not_ledgered` and
`test_a_long_string_result_is_not_ledgered` fail.
"""
from __future__ import annotations

import asyncio
import json
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import pytest  # noqa: E402

import actions.gated_execution as ge  # noqa: E402
from actions.shadow_world import ActionSpec, register_simulator  # noqa: E402
from utils.approval import ApprovalGuardrail  # noqa: E402

#: Never a real secret: a marker whose presence anywhere in a ledger record is the defect.
PAYLOAD_MARKER = "PAYLOAD-MARKER-7f3a9c"


@pytest.fixture(autouse=True)
def ledger_capture(monkeypatch):
    recorded = []
    seq = {"n": 0}

    async def fake_append(kind, *, user_id, action_id, task_id=None, record=None):
        seq["n"] += 1
        entry = {"seq": seq["n"], "kind": kind, "user_id": user_id,
                 "action_id": action_id, "record": record or {}}
        recorded.append(entry)
        return entry

    monkeypatch.setattr("trust.ledger.append_record", fake_append)
    return recorded


@pytest.fixture(autouse=True)
def _sim():
    async def ok_sim(spec):
        return {"ok": True, "detail": "dry-run ok"}
    register_simulator("email_send", ok_sim)


def _spec():
    return ActionSpec(kind="email_send", intended_action="send a note",
                      requested_by="owner", risk="low")


def _guard():
    return ApprovalGuardrail(ttl_seconds=300, now=lambda: 1000.0)


async def _stage_and_confirm(result):
    g = _guard()
    staged = await ge.stage_action(_spec(), is_owner=True, execute=lambda: result, guard=g)
    assert staged["status"] == "confirmation_required", staged
    return await ge.confirm_action(_spec(), token=staged["token"], guard=g)


def _ledger_text(records) -> str:
    return json.dumps([r["record"] for r in records], default=str)


def test_an_executor_that_echoes_its_payload_is_not_ledgered(ledger_capture):
    payload = {"status": "sent", "to": "someone@example.test",
               "subject": "hello", "body": PAYLOAD_MARKER, "token": PAYLOAD_MARKER}

    out = asyncio.run(_stage_and_confirm(payload))

    assert out["status"] == "executed"
    assert out["result"] == payload, "the caller still gets the real result; only the ledger is summarised"
    kinds = [r["kind"] for r in ledger_capture]
    assert kinds == ["action", "approval", "execution"]
    assert PAYLOAD_MARKER not in _ledger_text(ledger_capture), (
        "the executor's payload reached the trust ledger")
    execution = ledger_capture[-1]["record"]["result"]
    assert execution["status"] == "sent", "the outcome is still recorded"
    assert execution["shape"] == "object with 5 field(s)"


def test_a_long_string_result_is_not_ledgered(ledger_capture):
    out = asyncio.run(_stage_and_confirm(PAYLOAD_MARKER * 50))

    assert out["status"] == "executed"
    text = _ledger_text(ledger_capture)
    assert PAYLOAD_MARKER not in text
    assert len(json.dumps(ledger_capture[-1]["record"])) < 200, "the record stays small"


@pytest.mark.parametrize("result, shape", [
    (None, "no result"),
    (True, "True"),
    (3, "3"),
    (["a", "b"], "2 item(s)"),
    ({"status": "ok"}, "object with 1 field(s)"),
    ("done", "str"),
])
def test_summarize_records_only_the_shape(result, shape):
    summary = ge._summarize(result)
    assert summary["shape"] == shape
    assert PAYLOAD_MARKER not in json.dumps(summary)


def test_summarize_keeps_a_short_status_and_nothing_else():
    summary = ge._summarize({"status": "x" * 500, "body": PAYLOAD_MARKER})
    assert set(summary) == {"shape", "status"}
    assert len(summary["status"]) <= 40
    assert PAYLOAD_MARKER not in json.dumps(summary)

    assert "status" not in ge._summarize({"status": {"nested": PAYLOAD_MARKER}}), (
        "a status that is not a scalar is not copied")
