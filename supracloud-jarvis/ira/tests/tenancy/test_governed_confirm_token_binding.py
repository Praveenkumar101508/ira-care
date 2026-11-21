"""The governed confirm honours a token only for the spec it was staged for.

`confirm_action` has two ways to the side effect: the plain one and, when a
principal and a governed (domain, action) pair are given, `_confirm_governed`,
which consumes the token inside the gateway's executor. Both now pass the
spec's kind and intended action to `ApprovalGuardrail.confirm`. The plain path
is tested without a database (tests/test_approval_token_binding.py); this file
runs the governed one end to end, because only a database reaches it:

    confirm_action → _confirm_governed → action_bridge.execute_spec
                   → Constitution → ticket → gateway → executor → token

Only the side effect is a double.
"""
from __future__ import annotations

import os
import uuid

import pytest

pytestmark = [pytest.mark.asyncio]

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

from actions import gated_execution  # noqa: E402
from actions.shadow_world import ActionSpec  # noqa: E402
from core.governance.store import save_rule  # noqa: E402
from core.identity.principal import system_principal  # noqa: E402
from utils.approval import ApprovalGuardrail  # noqa: E402

GOVERNED = ("email", "send")

#: Tenants this module created; their memberships are removed afterwards
#: (test_zz_no_fixture_residue.py fails the directory otherwise). The tenants
#: and their ledger rows stay: the ledger is append-only.
_CREATED: list[uuid.UUID] = []


@pytest.fixture(autouse=True)
async def app_pool():
    import utils.db as db

    pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db._pool = db._pool, pool
    yield
    db._pool = previous
    await pool.close()


@pytest.fixture(autouse=True)
def private_edition(monkeypatch):
    monkeypatch.delenv("IRA_EDITION", raising=False)


@pytest.fixture(autouse=True)
def email_send_simulates_ok(monkeypatch):
    """The bridge re-simulates from the Shadow World registry, which has no
    simulator for email_send: without one, every governed send is refused, and
    a refusal would prove nothing about tokens. Scoped to this module's tests."""
    import actions.shadow_world as sw

    async def _ok(spec):
        return {"ok": True, "detail": "test simulator: safe to send"}

    monkeypatch.setattr(sw, "_SIMULATORS", {**sw._SIMULATORS, "email_send": _ok})


@pytest.fixture(autouse=True)
async def _remove_membership_residue():
    before = len(_CREATED)
    yield
    mine, _CREATED[before:] = _CREATED[before:], []
    if not mine:
        return
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(
            "DELETE FROM tenant_memberships WHERE tenant_id = ANY($1::uuid[])", mine)
    finally:
        await conn.close()


async def _allowed_principal():
    """A real tenant and user whose Constitution allows sending email."""
    from dataclasses import replace

    tenant, user = uuid.uuid4(), uuid.uuid4()
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute("INSERT INTO tenants (id, name, slug) VALUES ($1,$2,$2)",
                           tenant, f"tokbind-{tenant.hex[:10]}")
        await conn.execute(
            "INSERT INTO users (id, email, display_name) VALUES ($1,$2,$3)",
            user, f"tokbind-{user.hex[:10]}@example.test", "token-binding")
        await conn.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role, status) "
            "VALUES ($1,$2,'admin','active')", tenant, user)
    finally:
        await conn.close()
    _CREATED.append(tenant)
    principal = replace(
        system_principal(tenant_id=tenant, reason="token-binding-governed"), user_id=user)
    await save_rule(principal, {"domain": GOVERNED[0], "action": GOVERNED[1],
                                "effect": "allow", "priority": 10,
                                "label": "token binding: allow email send"})
    return principal


def _spec(recipient: str) -> ActionSpec:
    return ActionSpec(
        kind="email_send", risk="high", requested_by="token-binding",
        intended_action=f"Send the quarterly summary to {recipient}",
        objects_affected=[f"mailto:{recipient}"],
        action_id=f"tokbind-{uuid.uuid4().hex[:12]}",
    )


def _staged(guard: ApprovalGuardrail, spec: ActionSpec, ran: list[str]) -> str:
    """Draft the token the way stage_action does: bound to kind and preview."""
    async def _send():
        ran.append(spec.intended_action)
        return {"status": "sent"}

    return guard.draft(owner=spec.requested_by, action=spec.kind,
                       preview=spec.intended_action, execute=_send).token


async def _simulated_ok(spec: ActionSpec) -> dict:
    """No simulator is registered for email_send; without this one the governed
    path refuses every send, and a refusal would prove nothing about tokens."""
    return {"ok": True, "detail": "test simulator: safe to send"}


async def _confirm(spec: ActionSpec, token: str, principal, guard: ApprovalGuardrail) -> dict:
    return await gated_execution.confirm_action(
        spec, token=token, principal=principal, governed=GOVERNED, guard=guard,
        simulator=_simulated_ok)


async def test_a_governed_confirm_runs_the_spec_its_token_was_staged_for():
    """The positive control: without it, the refusal below could be the
    governed path refusing everything."""
    principal, guard, ran = await _allowed_principal(), ApprovalGuardrail(), []
    spec = _spec("finance@example.test")
    result = await _confirm(spec, _staged(guard, spec, ran), principal, guard)
    assert ran == [spec.intended_action], result


async def test_a_governed_confirm_refuses_a_token_staged_for_another_recipient():
    principal, guard, ran = await _allowed_principal(), ApprovalGuardrail(), []
    staged = _spec("finance@example.test")
    token = _staged(guard, staged, ran)

    other = _spec("someone-else@example.test")
    result = await _confirm(other, token, principal, guard)
    assert ran == [], "a token staged for one recipient sent mail to another"
    assert result.get("governed") is False, result

    # Spent by the refusal: it no longer runs even the spec it was staged for.
    await _confirm(staged, token, principal, guard)
    assert ran == [], "a refused token stayed usable"
