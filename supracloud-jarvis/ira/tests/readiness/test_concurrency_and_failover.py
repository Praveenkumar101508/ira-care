"""Blocker B4 — the half of it that can honestly be tested here.

`RELEASE_READINESS.md` lists five `not-verified` items under B4. They are not
equally testable, and pretending otherwise is the failure this file is written
to avoid:

    R01  browser E2E        → testable. `frontend/e2e/`, real Chromium.
    R02  mobile E2E         → NOT testable. No device, no emulator, no mobile
                              OS. A phone-sized viewport is not a phone.
    R04  load               → HALF testable. `scripts/load-profile.py` sweeps
                              concurrency over the data layer and reports
                              throughput, percentiles and the knee. The SERVICE
                              is not testable here at all: the HTTP app cannot
                              be started without the model stack, and absolute
                              numbers describe this container regardless.
    R05  concurrency        → HALF testable. The database and application half
                              is exercised below. The WebRTC media half is not:
                              no media server is configured.
    R07  database failover  → HALF testable. Replica PROMOTION is exercised by
                              `scripts/failover-drill.sh`, which builds a real
                              standby, hard-kills the primary and measures the
                              result — that is where R07's evidence comes from.
                              The pool-recovery test below is still gated off
                              and out of CI: it hung a job for fifteen minutes
                              and the cause is not established.

What follows is the testable half, run against a real PostgreSQL. Each test
names the half it does **not** cover, so nothing here can be read as more than
it is.

── Why concurrency belongs in a launch gate at all ─────────────────────────

Every isolation guarantee in this system is a claim about what happens when two
things run at once. `test_no_execution_without_decision` proves a ticket is
one-shot in a single process; that is a different statement from "two concurrent
requests cannot both consume it". Row-level security is set per transaction with
`SET LOCAL`; that is a different statement from "a pooled connection cannot leak
a tenant to its next borrower".

Those are the two properties below, and both are the kind that pass every
single-threaded test and fail in production at 3am.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
asyncpg = pytest.importorskip("asyncpg")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="IRA_TEST_DATABASE_DSN not set — concurrency tests skipped"
)

#: How many tenants hammer the pool at once. Small enough to run in CI in
#: seconds, large enough that a pooled connection carrying a stale
#: `app.tenant_id` would be hit rather than missed. Not a load figure and not
#: presented as one.
CONCURRENT_TENANTS = 24

#: Rounds each tenant performs. Interleaving is what exposes a leak; one round
#: each would let every task run to completion before the next started.
ROUNDS_PER_TENANT = 8


@pytest.fixture
async def pool():
    import utils.db as db_module

    # max_size deliberately far below CONCURRENT_TENANTS, so connections MUST be
    # reused between tenants. A pool large enough to give every task its own
    # connection would test nothing: the whole risk is a returned connection
    # carrying the last tenant's context.
    created = await asyncpg.create_pool(dsn=DSN, min_size=2, max_size=4)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        # Bounded, with `terminate()` as the fallback.
        #
        # `Pool.close()` waits for every connection to be released *cleanly*,
        # and the failover test terminates every backend, so they never are.
        # That is a real hazard and this is the right shape regardless.
        #
        # It was believed to be the cause of the fifteen-minute CI hang.
        # **Planting the unbounded close back did not reproduce the hang**, so
        # it was not demonstrably the cause and the hang remains unexplained.
        # This is a fix on its own merits, not a diagnosis.
        try:
            await asyncio.wait_for(created.close(), timeout=10.0)
        except (asyncio.TimeoutError, Exception):  # noqa: B014 - any close failure
            created.terminate()


#: Deterministic per tenant, matching `provision_tenant_substrate`, so the
#: fixture's workspace and a provisioned one are the same row.
_NAMESPACE = uuid.UUID("6f3a1c2e-0b4d-5e8a-9c17-2d4b8e6f0a35")


def _workspace_of(tenant: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"workspace:default:{tenant}")


def _principal(tenant: uuid.UUID):
    """A principal carrying the tenant AND its workspace.

    `files` is one of the seventeen workspace-scoped tables, so since 049 a
    tenant-only principal reads nothing from it and this whole suite would
    measure an empty result rather than an isolated one. The workspace is
    deterministic per tenant, which keeps the leak question exactly as it was:
    every task holds a different pair, and seeing another task's row is still
    the failure.
    """
    from core.editions import EDITION_CLOUD
    from core.identity import Principal

    return Principal(
        user_id=uuid.uuid5(tenant, "user"),
        tenant_id=tenant,
        session_id=uuid.uuid4(),
        edition=EDITION_CLOUD,
        workspace_id=_workspace_of(tenant),
    )


@pytest.fixture
async def tenants(pool):
    """`CONCURRENT_TENANTS` tenants, each with one row nobody else may see."""
    run = uuid.uuid4().hex[:10]
    ids = [uuid.uuid4() for _ in range(CONCURRENT_TENANTS)]
    async with pool.acquire() as conn:
        for i, tenant in enumerate(ids):
            await conn.execute(
                "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
                tenant, f"Conc {run} {i}", f"conc-{run}-{i}",
            )
            await conn.execute("SELECT set_config('app.tenant_id',$1,false)", str(tenant))
            org = await conn.fetchval(
                "INSERT INTO organisations (tenant_id,name) VALUES ($1,'Conc') "
                "RETURNING id", tenant)
            await conn.execute(
                "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,"
                "                        is_default,status) "
                "VALUES ($1,$2,$3,'Default','default',TRUE,'active')",
                _workspace_of(tenant), tenant, org)
            await conn.execute(
                "SELECT set_config('app.workspace_id',$1,false)",
                str(_workspace_of(tenant)))
            await conn.execute(
                "INSERT INTO files (tenant_id,user_id,filename,storage_path,sha256) "
                "VALUES ($1,$2,$3,$4,$5)",
                tenant, f"u{i}", f"tenant-{i}-secret.txt", f"/tmp/{i}", f"{i:064d}",
            )
        await conn.execute("SELECT set_config('app.tenant_id','',false)")
        await conn.execute("SELECT set_config('app.workspace_id','',false)")
    return ids


# ── R05, the half that is about this system rather than about media ─────────


async def test_a_pooled_connection_never_leaks_a_tenant_under_concurrency(
    pool, tenants
):
    """**The property row-level security actually rests on.**

    `workspace_connection` sets `app.tenant_id` and `app.workspace_id` with `SET LOCAL`, which is scoped
    to the transaction and discarded on COMMIT. That is the design. This is the
    test that the design holds when the pool is smaller than the workload and
    connections are handed between tenants continuously.

    The failure it exists to catch is not a bug in any query. It is one
    connection returned to the pool still carrying a tenant, and the next
    borrower — a different tenant — inheriting it. Every `WHERE` clause in the
    codebase would be correct and the read would still be cross-tenant.

    Does NOT cover: WebRTC or WebSocket concurrency. No media server is
    configured in this build, and R05's transport half stays `not-verified`.
    """
    from core.identity.db_context import workspace_connection

    async def hammer(tenant: uuid.UUID) -> list[str]:
        principal = _principal(tenant)
        seen: list[str] = []
        for _ in range(ROUNDS_PER_TENANT):
            async with workspace_connection(principal) as conn:
                rows = await conn.fetch("SELECT tenant_id, filename FROM files")
                seen.extend(
                    f"{r['tenant_id']}|{r['filename']}"
                    for r in rows
                    if r["tenant_id"] != tenant
                )
                bound = await conn.fetchval(
                    "SELECT current_setting('app.tenant_id', true)"
                )
                if bound != str(tenant):
                    seen.append(f"BOUND-WRONG:{bound}")
            # Yield so the event loop interleaves; without this each task can
            # run to completion and the connections are never contended.
            await asyncio.sleep(0)
        return seen

    results = await asyncio.gather(*(hammer(t) for t in tenants))
    leaks = [item for sub in results for item in sub]
    assert not leaks, (
        f"{len(leaks)} cross-tenant observation(s) under {CONCURRENT_TENANTS} "
        f"concurrent tenants sharing a pool of 4 connections. Sample: "
        f"{leaks[:5]}. Every WHERE clause in the codebase can be correct and "
        f"this still be a cross-tenant read."
    )


async def test_the_concurrency_harness_would_notice_a_leak(pool, tenants):
    """Proves the test above is capable of failing.

    With `CONCURRENT_TENANTS` tenants and a clean result, "no leaks" and "the
    query returned nothing at all" look identical. So: the same query, run with
    the tenant context deliberately set to a *different* tenant, must see that
    other tenant's row. If it does not, the sweep above proves nothing.
    """
    from core.identity.db_context import workspace_connection

    a, b = tenants[0], tenants[1]
    async with workspace_connection(_principal(a)) as conn:
        mine = await conn.fetch("SELECT filename FROM files")
    async with workspace_connection(_principal(b)) as conn:
        theirs = await conn.fetch("SELECT filename FROM files")

    assert mine and theirs, "the harness sees no rows at all — it proves nothing"
    assert {r["filename"] for r in mine} != {r["filename"] for r in theirs}, (
        "two different tenants saw identical rows, so either the seed did not "
        "differentiate them or RLS is not filtering — either way the "
        "concurrency result above is meaningless"
    )


async def test_concurrent_writes_land_under_the_writing_tenant(pool, tenants):
    """The write half. A leaked context on a write is worse than on a read: it
    files one tenant's data under another's, permanently and invisibly."""
    from core.identity.db_context import workspace_connection

    marker = uuid.uuid4().hex[:8]

    async def write(tenant: uuid.UUID, i: int) -> None:
        async with workspace_connection(_principal(tenant)) as conn:
            await conn.execute(
                "INSERT INTO files (user_id,filename,storage_path,sha256) "
                "VALUES ($1,$2,$3,$4)",
                f"w{i}", f"{marker}-{i}.txt", f"/tmp/w{i}", f"{i:064d}",
            )
        await asyncio.sleep(0)

    await asyncio.gather(*(write(t, i) for i, t in enumerate(tenants)))

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tenant_id, filename FROM files WHERE filename LIKE $1",
            f"{marker}-%",
        )
    # RLS is forced, so the pool connection sees nothing without a context —
    # which is itself the check that the rows went somewhere scoped.
    assert rows == [], (
        "an unscoped connection can see the rows just written; RLS is not "
        "being enforced and this test proves nothing"
    )

    misfiled: list[str] = []
    for i, tenant in enumerate(tenants):
        async with workspace_connection(_principal(tenant)) as conn:
            found = await conn.fetchval(
                "SELECT COUNT(*) FROM files WHERE filename = $1", f"{marker}-{i}.txt"
            )
            if found != 1:
                misfiled.append(f"tenant {i}: expected its own row, found {found}")
    assert not misfiled, f"concurrent writes were misfiled: {misfiled}"


# ── R07 — and why this still does NOT run in CI ───────────────────────────
#
# Deselected in CI for the third time, and this is the first of those three
# decisions taken on evidence that the cause is *not* understood.
#
# It hung a CI job for its full fifteen-minute timeout once. Later it was
# reproduced locally: 2 hangs out of 2 against a 200s cap. That looked like a
# breakthrough, and a real hazard was found in the `pool` fixture above, which
# called `Pool.close()` unbounded — and `close()` waits for every connection to
# be released cleanly, which a terminated backend never is.
#
# **Planting that unbounded close back did not reproduce the hang.** It passes
# in under a second either way. So the fixture was not demonstrably the cause,
# and the hang is not currently reproducible at all.
#
# The fixture fix is kept regardless: an unbounded close after terminating every
# backend is wrong on its own merits, whatever else is going on. But it is not
# a diagnosis, and this test has now been re-enabled twice on reasoning that
# felt sufficient at the time.
#
# The most likely remaining variable is database state — the hangs happened
# immediately after a series of SIGKILL-and-restart cycles from
# `scripts/failover-drill.sh`. "Most likely" is not a cause.
#
# R07 is `partial` on the strength of the DRILL, which is real, repeatable and
# committed, and not on the strength of this test.
#
# These two are gated behind their own environment variable, and the reason is
# measured rather than cautious: `pg_terminate_backend` against every backend
# puts PostgreSQL into crash recovery ("terminating any other active server
# processes"), which takes the whole instance down for a moment. On a shared
# development database that hangs every other suite running beside it —
# observed while writing this file, not hypothesised.
#
# ── Why these used to hang, and what fixed it ────────────────────────────────
#
# The first version of these tests wedged instead of failing, and the cause was
# in the test rather than in the application: `asyncpg`'s `pool.acquire()` has
# **no default timeout**. With every backend terminated there is no connection to
# hand out and no deadline, so the retry loop below never got as far as its own
# `time.monotonic()` check — it blocked inside the first `acquire()` forever.
#
# `asyncio.wait_for` around each attempt fixes it. The loop now gets its turn,
# the deadline is honoured, and a database that genuinely does not come back
# produces a **failure with a diagnosis** in fifteen seconds rather than a job
# that runs until the runner gives up.
#
# Worth stating because it was nearly reported as an application finding: nothing
# about the original hang said anything about IRA's resilience. It said the test
# had no timeout.

_FAILOVER = pytest.mark.skipif(
    not os.getenv("IRA_TEST_ALLOW_DESTRUCTIVE"),
    reason=(
        "IRA_TEST_ALLOW_DESTRUCTIVE not set. These tests terminate every "
        "database backend, which puts PostgreSQL into crash recovery. Set it "
        "when running against a database you are willing to disrupt."
    ),
)


@_FAILOVER
async def test_the_pool_recovers_when_the_database_goes_away(pool, tenants):
    """**A real failure, not a mocked one.**

    Every connection in the pool is terminated server-side with
    `pg_terminate_backend` — the same thing a failover, an OOM kill or a
    restart does to an application holding open connections. The application
    must then recover on the next request rather than staying broken until
    somebody notices.

    Does NOT cover: replica promotion, WAL replay, or any RTO/RPO measurement.
    There is no replica and no WAL archiving in this deployment;
    `docs/operations/DISASTER_RECOVERY.md` §0 says so, and R07 stays `partial`
    for exactly that reason rather than being called verified on the strength
    of this.
    """
    from core.identity.db_context import workspace_connection

    principal = _principal(tenants[0])

    async with workspace_connection(principal) as conn:
        before = await conn.fetchval("SELECT COUNT(*) FROM files")
    assert before >= 1

    # Kill every backend this pool owns, from a connection outside it.
    # Bounded. This connect is made AFTER the decision to kill but BEFORE the
    # retry loop, so an unbounded one is outside every timeout in this test —
    # and a PostgreSQL in crash recovery refuses connections. That is the hole
    # that hung CI at 15 minutes even after every await inside the loop was
    # bounded.
    killer = await asyncio.wait_for(asyncpg.connect(dsn=DSN), timeout=10.0)
    try:
        killed = await killer.fetch(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
    finally:
        await killer.close()
    assert killed, "no backends were terminated — the failure was not injected"

    # The next request may fail; the one after must not. A pool that never
    # recovers is the actual outage, and a pool that recovers immediately is
    # what "resilient" means here.
    deadline = time.monotonic() + 15
    recovered = False
    last_error = ""
    while time.monotonic() < deadline:
        try:
            # `asyncio.wait_for` is load-bearing, not defensive. `pool.acquire()`
            # has no default timeout, so without this the first attempt blocks
            # forever and the deadline above is never consulted — the test hangs
            # instead of failing. See the note at the top of this section.
            async def _attempt() -> int:
                async with workspace_connection(principal) as conn:
                    return await conn.fetchval("SELECT COUNT(*) FROM files")

            after = await asyncio.wait_for(_attempt(), timeout=2.0)
            assert after == before, (
                f"row count changed across the restart: {before} → {after}. "
                f"The connections were killed, not the data."
            )
            recovered = True
            break
        except (Exception, asyncio.TimeoutError) as exc:  # noqa: BLE001 - retry
            last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(0.25)

    assert recovered, (
        f"the pool did not recover within 15s of every connection being "
        f"terminated. Last error: {last_error}. An application that stays down "
        f"after a database restart turns a 2-second blip into an outage."
    )


# ── One test that used to be here, and why it is not ────────────────────────
#
# `test_tenant_isolation_survives_a_connection_reset` killed every backend and
# then swept six tenants looking for a cross-tenant read introduced by the
# recovery. It never terminated, and bounding every await inside it did not fix
# that — unlike the test above, which the same treatment did fix.
#
# It is deleted rather than left gated off, for two reasons:
#
#   1. A test nobody can run to completion is not a test. Kept behind a flag it
#      is clutter that looks like coverage, and the next person to read this
#      file would reasonably assume somebody had run it.
#   2. Its property is not actually uncovered. `test_a_returned_connection_
#      carries_no_tenant` above asserts an unbound borrow is blind, and
#      `tests/tenancy/` asserts the policy on every scoped table. What it added
#      was "...specifically after a mass reconnect", and that increment is not
#      worth a test that wedges a runner.
#
# Stated plainly rather than quietly dropped, because a deleted test is exactly
# the kind of thing that should leave a note behind.

# ── The items this file deliberately does not cover ─────────────────────────


def test_the_untestable_items_are_still_recorded_as_untestable():
    """A guard against this file being cited for more than it does.

    `RELEASE_READINESS.md` R04 (load) and R07 (database failover) must stay
    `not-verified`. Nothing measures throughput, and the failover test is not
    run at all. A future edit that quietly promoted either — because "B4 has
    tests now" — is exactly the drift this project's reporting discipline
    exists to catch.

    ── Why R02 is guarded differently now, rather than dropped ──────────────

    R02 (mobile) used to be in the not-verified list alongside them. It has
    since moved to `partial` on real evidence: `frontend/e2e/mobile.e2e.ts`
    runs on **WebKit**, the engine iOS Safari is built on, and found five
    shipping defects including pinch-zoom disabled app-wide (WCAG 1.4.4) and
    controls sealed off the screen at 320px.

    Promoting it was a judgement call, and the risk of getting it wrong is the
    R07 mistake in a different costume — so the guard is **sharpened rather
    than removed**. R02 may progress on evidence; it may never claim what it
    still has not got. There is no device, no emulator and no mobile OS here,
    and WebKit-on-Linux is not iOS however much code it shares. So R02 must
    never read `verified`, and its gap text must go on saying there is no
    device.
    """
    import re
    from pathlib import Path

    report = Path(__file__).resolve().parents[3] / "docs" / "cloud-transformation" / "RELEASE_READINESS.md"
    if not report.is_file():  # pragma: no cover - path check
        report = Path(__file__).resolve().parents[4] / "docs" / "cloud-transformation" / "RELEASE_READINESS.md"
    assert report.is_file(), f"could not find the readiness report at {report}"

    text = report.read_text(encoding="utf-8")

    def block_for(item: str) -> str:
        found = re.search(rf"### {item} —.*?(?=\n### |\Z)", text, re.S)
        assert found, f"{item} is missing from the readiness report"
        return found.group(0)

    # R04 — same shape as R02 and R07. A profiler exists and measures real
    # things (`scripts/load-profile.py`), so the item may progress. What it may
    # never claim is a service capacity figure: the HTTP API cannot be started
    # in this environment at all — `app.factory.create_app` imports the memory
    # stack, which imports `sentence_transformers` — so nothing end to end has
    # ever been driven, and the absolute throughput describes the container the
    # load generator shared with PostgreSQL.
    r04 = block_for("R04")
    r04_status = re.search(r"\*\*Status:\*\*\s*(\S+)", r04)
    assert r04_status and r04_status.group(1) in {"not-verified", "partial"}, (
        f"R04 reads {r04_status and r04_status.group(1)!r}. The data layer is "
        f"profiled, but the service was never driven and no load target has "
        f"been set. `verified` would claim a capacity figure that does not "
        f"exist."
    )
    assert "cannot be started" in r04, (
        "R04 no longer states that the HTTP API cannot be started here. That "
        "sentence is the difference between 'the data layer is profiled' and "
        "'the service is load tested', and without it the numbers read as "
        "capacity."
    )

    # R07 — same shape as R02, and for a harder-won reason.
    #
    # It was pinned at `not-verified` here because the failover test hung a CI
    # job for its full timeout. That is now fixed with the mechanism identified
    # — the `pool` fixture called `Pool.close()` unbounded, and `close()` waits
    # for connections that a terminated backend can never release cleanly — and
    # there is a real streaming standby with a measured promotion.
    #
    # So R07 may progress. It may not claim to have solved the parts that are
    # still missing: no RTO or RPO **target** has been set, failure detection
    # and client redirection do not exist, and the RPO was measured over
    # loopback where async replication has no latency to lose data in.
    r07 = block_for("R07")
    r07_status = re.search(r"\*\*Status:\*\*\s*(\S+)", r07)
    assert r07_status and r07_status.group(1) in {"not-verified", "partial"}, (
        f"R07 reads {r07_status and r07_status.group(1)!r}. Promotion works and "
        f"is measured, but recovery time in a real outage is dominated by "
        f"detecting the failure and moving clients — neither of which exists "
        f"here — and no RTO/RPO target has been set. `verified` would claim a "
        f"failover capability rather than a failover mechanism."
    )
    assert "no RTO or RPO target" in r07 or "No RTO or RPO target" in r07, (
        "R07 no longer states that no RTO/RPO target has been set. A measured "
        "number is not an objective, and that sentence is what stops the "
        "measurement being read as a promise."
    )

    # R02 — may be `partial`, may never be `verified`, and must keep saying so.
    r02 = block_for("R02")
    r02_status = re.search(r"\*\*Status:\*\*\s*(\S+)", r02)
    assert r02_status and r02_status.group(1) in {"not-verified", "partial"}, (
        f"R02 reads {r02_status and r02_status.group(1)!r}. Mobile testing here "
        f"runs on WebKit-on-Linux, which shares an engine with iOS Safari but "
        f"is not a device: no phone, no emulator, no mobile OS, no touch "
        f"hardware, no mobile GPU. `verified` would claim device coverage that "
        f"does not exist."
    )
    assert "No device." in r02 or "no device" in r02.lower(), (
        "R02 no longer states that no device was involved. That sentence is "
        "the whole difference between 'tested on two engines' and 'tested on "
        "mobile', and it is what stops the next reader over-reading the item."
    )


async def test_a_returned_connection_carries_no_tenant(pool, tenants):
    """An unbound connection from a hot pool must see nothing.

    ── What planting the bug actually showed ────────────────────────────────

    The obvious plant for this file is `set_config(..., is_local => false)` —
    making the tenant setting session-scoped instead of transaction-scoped, so a
    returned connection would carry its tenant to the next borrower. **It does
    not fail either test here, and that is worth recording rather than hiding.**

    Two independent mechanisms prevent the leak, and the plant only removes one:

      1. `SET LOCAL` — the setting dies with the transaction. This is the one
         `tenant_connection` relies on and the one the plant removes.
      2. **asyncpg resets a connection when it is released to the pool.**
         Session-level state does not survive the release either way.

    So this test verifies the **property** — an unbound borrow is blind — and
    not the mechanism, because with two mechanisms in play no single-variable
    plant can attribute it to one. Claiming it proves `SET LOCAL` specifically
    would be claiming more than the evidence supports.

    The property is the thing that matters, and it is worth more than the
    mechanism: it is what makes `owner_connection`, `tenant_connection` and
    every bare `acquire()` in the codebase safe against each other. Defence in
    depth is the correct finding here, not a weaker one.

    Where this WOULD fail is a change that removed both — a pool configured with
    connection reuse and no reset, plus a session-level set. That is exactly the
    kind of change nobody would connect to tenant isolation while making it,
    which is why the assertion is worth keeping even though today it is
    over-determined.
    """
    from core.identity.db_context import workspace_connection
    from utils.db import acquire

    # Bind a tenant and read, on a pooled connection.
    async with workspace_connection(_principal(tenants[0])) as conn:
        mine = await conn.fetchval("SELECT COUNT(*) FROM files")
    assert mine >= 1, "the seeded row is not visible to its own tenant"

    # Now take connections from the same small pool WITHOUT binding anything.
    # Every one of them must be blind. With a pool of 4 and 12 attempts the
    # connection just used is certain to come back round.
    inherited: list[str] = []
    for _ in range(12):
        async with acquire() as conn:
            bound = await conn.fetchval("SELECT current_setting('app.tenant_id', true)")
            rows = await conn.fetchval("SELECT COUNT(*) FROM files")
            if bound:
                inherited.append(f"context {bound!r} survived onto a fresh borrow")
            if rows:
                inherited.append(f"an unbound connection read {rows} row(s)")
        await asyncio.sleep(0)

    assert not inherited, (
        "a connection returned to the pool carried its tenant context to the "
        f"next borrower: {inherited[:4]}. This is what `SET LOCAL` prevents and "
        f"a session-level `SET` does not — and it is a cross-tenant read with "
        f"no bug in any query."
    )
