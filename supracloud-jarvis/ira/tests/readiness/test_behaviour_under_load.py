"""Blocker B4 / readiness R04 — behaviour under load, which is not capacity.

── Read this before citing anything in here ──────────────────────────────────

**This file does not measure capacity, and nothing in it is a performance
number.** There is no representative deployment: this runs in a shared,
CPU-throttled container against a PostgreSQL on the same box. Any
requests-per-second figure taken from here would describe the container.

R04 asks for load tests. "Load testing" is really two questions, and only one
of them is answerable here:

  1. **How much can it take?**  Capacity, throughput, latency percentiles,
     saturation points. Needs a representative deployment. **Not answerable
     here, and this file does not pretend otherwise.** R04 stays `not-verified`
     on that basis.

  2. **What does it do when many things happen at once?**  Whether a ceiling is
     still a ceiling under concurrency, whether a saturated pool sheds load or
     hangs, whether connections come back. These are *properties*, not numbers.
     A property that holds here holds on bigger hardware — bigger hardware only
     changes when you reach the condition, not what happens when you do.

This file is the second question. Every count below is chosen to make a race
*reproducible*, not to describe throughput, and each is small enough to run in
CI in seconds.

── Bounded, deliberately ─────────────────────────────────────────────────────

Every await that could block is wrapped in `asyncio.wait_for`. That is not
defensive style; it is a direct consequence of R07, where an unbounded connect
in a test hung a CI job for its full fifteen-minute timeout. A load test is the
most likely place in this repository to hang, so nothing here is allowed to
wait forever.
"""

import asyncio
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="IRA_TEST_DATABASE_DSN not set — load-behaviour tests skipped"
)

#: Requests fired simultaneously at a quota. Comfortably more than the limit
#: below, so a check-then-act race has room to show itself. Not a load figure.
CONCURRENT_REQUESTS = 24

#: The ceiling under test. Small so the overshoot is unambiguous: with a limit
#: of 5 and 24 concurrent requests, a racing implementation admits far more
#: than 5 and an atomic one admits exactly 5.
DAILY_LIMIT = 5

#: Seconds any single bounded operation may take before the test fails rather
#: than hangs.
OP_TIMEOUT = 20.0


@pytest.fixture
async def pool():
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=2, max_size=8)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await asyncio.wait_for(created.close(), timeout=OP_TIMEOUT)


def _principal(tenant: uuid.UUID):
    from core.editions import EDITION_CLOUD
    from core.identity import Principal

    return Principal(
        user_id=uuid.uuid5(tenant, "user"),
        tenant_id=tenant,
        session_id=uuid.uuid4(),
        edition=EDITION_CLOUD,
    )


@pytest.fixture
async def metered_tenant(pool):
    """One tenant with a hard daily request ceiling and no usage yet.

    The `users` row is not incidental. `model_usage.user_id` has a foreign key,
    and `record_usage` is **fail-soft** by design — it logs and returns None
    rather than raising, because a lost metering row should not fail a request
    the user already paid for. So without this row every write is silently
    dropped, usage never accumulates, and a quota test would "prove" a race
    that it had actually just disabled the accounting for. That happened on the
    first run of this file.
    """
    tenant = uuid.uuid4()
    run = uuid.uuid4().hex[:10]
    principal = _principal(tenant)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
            tenant, f"Load {run}", f"load-{run}",
        )
        await conn.execute(
            "INSERT INTO users (id, display_name) VALUES ($1,$2)",
            principal.user_id, f"load-{run}",
        )
        await conn.execute("SELECT set_config('app.tenant_id',$1,false)", str(tenant))
        await conn.execute(
            "INSERT INTO usage_quotas (tenant_id, daily_request_limit, on_exceeded) "
            "VALUES ($1,$2,'refuse')",
            tenant, DAILY_LIMIT,
        )
        await conn.execute("SELECT set_config('app.tenant_id','',false)")
    return tenant


async def _counting_request(principal) -> bool:
    """The **old**, check-then-act sequence. Kept so the race stays provable.

    This is what `ModelGateway.route` did before migration 032: read the
    ceiling, decide against that snapshot, call, then write the row the next
    reader will count.

    It is not dead code and it is not the application's path any more. It is the
    planted violation, kept permanently, so that
    `test_counting_rows_would_still_let_them_all_through` can show the
    reservation is load-bearing rather than decorative.
    """
    from core.models import check_quota
    from core.models.store import load_quota, record_usage

    quota, usage = await load_quota(principal)
    verdict = check_quota(quota, usage)
    if not verdict.allowed:
        return False

    # A real call would happen here. The window between the check and the
    # write is what the race lives in, and it is wider in production than it
    # is here — a model call takes far longer than this yield.
    await asyncio.sleep(0)

    await record_usage(principal, {"outcome": "ok", "model": "test", "cost": 0})
    return True


async def _reserved_request(principal) -> bool:
    """The current sequence: reserve the slot atomically, then spend it.

    Mirrors `IRAModelGateway.route` after migration 032 — `check_quota` for the
    specific refusal message, then `reserve_daily_request`, which is the step
    that actually holds under concurrency.
    """
    from core.models import check_quota
    from core.models.store import load_quota, record_usage, reserve_daily_request

    quota, usage = await load_quota(principal)
    if not check_quota(quota, usage).allowed:
        return False
    if not await reserve_daily_request(principal):
        return False

    await asyncio.sleep(0)

    await record_usage(principal, {"outcome": "ok", "model": "test", "cost": 0})
    return True


async def _fire_concurrently(principal, sequence):
    """`CONCURRENT_REQUESTS` of `sequence`, released together. Bounded."""
    start = asyncio.Event()

    async def worker() -> bool:
        await start.wait()
        return await sequence(principal)

    tasks = [asyncio.create_task(worker()) for _ in range(CONCURRENT_REQUESTS)]
    start.set()
    results = await asyncio.wait_for(
        asyncio.gather(*tasks, return_exceptions=True), timeout=OP_TIMEOUT
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"requests raised instead of being refused: {failures[:3]}"
    return sum(1 for r in results if r is True)


async def test_a_daily_ceiling_is_still_a_ceiling_under_concurrency(
    pool, metered_tenant
):
    """**A quota that only holds when requests arrive one at a time is not a quota.**

    `CONCURRENT_REQUESTS` released together against a ceiling of `DAILY_LIMIT`.
    Exactly `DAILY_LIMIT` must get through.

    Before migration 032 this admitted **24 of 24**. Admission is now a single
    conditional UPDATE (`reserve_daily_request`), so PostgreSQL serialises
    concurrent callers on the row lock and the statement either takes a slot or
    matches no row.
    """
    principal = _principal(metered_tenant)
    admitted = await _fire_concurrently(principal, _reserved_request)

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,false)", str(metered_tenant))
        reserved = await conn.fetchval(
            "SELECT daily_requests_used FROM usage_quotas WHERE tenant_id = $1",
            metered_tenant,
        )
        await conn.execute("SELECT set_config('app.tenant_id','',false)")

    assert admitted == DAILY_LIMIT, (
        f"the daily ceiling was {DAILY_LIMIT} and {admitted} of "
        f"{CONCURRENT_REQUESTS} concurrent requests were admitted"
    )
    assert reserved == DAILY_LIMIT, (
        f"the counter reads {reserved} against a ceiling of {DAILY_LIMIT}. The "
        f"reservation is what a bill and a rate limit are built from, so it "
        f"being wrong matters even when the caller count happens to be right."
    )


async def test_counting_rows_would_still_let_them_all_through(pool, metered_tenant):
    """The planted violation, kept permanently rather than run once and deleted.

    This drives the **old** check-then-act sequence — `load_quota` ->
    `check_quota` -> `record_usage`, with no reservation — and asserts it
    overshoots badly.

    Two things it is for. It proves the reservation in the test above is
    load-bearing rather than incidental: without this, a `reserve_daily_request`
    that silently returned True for everything would leave that test green. And
    it pins the reason migration 032 exists, so that anyone tempted to go back
    to counting rows has to delete an explicit statement of why counting does
    not work.

    If this test ever *passes* — if counting starts holding under concurrency —
    something has changed that this file does not understand, and that is worth
    a failure too.
    """
    principal = _principal(metered_tenant)
    admitted = await _fire_concurrently(principal, _counting_request)

    assert admitted > DAILY_LIMIT, (
        f"the check-then-act sequence admitted {admitted} of "
        f"{CONCURRENT_REQUESTS} against a ceiling of {DAILY_LIMIT} — it was "
        f"expected to overshoot, because every caller reads the usage count "
        f"before any caller writes its row. It did not, which means this test "
        f"is no longer demonstrating the race that migration 032 exists to fix, "
        f"and the guard above may now be proving nothing."
    )


async def test_a_saturated_pool_sheds_load_instead_of_hanging(pool):
    """**A request that waits forever is an outage that reports itself as slow.**

    asyncpg's `Pool.acquire()` has **no default timeout**. With every connection
    busy, callers queue until one frees up, however long that takes. That is
    worse than it sounds: each waiter holds its own worker and task while it
    waits, so saturation compounds instead of settling — and nothing alerts,
    because no error rate has moved. Nothing has errored yet.

    `utils.db.acquire()` now passes `ACQUIRE_TIMEOUT_SECONDS` and raises
    `PoolSaturated`. This test saturates the pool, then asks for one more
    connection through the application's helper and requires it to **give up
    promptly and say why**.

    The first version of this test asserted that the raw `pool.acquire()` timed
    out, and called that "sheds load". It proved the opposite — that the pool
    queues — and it was named for the behaviour that was wanted rather than the
    behaviour being measured.
    """
    import utils.db as db_module
    from utils.db import PoolSaturated, acquire

    release = asyncio.Event()
    ready = asyncio.Semaphore(0)
    holders: list[asyncio.Task] = []

    async def hold():
        async with acquire():
            ready.release()
            await release.wait()

    size = pool.get_max_size()
    for _ in range(size):
        holders.append(asyncio.create_task(hold()))

    original = db_module.ACQUIRE_TIMEOUT_SECONDS
    # Shortened so the test does not sit through the production timeout. The
    # behaviour under test is "does it give up at all", not the exact number.
    db_module.ACQUIRE_TIMEOUT_SECONDS = 1.0
    try:
        for _ in range(size):
            await asyncio.wait_for(ready.acquire(), timeout=OP_TIMEOUT)

        with pytest.raises(PoolSaturated) as caught:
            async with await asyncio.wait_for(
                _borrow_one(), timeout=OP_TIMEOUT
            ):
                pass

        assert "saturated" in str(caught.value).lower() or "free" in str(caught.value), (
            f"the refusal must say the pool was saturated, so an operator tunes "
            f"the pool rather than the query. Got: {caught.value}"
        )
    finally:
        db_module.ACQUIRE_TIMEOUT_SECONDS = original
        release.set()
        await asyncio.wait_for(
            asyncio.gather(*holders, return_exceptions=True), timeout=OP_TIMEOUT
        )


async def _borrow_one():
    """Enter `acquire()` so the saturation test can bound the whole attempt."""
    from utils.db import acquire

    cm = acquire()
    await cm.__aenter__()
    return cm


async def test_the_pool_gets_every_connection_back_after_sustained_load(pool):
    """**A leak shows up as an outage hours later, under load that already passed.**

    Sustained interleaved work through the same helper the application uses,
    then a check that the pool is idle again. A connection leaked per request —
    an `acquire()` whose release is skipped on an error path, say — would leave
    the pool permanently short, and the symptom would appear long after the
    traffic that caused it.

    The count, again, is chosen to interleave rather than to describe capacity.
    """
    from utils.db import acquire

    rounds = 40

    async def churn(n: int):
        for _ in range(rounds):
            async with acquire() as conn:
                await conn.fetchval("SELECT $1::int", n)
            await asyncio.sleep(0)

    await asyncio.wait_for(
        asyncio.gather(*(churn(i) for i in range(8))), timeout=OP_TIMEOUT
    )

    # asyncpg reports the pool's own bookkeeping: size is what exists, idle is
    # what is available. Anything held after every caller has finished is leaked.
    in_use = pool.get_size() - pool.get_idle_size()
    assert in_use == 0, (
        f"{in_use} connection(s) were still checked out after {rounds * 8} "
        f"completed operations. Every caller has returned, so nothing should be "
        f"held: this is a leak, and it exhausts the pool under sustained traffic."
    )


async def test_a_failing_operation_still_returns_its_connection(pool):
    """The leak path that actually happens: an exception mid-query.

    Happy-path churn does not exercise it. A connection acquired and then lost
    to a raised exception is the classic pool leak, and it is exactly the case
    that only appears under load — because under load, something is always
    failing somewhere.
    """
    from utils.db import acquire

    for _ in range(20):
        with pytest.raises(asyncpg.PostgresError):
            async with acquire() as conn:
                await conn.fetchval("SELECT 1 / 0")

    in_use = pool.get_size() - pool.get_idle_size()
    assert in_use == 0, (
        f"{in_use} connection(s) were still checked out after 20 failed queries. "
        f"An exception path is not returning its connection to the pool."
    )
