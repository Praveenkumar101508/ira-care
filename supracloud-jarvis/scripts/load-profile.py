#!/usr/bin/env python3
"""Blocker B4 / readiness R04 — a load profiler for IRA's data layer.

── What this is, and what it deliberately is not ────────────────────────────

R04 asks for load tests: throughput, latency percentiles, the saturation point.
This measures those. It does **not** measure IRA's service capacity, and the
reason is not modesty:

  * **The HTTP API cannot be started here at all.** `app.factory.create_app`
    imports the memory stack, which imports `sentence_transformers`, which is
    not installable in this environment. There is no service to drive, so no
    end-to-end request-per-second figure exists to be measured or misquoted.
  * **The absolute numbers below describe this machine.** A shared,
    CPU-throttled container running the load generator, the application code
    and PostgreSQL on the same cores. Quoting them as capacity would describe
    the container.

So what is the point?

**Two of the three outputs survive a change of hardware, and one does not.**

  1. **Ratios** — what tenant isolation costs per query, measured as bound vs
     unbound. A ratio is a property of the work, not of the clock speed.
  2. **Shapes** — where throughput stops rising with concurrency, and whether
     latency knees gracefully or the error rate explodes. Bigger hardware moves
     *where* the knee is; it does not change *that* there is one, or what
     happens past it.
  3. **Absolute throughput** — this is the one that does not survive. It is
     recorded as a **regression baseline** only: run the same profile before
     and after a change and a 3x slowdown shows up on any hardware, even though
     the raw number means nothing on its own.

The per-tenant quota ceiling below is the sharpest finding of the three,
because it is *architectural*: `reserve_daily_request` is a conditional UPDATE
against one row per tenant, so concurrent requests for a single tenant
serialise on that row lock. That ceiling exists on any hardware. Only its
height changes.

Usage:
    IRA_TEST_DATABASE_DSN=postgresql://... ./load-profile.py
    IRA_TEST_DATABASE_DSN=postgresql://... ./load-profile.py --json baseline.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ira"))

try:
    import asyncpg
except ImportError:  # pragma: no cover
    print("asyncpg is required", file=sys.stderr)
    raise SystemExit(2)

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")

#: The sweep. Doubling, because the knee is found by where the curve stops
#: doubling with it — a linear sweep wastes runs on the flat part.
CONCURRENCY = [1, 2, 4, 8, 16, 32, 64]

#: Operations per level. Enough that percentiles mean something, small enough
#: that the whole sweep runs in well under a minute.
OPS_PER_LEVEL = 400

#: Hard ceiling on any single operation. Nothing here is allowed to hang — the
#: R07 lesson, applied to a file whose whole purpose is to saturate things.
OP_TIMEOUT = 30.0


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1)))))
    return ordered[k]


class Workload:
    """One measurable operation, plus whatever setup it needs."""

    name = ""
    description = ""

    async def setup(self, pool) -> None: ...
    async def run_one(self, pool, worker: int) -> None: ...


class UnboundRead(Workload):
    name = "unbound_read"
    description = "SELECT on a pooled connection with no tenant bound (RLS returns nothing)"

    async def run_one(self, pool, worker: int) -> None:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT COUNT(*) FROM files")


class TenantRead(Workload):
    name = "rls_read"
    description = "the same SELECT through tenant_connection — RLS bound, the real read path"

    def __init__(self) -> None:
        self.tenants: list[uuid.UUID] = []

    async def setup(self, pool) -> None:
        run = uuid.uuid4().hex[:8]
        async with pool.acquire() as conn:
            for i in range(8):
                tid = uuid.uuid4()
                await conn.execute(
                    "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
                    tid, f"Load {run} {i}", f"load-{run}-{i}",
                )
                await conn.execute("SELECT set_config('app.tenant_id',$1,false)", str(tid))
                await conn.execute(
                    "INSERT INTO files (tenant_id,user_id,filename,storage_path,sha256) "
                    "VALUES ($1,$2,$3,$4,$5)",
                    tid, "u", f"{run}-{i}.txt", f"/tmp/{run}-{i}", f"{i:064d}",
                )
            await conn.execute("SELECT set_config('app.tenant_id','',false)")
            self.tenants.append(tid)
            self.tenants = [t for t in self.tenants]
            # Re-read them all rather than trusting the loop variable.
            rows = await conn.fetch(
                "SELECT id FROM tenants WHERE slug LIKE $1", f"load-{run}-%"
            )
            self.tenants = [r["id"] for r in rows]

    async def run_one(self, pool, worker: int) -> None:
        from core.identity import Principal
        from core.editions import EDITION_CLOUD
        from core.identity.db_context import tenant_connection

        tid = self.tenants[worker % len(self.tenants)]
        principal = Principal(
            user_id=uuid.uuid5(tid, "user"), tenant_id=tid,
            session_id=uuid.uuid4(), edition=EDITION_CLOUD,
        )
        async with tenant_connection(principal) as conn:
            await conn.fetchval("SELECT COUNT(*) FROM files")


class QuotaReserveOneTenant(Workload):
    """The architectural ceiling: every request for ONE tenant hits one row."""

    name = "quota_reserve_single_tenant"
    description = "reserve_daily_request for a single tenant — serialises on one row lock"

    def __init__(self) -> None:
        self.tenant: uuid.UUID | None = None

    async def setup(self, pool) -> None:
        run = uuid.uuid4().hex[:8]
        tid = uuid.uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
                tid, f"Quota {run}", f"quota-{run}",
            )
            await conn.execute("SELECT set_config('app.tenant_id',$1,false)", str(tid))
            # No daily_request_limit: unmetered, so the reservation always
            # succeeds and we measure the LOCK, not the refusal path.
            await conn.execute(
                "INSERT INTO usage_quotas (tenant_id, on_exceeded) VALUES ($1,'refuse')", tid
            )
            await conn.execute("SELECT set_config('app.tenant_id','',false)")
        self.tenant = tid

    async def run_one(self, pool, worker: int) -> None:
        from core.identity import Principal
        from core.editions import EDITION_CLOUD
        from core.models.store import reserve_daily_request

        principal = Principal(
            user_id=uuid.uuid5(self.tenant, "user"), tenant_id=self.tenant,
            session_id=uuid.uuid4(), edition=EDITION_CLOUD,
        )
        await reserve_daily_request(principal)


async def measure(pool, workload: Workload, concurrency: int, ops: int) -> dict:
    """One point on the curve. Returns throughput, percentiles and errors."""
    latencies: list[float] = []
    errors = 0
    per_worker = max(1, ops // concurrency)

    async def worker(n: int) -> None:
        nonlocal errors
        for _ in range(per_worker):
            t0 = time.perf_counter()
            try:
                await asyncio.wait_for(workload.run_one(pool, n), timeout=OP_TIMEOUT)
                latencies.append((time.perf_counter() - t0) * 1000)
            except Exception:  # noqa: BLE001 - an error IS the measurement here
                errors += 1

    # Warm up before the clock starts. Without this the FIRST level measured
    # carries every cold cost — connection establishment, PostgreSQL plan
    # caching, Python import of the code path — and reports a throughput seven
    # times lower than the next level up, which looks like a scaling result and
    # is an artefact.
    for i in range(min(concurrency, 4)):
        try:
            await asyncio.wait_for(workload.run_one(pool, i), timeout=OP_TIMEOUT)
        except Exception:  # noqa: BLE001 - warmup failures are not measurements
            pass

    started = time.perf_counter()
    await asyncio.gather(*(worker(i) for i in range(concurrency)))
    elapsed = time.perf_counter() - started
    done = len(latencies)

    return {
        "concurrency": concurrency,
        "completed": done,
        "errors": errors,
        "seconds": round(elapsed, 3),
        "throughput_per_sec": round(done / elapsed, 1) if elapsed > 0 else 0.0,
        "p50_ms": round(pct(latencies, 50), 2),
        "p95_ms": round(pct(latencies, 95), 2),
        "p99_ms": round(pct(latencies, 99), 2),
        "mean_ms": round(statistics.fmean(latencies), 2) if latencies else float("nan"),
    }


def find_knee(points: list[dict]) -> dict:
    """Where throughput stops rising with concurrency.

    Defined as the last level whose throughput is at least 5% above the
    previous one. Past it, adding concurrency buys latency and not work — which
    is the useful thing to know, and the part that survives a hardware change.
    """
    knee = points[0]
    for prev, cur in zip(points, points[1:]):
        if cur["throughput_per_sec"] > prev["throughput_per_sec"] * 1.05:
            knee = cur
        else:
            break
    return knee


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", help="write the full profile here")
    parser.add_argument("--ops", type=int, default=OPS_PER_LEVEL)
    args = parser.parse_args()

    if not DSN:
        print("IRA_TEST_DATABASE_DSN is not set — nothing to profile.", file=sys.stderr)
        return 2

    import utils.db as db_module

    profile: dict = {
        "environment_warning": (
            "Absolute throughput describes the machine this ran on, which also "
            "ran the load generator and PostgreSQL. Use it as a regression "
            "baseline, never as service capacity. The HTTP API is not exercised "
            "at all: it cannot be started without the model stack."
        ),
        "workloads": {},
    }

    for workload in (UnboundRead(), TenantRead(), QuotaReserveOneTenant()):
        pool = await asyncpg.create_pool(dsn=DSN, min_size=4, max_size=64)
        previous, db_module._pool = db_module._pool, pool
        try:
            await workload.setup(pool)
            points = []
            for c in CONCURRENCY:
                points.append(await measure(pool, workload, c, args.ops))
            knee = find_knee(points)
            profile["workloads"][workload.name] = {
                "description": workload.description,
                "points": points,
                "knee": {
                    "concurrency": knee["concurrency"],
                    "throughput_per_sec": knee["throughput_per_sec"],
                },
                "peak_throughput_per_sec": max(p["throughput_per_sec"] for p in points),
            }

            print(f"\n── {workload.name} ──  {workload.description}")
            print(f"{'conc':>5} {'ops/s':>10} {'p50 ms':>9} {'p95 ms':>9} {'p99 ms':>9} {'err':>5}")
            for p in points:
                print(f"{p['concurrency']:>5} {p['throughput_per_sec']:>10} "
                      f"{p['p50_ms']:>9} {p['p95_ms']:>9} {p['p99_ms']:>9} {p['errors']:>5}")
            print(f"      knee at concurrency {knee['concurrency']} "
                  f"({knee['throughput_per_sec']} ops/s)")
        finally:
            db_module._pool = previous
            try:
                await asyncio.wait_for(pool.close(), timeout=10.0)
            except Exception:  # noqa: BLE001
                pool.terminate()

    # The ratio, which is the part that travels.
    unbound = profile["workloads"]["unbound_read"]["peak_throughput_per_sec"]
    bound = profile["workloads"]["rls_read"]["peak_throughput_per_sec"]
    if bound > 0:
        profile["rls_overhead_ratio"] = round(unbound / bound, 2)
        print(f"\nRLS overhead: an unbound read is {unbound / bound:.2f}x the throughput "
              f"of a tenant-bound one.")
        print("That ratio is a property of the work and survives a change of hardware.")

    if args.json:
        Path(args.json).write_text(json.dumps(profile, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
