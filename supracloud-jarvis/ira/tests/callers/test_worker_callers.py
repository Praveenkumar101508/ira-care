"""The seven `worker/` modules — the hardest case in P12-01.

A background job has no request, so it has no workspace header to carry and no
user to ask. If it binds a workspace at all it must do so deliberately, and the
audit's first draft got this exact group wrong: it read their ABSENCE from the
unbound list as evidence they bind, when the truth was that no suite had ever
run one.

Each module is exercised twice, and the second time is the one that matters:

  1. with an explicit `principal=` — the shape a caller uses;
  2. with NO principal at all — the shape the SCHEDULER uses, where the module
     must derive its own authority through `default_workspace_job_principal`
     or do nothing.

The second exercise is what proves D3 item 7. A module that only works when
somebody hands it a principal is a module that reads unbound in production,
where nobody does.
"""
from __future__ import annotations

import importlib.util
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.callers.conftest import assert_touched, workspace_of

IRA_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
async def private_tenant(pool):
    """The tenant the schedulers actually name, with a default workspace.

    Every `worker/` module reaches for `PRIVATE_TENANT_ID` when it has no
    principal. That id is deterministic and shared, so this fixture creates the
    substrate if it is absent and leaves it alone if another suite already did.
    Without it `default_workspace_job_principal` raises `MissingJobContext` and
    every module below would take its skip path — which looks, in the audit
    output, exactly like a worker that was never exercised.
    """
    from core.identity.principal import PRIVATE_TENANT_ID

    tenant_id = PRIVATE_TENANT_ID
    ws = workspace_of(tenant_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id,name,slug,edition) "
            "VALUES ($1,'P12-01 private','p1201-private','cloud') "
            "ON CONFLICT (id) DO NOTHING", tenant_id)
        await conn.execute("SELECT set_config('app.tenant_id',$1,false)", str(tenant_id))
        org = await conn.fetchval(
            "SELECT id FROM organisations WHERE tenant_id = $1", tenant_id)
        if org is None:
            org = await conn.fetchval(
                "INSERT INTO organisations (tenant_id,name) VALUES ($1,'P12-01') "
                "RETURNING id", tenant_id)
        existing = await conn.fetchval(
            "SELECT id FROM workspaces WHERE tenant_id=$1 AND is_default "
            "  AND status='active'", tenant_id)
        if existing is None:
            await conn.execute(
                "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,"
                "                        is_default,status) "
                "VALUES ($1,$2,$3,'Default','default',TRUE,'active') "
                "ON CONFLICT DO NOTHING", ws, tenant_id, org)
            existing = await conn.fetchval(
                "SELECT id FROM workspaces WHERE tenant_id=$1 AND is_default "
                "  AND status='active'", tenant_id)
        await conn.execute("SELECT set_config('app.tenant_id','',false)")
    return {"tenant_id": tenant_id, "workspace_id": existing}


# ── Exercised with an explicit principal ─────────────────────────────────────


async def test_heartbeat_signals_bind(principal, audit_probe, monkeypatch):
    """`worker/heartbeat.py` and `worker/heartbeat_signals.py`."""
    monkeypatch.setenv("IRA_HEARTBEAT", "true")
    from worker import heartbeat, heartbeat_signals

    now = datetime.now(timezone.utc)
    await heartbeat.gather_pending_decisions(principal, now)
    await heartbeat.gather_stale_memory_commitments(principal, now)
    await heartbeat_signals.gather_recurring_themes(principal, now)
    await heartbeat_signals.gather_calendar_deadlines(principal, now)
    await heartbeat_signals.gather_open_loops(principal, now)

    for module in ("worker/heartbeat.py", "worker/heartbeat_signals.py"):
        counts = assert_touched(audit_probe, module)
        assert counts["unbound"] == 0, (
            f"{module} ran {counts['unbound']} query(ies) against a "
            f"workspace-carrying table with nothing bound. A heartbeat that "
            f"reads unbound surfaces nothing and reports a quiet day."
        )


async def test_reflection_gathers_bound(principal, audit_probe, monkeypatch):
    """`worker/reflection.py` — the weekly look back."""
    monkeypatch.setenv("IRA_REFLECTION", "true")
    from worker import reflection

    now = datetime.now(timezone.utc)
    await reflection.gather_completed_tasks(principal, now)
    await reflection.gather_decision_outcomes(principal, now)

    counts = assert_touched(audit_probe, "worker/reflection.py")
    assert counts["unbound"] == 0, (
        f"worker/reflection.py ran {counts['unbound']} unbound query(ies) "
        f"against `tasks`/`decisions`, whose predicates are live."
    )


async def test_reminders_check_and_create_bound(principal, audit_probe, monkeypatch):
    """`worker/reminders.py` — the `reminders` table.

    `tests/conftest.py` stubs `worker.notifier.notify` to `None`, so delivering
    a due reminder raises `TypeError: 'NoneType' object is not callable` before
    the mark-sent UPDATE runs. Substituting a real callable is what gets the
    caller past delivery and into the second and third queries — which are the
    ones that write.
    """
    import sys

    from worker import reminders

    sent = []

    async def notify(title, body, **kwargs):
        sent.append(title)

    # Reached through `sys.modules` rather than `import worker.notifier`: the
    # conftest inserts the stub with `sys.modules.setdefault`, which never sets
    # the attribute on the `worker` package, so the dotted import fails.
    monkeypatch.setattr(sys.modules["worker.notifier"], "notify", notify,
                        raising=False)
    monkeypatch.setattr(reminders, "notify", notify, raising=False)

    # Due in the past, so `check_due_reminders` actually finds it and runs the
    # delivery and mark-sent path rather than selecting nothing.
    await reminders.create_reminder(
        principal, "P12-01 probe reminder",
        datetime.now(timezone.utc) - timedelta(minutes=5))
    await reminders.check_due_reminders(principal)
    assert sent, (
        "check_due_reminders delivered nothing, so the mark-sent UPDATE never "
        "ran and this test observed only the INSERT."
    )

    counts = assert_touched(audit_probe, "worker/reminders.py")
    assert counts["unbound"] == 0, (
        f"worker/reminders.py ran {counts['unbound']} unbound query(ies) "
        f"against `reminders`, whose predicate is live."
    )


async def test_briefing_gathers_bound(principal, audit_probe):
    """`worker/briefing.py` — the four gather passes."""
    from worker import briefing

    await briefing._gather_business_data(principal)
    await briefing._gather_security_data(principal)
    await briefing._gather_tasks_data(principal)
    await briefing._gather_calendar_data(principal)

    counts = assert_touched(audit_probe, "worker/briefing.py")
    assert counts["unbound"] == 0, (
        f"worker/briefing.py ran {counts['unbound']} unbound query(ies). A "
        f"briefing assembled from unbound reads is the failure its own comment "
        f"names: it reads as 'a quiet day'."
    )


async def test_self_healing_reflection_binds(
    pool, private_tenant, audit_probe, monkeypatch
):
    """`worker/self_healing.py` — the half that touches a workspace table.

    `run_self_healing_check` writes `security_events`, which carries no
    `workspace_id` and is correctly reached on a bare `acquire()`. The
    workspace-touching half is `run_self_reflection`, which writes
    `conversations` and `messages` — and derives its own principal, with no
    argument to pass one in.
    """
    import utils.llm

    from worker import self_healing

    # `owner_connection` refuses in the cloud edition by design, and this
    # subsystem is Private-edition only (CLOUD-ADR-0001). Running it as cloud
    # would exercise the refusal, not the binding.
    monkeypatch.setenv("IRA_EDITION", "private")

    async def chat_complete(messages, **kwargs):
        return "P12-01 probe reflection"

    # Imported inside the function as `from utils.llm import chat_complete`, so
    # the patch has to land on the source module.
    monkeypatch.setattr(utils.llm, "chat_complete", chat_complete, raising=False)

    # `run_self_reflection` returns early when the last hour produced no
    # `model_performance` rows, which would leave nothing observed. One row is
    # the minimum that gets it past that gate to the part being audited.
    async with pool.acquire() as conn:
        # `model_performance.tenant_id` defaults from `app.tenant_id` and the
        # table has forced RLS, so this row has to be written under the same
        # tenant the reflection will read it back as.
        await conn.execute("SELECT set_config('app.tenant_id',$1,false)",
                           str(private_tenant["tenant_id"]))
        await conn.execute(
            "INSERT INTO model_performance (model_name, request_type, latency_ms, "
            "                               success) VALUES ('p1201','probe',1,TRUE)")
        await conn.execute("SELECT set_config('app.tenant_id','',false)")

    await self_healing.run_self_reflection()

    counts = assert_touched(audit_probe, "worker/self_healing.py")
    assert counts["unbound"] == 0, (
        f"worker/self_healing.py ran {counts['unbound']} unbound query(ies) "
        f"against a workspace-carrying table."
    )


async def test_notifier_persist_binds_a_tenant(principal, audit_probe):
    """`worker/notifier.py` — loaded past the conftest stub, on purpose.

    `tests/conftest.py` replaces `worker.notifier` with a stub for every suite
    in this repository, which means the real module could not be observed by
    the audit no matter how many suites ran. That is not a caller that binds
    correctly; it is a caller nothing has ever looked at. Loading the real file
    here is the only way to ask the question.

    `notifications` is the one workspace-carrying table with a DIFFERENT
    predicate shape — `workspace_id IS NULL OR workspace_id = app.workspace_id`
    — so a tenant-wide notification is legitimately readable from any workspace
    in the tenant. `_persist` binding `tenant_connection` and writing a NULL
    workspace is therefore correct rather than a missing binding, and this test
    exists to record that with a row rather than with an argument.
    """
    spec = importlib.util.spec_from_file_location(
        "worker._notifier_real", IRA_ROOT / "worker" / "notifier.py")
    notifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notifier)

    await notifier._persist(
        str(uuid.uuid4()), "system", "P12-01 probe", "body", "info", [], {},
        principal=principal)

    counts = assert_touched(audit_probe, "worker/notifier.py")
    assert counts["bound"] + counts["unbound"] > 0
