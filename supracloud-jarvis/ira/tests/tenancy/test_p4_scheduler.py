"""Phase 4 batch 11 — retention runs without being asked.

P4-06 made retention correct and gave it an HTTP caller. A user's retention
window still only applied when somebody pressed a button, which is a retention
feature rather than a retention policy. This is the scheduler.

The tests are grouped by the property they defend, because the properties are
what make a background deleter safe to leave running:

  * it actually runs, and actually deletes;
  * exactly one instance at a time, fleet-wide;
  * a tenant that fails does not stop the others;
  * a crash leaves a record, and restarting is safe;
  * it does not run more often than its interval, however often it is started.

The last one matters more than it looks. A crash loop restarting every thirty
seconds would run retention every thirty seconds if the interval were a process
timer, and the first symptom would be a lot of data disappearing quickly.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

for _k, _v in {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-test",
    "REDIS_PASSWORD": "ci-test",
    "VLLM_API_KEY": "ci-test",
}.items():
    os.environ.setdefault(_k, _v)

TENANT = uuid.uuid4()
OTHER_TENANT = uuid.uuid4()
USER = uuid.uuid4()
OTHER_USER = uuid.uuid4()
WORKSPACE = uuid.uuid4()
OTHER_WORKSPACE = uuid.uuid4()


@pytest.fixture
async def pool():
    """**The APPLICATION role, not a superuser.**

    The first version of this fixture used `ADMIN_DSN`. A superuser bypasses RLS
    entirely, so the scheduler's enumeration saw every tenant's
    `retention_settings` regardless of what was bound — which hid two defects at
    once: the module was using `owner_connection` (forbidden for cloud-scoped
    data, and bound to the PRIVATE tenant), and an unbound query against a
    FORCE-RLS table would have matched zero rows in production.

    Both were invisible under a superuser and obvious under `ira_app`.
    """
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=6)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


@pytest.fixture
async def seeded(pool):
    """Two tenants, each with a user who has a retention window and old data."""
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await _wipe(admin)
        for tenant, user, workspace, slug in (
            (TENANT, USER, WORKSPACE, "sched-a"),
            (OTHER_TENANT, OTHER_USER, OTHER_WORKSPACE, "sched-b"),
        ):
            await admin.execute(
                "INSERT INTO tenants (id,name,slug,edition) "
                "VALUES ($1,$2,$3,'cloud')", tenant, slug, slug)
            await admin.execute(
                "INSERT INTO users (id,display_name,auth_provider,status) "
                "VALUES ($1,'S','password','active')", user)
            await admin.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
                "VALUES ($1,$2,'member','active')", tenant, user)
            org = await admin.fetchval(
                "INSERT INTO organisations (tenant_id,name,slug) "
                "VALUES ($1,'O',$2) RETURNING id", tenant, slug)
            await admin.execute(
                "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,"
                " is_default) VALUES ($1,$2,$3,'W',$4,TRUE)",
                workspace, tenant, org, slug)
            await admin.execute(
                "INSERT INTO workspace_memberships "
                " (tenant_id,workspace_id,user_id,role) "
                "VALUES ($1,$2,$3,'member')", tenant, workspace, user)
            await admin.execute(
                "INSERT INTO retention_settings (tenant_id,user_id,"
                " conversation_retention_days) VALUES ($1,$2,30)", tenant, user)
        await admin.execute("DELETE FROM job_runs WHERE job = 'retention'")
        yield admin
    finally:
        await admin.execute("DELETE FROM job_runs WHERE job = 'retention'")
        await _wipe(admin)
        await admin.close()


async def _wipe(admin):
    for t in (TENANT, OTHER_TENANT):
        await admin.execute("DELETE FROM tenants WHERE id = $1", t)
    for u in (USER, OTHER_USER):
        await admin.execute("DELETE FROM users WHERE id = $1", u)


async def _old_conversation(admin, *, tenant, workspace, days=400):
    # Owned by the tenant's user, whose window the pass applies (093, ADR-0009):
    # a pass reaches only that user's own conversations.
    owner = USER if tenant == TENANT else OTHER_USER
    cid = await admin.fetchval(
        "INSERT INTO conversations (tenant_id,workspace_id,owner_user_id,session_id,created_at) "
        "VALUES ($1,$2,$3,'sched', NOW() - ($4 || ' days')::interval) RETURNING id",
        tenant, workspace, owner, str(days))
    await admin.execute(
        "INSERT INTO messages (conversation_id,tenant_id,workspace_id,owner_user_id,role,"
        " content,created_at) VALUES ($1,$2,$3,$4,'user','x',"
        "  NOW() - ($5 || ' days')::interval)",
        cid, tenant, workspace, owner, str(days))
    return cid


# ─────────────────────────────────────────────────────────────────────────────
# It runs, and it deletes
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_pass_removes_data_past_its_window(seeded):
    """**The whole point.** No HTTP request, no user action — the scheduler
    enumerates, binds each principal, and expires."""
    from core.jobs.scheduler import run_retention_once

    old = await _old_conversation(seeded, tenant=TENANT, workspace=WORKSPACE)
    fresh = await _old_conversation(seeded, tenant=TENANT, workspace=WORKSPACE,
                                    days=2)

    outcome = await run_retention_once(force=True)
    assert outcome.status == "succeeded", outcome.failures
    assert outcome.items_removed >= 2, "nothing was removed"

    assert await seeded.fetchval(
        "SELECT count(*) FROM conversations WHERE id=$1", old) == 0
    assert await seeded.fetchval(
        "SELECT count(*) FROM conversations WHERE id=$1", fresh) == 1, (
        "a conversation inside its window was deleted")


async def test_every_tenant_with_a_window_is_reached(seeded):
    """Not just the first. A scheduler that enumerated and then broke after one
    tenant would look identical in the logs of the tenant it did reach."""
    from core.jobs.scheduler import run_retention_once

    mine = await _old_conversation(seeded, tenant=TENANT, workspace=WORKSPACE)
    theirs = await _old_conversation(seeded, tenant=OTHER_TENANT,
                                     workspace=OTHER_WORKSPACE)

    outcome = await run_retention_once(force=True)
    assert outcome.tenants_seen >= 2

    for cid in (mine, theirs):
        assert await seeded.fetchval(
            "SELECT count(*) FROM conversations WHERE id=$1", cid) == 0


async def test_a_user_without_a_window_is_not_enumerated(seeded):
    """Enumerating everybody makes each pass proportional to the user table
    rather than to the work, and a user who never opened the Privacy Centre has
    nothing to expire."""
    from core.jobs.scheduler import run_retention_once

    await seeded.execute(
        "UPDATE retention_settings SET conversation_retention_days = NULL, "
        " memory_retention_days = NULL WHERE tenant_id = $1", OTHER_TENANT)
    theirs = await _old_conversation(seeded, tenant=OTHER_TENANT,
                                     workspace=OTHER_WORKSPACE)

    outcome = await run_retention_once(force=True)
    assert await seeded.fetchval(
        "SELECT count(*) FROM conversations WHERE id=$1", theirs) == 1, (
        "a tenant with no retention window had data expired")
    assert outcome.tenants_seen == 1


# ─────────────────────────────────────────────────────────────────────────────
# One at a time, fleet-wide
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_second_instance_skips_while_the_first_holds_the_lock(seeded):
    """**The property that makes running it on every instance safe.**

    Two passes at once would not corrupt anything — the work is idempotent — but
    it doubles the load and makes the run history a lie: two rows, each claiming
    to have done the pass.

    The lock is taken on a REAL second connection rather than by monkeypatching,
    because `pg_try_advisory_lock` is session-scoped and a fake would not
    exercise the thing being relied on.
    """
    from core.jobs.scheduler import RETENTION_JOB, run_retention_once

    holder = await asyncpg.connect(ADMIN_DSN)
    try:
        got = await holder.fetchval(
            "SELECT pg_try_advisory_lock(hashtext($1))", RETENTION_JOB)
        assert got is True, "the test could not take the lock it is testing"

        outcome = await run_retention_once(force=True)
        assert outcome.status == "skipped"
        assert outcome.run_id is None, (
            "a skipped pass wrote a job_runs row, so the history now claims a "
            "run that never happened")
    finally:
        await holder.execute(
            "SELECT pg_advisory_unlock(hashtext($1))", RETENTION_JOB)
        await holder.close()


async def test_the_lock_is_released_after_a_pass(seeded):
    """A scheduler that leaked the lock would run exactly once per process
    lifetime and report `skipped` forever after — with nothing failing."""
    from core.jobs.scheduler import RETENTION_JOB, run_retention_once

    await run_retention_once(force=True)

    other = await asyncpg.connect(ADMIN_DSN)
    try:
        free = await other.fetchval(
            "SELECT pg_try_advisory_lock(hashtext($1))", RETENTION_JOB)
        assert free is True, "the retention lock was not released"
        await other.execute(
            "SELECT pg_advisory_unlock(hashtext($1))", RETENTION_JOB)
    finally:
        await other.close()


# ─────────────────────────────────────────────────────────────────────────────
# A failure is bounded and recorded
# ─────────────────────────────────────────────────────────────────────────────


async def test_one_tenants_failure_does_not_stop_the_others(seeded, monkeypatch):
    """One bad tenant stopping the fleet's retention indefinitely is how a
    compliance obligation quietly stops being met."""
    from core.jobs import scheduler

    theirs = await _old_conversation(seeded, tenant=OTHER_TENANT,
                                     workspace=OTHER_WORKSPACE)
    real = scheduler.enforce_for if hasattr(scheduler, "enforce_for") else None

    from core.privacy import retention as retention_module
    original = retention_module.enforce_for
    calls = {"n": 0}

    async def _explode_for_the_first(principal, **kw):
        calls["n"] += 1
        if principal.tenant_id == TENANT:
            raise RuntimeError("simulated tenant failure")
        return await original(principal, **kw)

    monkeypatch.setattr(retention_module, "enforce_for", _explode_for_the_first)

    outcome = await scheduler.run_retention_once(force=True)
    assert outcome.status == "failed"
    assert any(str(TENANT) in k for k in outcome.failures)
    assert calls["n"] >= 2, "the loop stopped at the first failure"

    assert await seeded.fetchval(
        "SELECT count(*) FROM conversations WHERE id=$1", theirs) == 0, (
        "the healthy tenant's retention did not run because another failed")
    assert real is None or True


async def test_a_failure_records_the_type_and_not_the_message(seeded, monkeypatch):
    """`job_runs` is read by operators who are not that tenant. An exception
    message can carry a row identifier or a fragment of content."""
    from core.jobs import scheduler
    from core.privacy import retention as retention_module

    async def _explode(principal, **kw):
        raise RuntimeError("conversation 1234-secret-content failed")

    monkeypatch.setattr(retention_module, "enforce_for", _explode)
    outcome = await scheduler.run_retention_once(force=True)

    assert set(outcome.failures.values()) == {"RuntimeError"}
    detail = await seeded.fetchval(
        "SELECT detail::text FROM job_runs WHERE id=$1", outcome.run_id)
    assert "secret-content" not in detail, "an exception message reached job_runs"


# ─────────────────────────────────────────────────────────────────────────────
# Observability and restart
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_pass_is_recorded_before_and_after(seeded):
    """A row written only at the END cannot describe a pass that died."""
    from core.jobs.scheduler import run_retention_once

    outcome = await run_retention_once(force=True)
    row = await seeded.fetchrow(
        "SELECT status, runner, started_at, finished_at, tenants_seen "
        "  FROM job_runs WHERE id=$1", outcome.run_id)
    assert row["status"] == "succeeded"
    assert row["finished_at"] is not None
    assert row["runner"], "the run does not say which process claimed it"
    assert row["tenants_seen"] >= 2


async def test_an_interrupted_run_is_closed_out_at_startup(seeded):
    """**Restart recovery.**

    A `running` row after a restart means the process holding it is gone — the
    advisory lock died with its connection, so nothing is locked, but the row
    would sit there forever claiming a pass was in progress.
    """
    from core.jobs.scheduler import recover_interrupted_runs

    stuck = await seeded.fetchval(
        "INSERT INTO job_runs (job, runner, status, started_at) "
        "VALUES ('retention','dead/1','running', NOW() - INTERVAL '9 hours') "
        "RETURNING id")

    closed = await recover_interrupted_runs()
    assert closed >= 1

    row = await seeded.fetchrow(
        "SELECT status, finished_at FROM job_runs WHERE id=$1", stuck)
    assert row["status"] == "interrupted"
    assert row["finished_at"] is not None


async def test_a_recent_running_row_is_left_alone(seeded):
    """The other half. Declaring a slow pass dead would let a second instance
    start while the first is still deleting."""
    from core.jobs.scheduler import recover_interrupted_runs

    live = await seeded.fetchval(
        "INSERT INTO job_runs (job, runner, status) "
        "VALUES ('retention','alive/1','running') RETURNING id")

    await recover_interrupted_runs()
    assert await seeded.fetchval(
        "SELECT status FROM job_runs WHERE id=$1", live) == "running", (
        "a pass that started moments ago was declared interrupted")


async def test_an_error_mid_pass_does_not_leave_the_row_running(seeded,
                                                                monkeypatch):
    """Recorded as failed there and then, rather than swept up an hour later by
    the stale check — which would make every crash look like a hang."""
    from core.jobs import scheduler

    async def _explode(tenant_id):
        raise RuntimeError("enumeration failed")

    monkeypatch.setattr(scheduler, "_principals_for", _explode)

    with pytest.raises(RuntimeError):
        await scheduler.run_retention_once(force=True)

    unfinished = await seeded.fetchval(
        "SELECT count(*) FROM job_runs "
        " WHERE job='retention' AND finished_at IS NULL")
    assert unfinished == 0, "a pass that raised left its row marked running"


# ─────────────────────────────────────────────────────────────────────────────
# The interval is fleet-wide, not per process
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_second_pass_is_not_due_immediately(seeded):
    """**Why the interval is read from the table.**

    A process timer would let a crash loop restarting every thirty seconds run
    retention every thirty seconds, and the first symptom would be a lot of data
    disappearing quickly.
    """
    from core.jobs.scheduler import run_retention_once

    first = await run_retention_once(force=True)
    assert first.status == "succeeded"

    second = await run_retention_once()  # not forced
    assert second.status == "skipped"
    assert second.run_id is None


async def test_force_overrides_the_interval_but_not_the_lock(seeded):
    """Operator tooling needs to run a pass now. Nothing needs two at once."""
    from core.jobs.scheduler import RETENTION_JOB, run_retention_once

    await run_retention_once(force=True)

    holder = await asyncpg.connect(ADMIN_DSN)
    try:
        await holder.fetchval(
            "SELECT pg_try_advisory_lock(hashtext($1))", RETENTION_JOB)
        outcome = await run_retention_once(force=True)
        assert outcome.status == "skipped", (
            "force bypassed the lock, so two passes can run at once")
    finally:
        await holder.execute(
            "SELECT pg_advisory_unlock(hashtext($1))", RETENTION_JOB)
        await holder.close()


# ─────────────────────────────────────────────────────────────────────────────
# It is wired, and it is off by default
# ─────────────────────────────────────────────────────────────────────────────


def test_the_scheduler_is_off_unless_switched_on(monkeypatch):
    """A background deleter that started by default would begin removing data
    the first time somebody ran the application locally against a copy of
    production — which is a thing people do."""
    from core.jobs import scheduler

    monkeypatch.delenv(scheduler.ENABLED_ENV, raising=False)
    assert scheduler.enabled() is False

    monkeypatch.setenv(scheduler.ENABLED_ENV, "true")
    assert scheduler.enabled() is True


def test_a_dedicated_worker_can_take_the_loop_off_the_api(monkeypatch):
    """P4-12 found this by running the topology it claims to run.

    With a dedicated scheduler process AND the API's lifespan both looping, a
    `job_runs` row came back stamped with the uvicorn pid. Nothing was corrupted
    — the advisory lock did its job — but the evidence no longer showed that the
    worker did the work, which is the whole point of running one.

    Default true so that a deployment WITHOUT a worker keeps its scheduler: the
    API's lifespan is currently the only thing that starts the loop, and a
    default of false would have silently stopped retention everywhere.
    """
    from core.jobs import scheduler

    monkeypatch.setenv(scheduler.ENABLED_ENV, "true")

    monkeypatch.delenv(scheduler.IN_API_ENV, raising=False)
    assert scheduler.enabled_in_api() is True, (
        "unset must keep today's behaviour; a false default would turn "
        "retention off for every deployment that has no worker")

    monkeypatch.setenv(scheduler.IN_API_ENV, "false")
    assert scheduler.enabled_in_api() is False

    monkeypatch.setenv(scheduler.IN_API_ENV, "true")
    assert scheduler.enabled_in_api() is True

    # The master switch still wins. Otherwise `IN_API=true` would resurrect a
    # scheduler the operator switched off.
    monkeypatch.delenv(scheduler.ENABLED_ENV, raising=False)
    assert scheduler.enabled_in_api() is False


def test_a_dedicated_worker_still_runs_the_loop_itself(monkeypatch):
    """`IN_API=false` must not reach the worker. It says where the loop runs,
    not whether it runs — a worker that consulted it would start, read its own
    configuration, and idle forever while reporting itself healthy."""
    import pathlib

    from core.jobs import scheduler

    monkeypatch.setenv(scheduler.ENABLED_ENV, "true")
    monkeypatch.setenv(scheduler.IN_API_ENV, "false")
    assert scheduler.enabled() is True

    worker = (pathlib.Path(__file__).resolve().parents[4]
              / "scripts" / "staging" / "run_scheduler.py")
    assert worker.is_file(), f"the staging worker has moved: {worker}"
    source = worker.read_text(encoding="utf-8")
    assert "scheduler.enabled()" in source, (
        "the worker must gate on the master switch")
    assert "enabled_in_api" not in source, (
        "the worker consults IRA_SCHEDULER_IN_API, so setting it false — the "
        "very setting that exists to hand the loop TO the worker — would stop "
        "the worker from looping")


def test_the_application_lifespan_starts_it():
    """**The "correct mechanism with no callers" check.**

    A scheduler nothing starts is a module. P4-06 shipped retention whose one
    consumer had no callers; this batch must not repeat it one level up.
    """
    import pathlib

    from app import lifespan as lifespan_module

    source = pathlib.Path(lifespan_module.__file__).read_text(encoding="utf-8")
    assert "scheduler" in source, (
        "app/lifespan.py never references the scheduler, so nothing starts it")
    assert "run_forever" in source, (
        "the scheduler is imported but its loop is never started")
    assert "enabled_in_api" in source, (
        "the lifespan gates on enabled() rather than enabled_in_api(), so a "
        "deployment with a dedicated worker runs the loop twice")


async def test_the_loop_stops_promptly_when_asked(seeded):
    """Shutdown must not hang for a tick interval. The wait is on an Event
    rather than a bare sleep for exactly this reason."""
    from core.jobs import scheduler

    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run_forever(stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)
    assert task.done()


#: A pass this long would outlast the 5 s allowance above many times over.
_STEPS, _STEP_SECONDS = 40, 0.25


async def _stop_mid_pass(scheduler) -> float:
    """Run the loop, ask it to stop mid-pass, and return how long it took to stop."""
    import time

    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run_forever(stop))
    await asyncio.sleep(0.6)
    stop.set()
    began = time.monotonic()
    await asyncio.wait_for(task, timeout=5.0)
    return time.monotonic() - began


async def _last_retention_row(admin):
    return await admin.fetchrow(
        "SELECT status, finished_at FROM job_runs WHERE job = 'retention' "
        " ORDER BY started_at DESC LIMIT 1")


async def _lock_is_free() -> bool:
    from core.jobs.scheduler import RETENTION_JOB

    other = await asyncpg.connect(ADMIN_DSN)
    try:
        free = await other.fetchval(
            "SELECT pg_try_advisory_lock(hashtext($1))", RETENTION_JOB)
        if free:
            await other.execute("SELECT pg_advisory_unlock(hashtext($1))", RETENTION_JOB)
        return bool(free)
    finally:
        await other.close()


async def test_a_long_pass_stops_at_the_next_principal_when_asked(seeded, monkeypatch):
    """**Shutdown waited for a whole pass.** A pass already running was allowed to
    finish, so the test above passed only while a pass was short: on a test
    database left with 2,048 tenants it took 3 s idle and over 5 s under load.

    Now the pass stops at the next principal. The one in hand finishes (no
    mid-delete kill), the row is closed as `interrupted` rather than left
    `running`, and the lock is released. Forty principals at 0.25 s each would
    take 10 s."""
    from types import SimpleNamespace

    from core.jobs import scheduler

    calls: list = []

    async def _one_tenant(conn):
        return [TENANT]

    async def _many_principals(tenant_id):
        return [(USER, WORKSPACE)] * _STEPS

    async def _slow_enforce(principal, **kw):
        calls.append(principal)
        await asyncio.sleep(_STEP_SECONDS)
        return SimpleNamespace(conversations_deleted=0, messages_deleted=0,
                               memories_deleted=0, memories_expired=0,
                               workspaces_not_covered=0)

    monkeypatch.setattr(scheduler, "_tenant_ids", _one_tenant)
    monkeypatch.setattr(scheduler, "_principals_for", _many_principals)
    monkeypatch.setattr("core.privacy.retention.enforce_for", _slow_enforce)

    waited = await _stop_mid_pass(scheduler)

    assert 0 < len(calls) < _STEPS, f"{len(calls)} of {_STEPS} principals ran"
    assert waited < 2.0, f"stopping took {waited:.2f}s"
    row = await _last_retention_row(seeded)
    assert row["status"] == "interrupted" and row["finished_at"] is not None, dict(row)
    assert await _lock_is_free(), "the retention lock was not released"
    # Not counted as a completed pass: the next one is due at once.
    assert await scheduler._due(seeded, scheduler.RETENTION_JOB, 3600.0)


async def test_enumerating_tenants_stops_too_when_asked(seeded, monkeypatch):
    """Enumeration is one query per tenant, so on a crowded database it is the
    slow half of a pass. It stops at the next tenant."""
    from core.jobs import scheduler

    seen: list = []

    async def _many_tenants(conn):
        return [uuid.uuid4() for _ in range(_STEPS)]

    async def _slow_principals(tenant_id):
        seen.append(tenant_id)
        await asyncio.sleep(_STEP_SECONDS)
        return []

    monkeypatch.setattr(scheduler, "_tenant_ids", _many_tenants)
    monkeypatch.setattr(scheduler, "_principals_for", _slow_principals)

    waited = await _stop_mid_pass(scheduler)

    assert 0 < len(seen) < _STEPS, f"{len(seen)} of {_STEPS} tenants enumerated"
    assert waited < 2.0, f"stopping took {waited:.2f}s"
    row = await _last_retention_row(seeded)
    assert row["status"] == "interrupted" and row["finished_at"] is not None, dict(row)
    assert await _lock_is_free(), "the retention lock was not released"


async def test_a_run_records_whether_it_was_forced(seeded):
    """**A record that cannot answer the question it exists for.**

    `force=True` deliberately skips the interval — the Privacy Centre's "run a
    pass now" uses it, and so does this suite. So two passes 0.1s apart with a
    24-hour floor is either a broken schedule or somebody pressing a button, and
    those need opposite responses.

    `job_runs` could not tell them apart. P4-12's own acceptance check measured
    the minimum gap between passes and reported a floor violation that was
    really two forced test runs — a false alarm that would have been
    indistinguishable from a real one in production.
    """
    from core.jobs import scheduler

    forced = await scheduler.run_retention_once(force=True)
    if forced.status == "skipped":
        pytest.skip("another instance holds the retention lock")

    row = await seeded.fetchrow(
        "SELECT detail, finished_at FROM job_runs WHERE id = $1", forced.run_id)
    assert row is not None, "the forced run wrote no row"
    detail = json.loads(row["detail"]) if isinstance(row["detail"], str) \
        else dict(row["detail"] or {})
    assert detail.get("forced") is True, (
        f"a forced pass is recorded as {detail.get('forced')!r}; the history "
        f"cannot distinguish it from a scheduled one")
    assert row["finished_at"] is not None, "the run was never closed out"


async def test_the_flag_survives_the_row_being_finished(seeded):
    """`_finish` replaces `detail` wholesale. A flag set only at INSERT time
    would be present on every running row and absent on every finished one —
    which is worse than not having it, because the absence reads as false."""
    from core.jobs import scheduler

    outcome = await scheduler.run_retention_once(force=True)
    if outcome.status == "skipped":
        pytest.skip("another instance holds the retention lock")

    raw = await seeded.fetchval(
        "SELECT detail->>'forced' FROM job_runs "
        " WHERE id = $1 AND finished_at IS NOT NULL", outcome.run_id)
    assert raw == "true", (
        f"after the row was finished, forced reads {raw!r} — _finish dropped it")
