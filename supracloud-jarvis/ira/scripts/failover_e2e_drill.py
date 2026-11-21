#!/usr/bin/env python3
"""P14-R2 — the end-to-end failover drill, and the first real RTO.

── What was missing ───────────────────────────────────────────────────────

Three mechanisms existed and nothing joined them. Every document said so:

  * **Detection** — `core/runtime/failure_detector.py` (P14-02). Turns probe
    results into `HEALTHY`/`SUSPECTED`/`FAILED`. Its `actionable` property was
    documented as "whether a caller should treat this as a failure" and had no
    caller.
  * **Promotion** — `scripts/failover-drill.sh` (Phase 14). Builds a streaming
    standby, kills the primary, promotes. Its number is *promotion time alone*,
    and the script says so in capitals.
  * **Redirection** — `infra/terraform/redirection/` (P15-04). An HAProxy
    router in front of two **independent** instances. Nothing is promoted
    there, so it is not redirection to a promoted primary.

    "The three have never been run end to end, so the interval that would be
    an RTO has never been measured. Adding the three numbers is not measuring
    it."

This runs them as one sequence and measures the interval.

── What the number means ──────────────────────────────────────────────────

**RTO** is measured from the SIGKILL to the moment a client — a new
connection, to the same unchanged address, with nothing about the client
reconfigured — completes a **write**. Not to "the standby started", not to
"promotion returned": a standby that is up but still in recovery has recovered
nothing, and a router that answers has not necessarily got a writable node
behind it.

That interval contains everything: the probe interval before the first failing
observation, the detector's declaration threshold, promotion, verification, the
router update, and the client's reconnect. Those are the parts a real outage
has, which is why the parts were never the point.

**RPO** is the count of rows a client was told were committed that are not
present after the failover. Each row is its own transaction, so "committed"
means PostgreSQL made a promise to a client about that row.

── What it is NOT ─────────────────────────────────────────────────────────

**It is an ENGINEERING measurement of this topology on one host.** It is not
the commercial RTO/RPO target, which is the owner's and stays the owner's.

Loopback has no network. Async replication's RPO is a function of link latency
and write rate, and a measurement taken with neither says little about a
deployment that has both. Read a zero as "this mechanism did not lose data
under these conditions", never as "this cannot lose data".

Two PostgreSQL processes on one machine also share a page cache, a disk and a
kernel. A host failure takes both. This measures the failover mechanism, not
the availability of a deployment.

── Destructive ────────────────────────────────────────────────────────────

It SIGKILLs a postmaster and promotes a standby, after which the pair has
diverged and must be rebuilt — which `--runs N` does between iterations. It
builds its own cluster in a scratch directory and never touches an existing
one, but it still refuses without `IRA_TEST_ALLOW_DESTRUCTIVE=1`.

Usage:
    IRA_TEST_ALLOW_DESTRUCTIVE=1 python3 scripts/failover_e2e_drill.py --runs 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

IRA_ROOT = Path(__file__).resolve().parents[1]
if str(IRA_ROOT) not in sys.path:
    sys.path.insert(0, str(IRA_ROOT))

from core.runtime.failover_orchestrator import (  # noqa: E402
    DEPENDENCY,
    FailoverOrchestrator,
    FailoverStage,
)
from core.runtime.failure_detector import (  # noqa: E402
    FAILURE_THRESHOLD,
    DependencyHealth,
    FailureDetector,
)

PGBIN = Path(os.environ.get("IRA_PGBIN", "/usr/lib/postgresql/16/bin"))
PROBE_INTERVAL = float(os.environ.get("IRA_DRILL_PROBE_INTERVAL", "0.1"))
PROBE_TIMEOUT = float(os.environ.get("IRA_DRILL_PROBE_TIMEOUT", "1.0"))
ROWS = int(os.environ.get("IRA_DRILL_ROWS", "200"))
#: Ceiling on the whole sequence. A drill that hangs is a drill that reports
#: nothing, which is worse than one that fails.
DEADLINE_SECONDS = float(os.environ.get("IRA_DRILL_DEADLINE", "120"))
#: How long a freshly started standby may take to reach `streaming`.
STREAMING_WAIT_SECONDS = 30.0


# ── Small helpers ───────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def _as_postgres(argv: list[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    """Run a PostgreSQL binary as an unprivileged user when we are root.

    initdb and postgres refuse to run as root, and CI runs as a normal user
    while this container runs as root — so the drill has to handle both rather
    than assume one.
    """
    if os.geteuid() == 0:
        return _run(["setpriv", "--reuid", "postgres", "--regid", "postgres",
                     "--clear-groups", *argv], cwd=str(cwd) if cwd else None)
    return _run(argv, cwd=str(cwd) if cwd else None)


class DrillError(RuntimeError):
    """A drill that could not be set up reports UNKNOWN, never a good number."""


# ── The cluster: a primary and a streaming standby ──────────────────────────

@dataclass
class Cluster:
    root: Path
    primary_dir: Path
    standby_dir: Path
    primary_port: int
    standby_port: int

    @classmethod
    def create(cls, root: Path) -> "Cluster":
        return cls(root=root,
                   primary_dir=root / "primary", standby_dir=root / "standby",
                   primary_port=_free_port(), standby_port=_free_port())

    # ── build ──
    def init_primary(self) -> None:
        proc = _as_postgres([str(PGBIN / "initdb"), "-D", str(self.primary_dir),
                             "-U", "postgres", "--auth=trust"])
        if proc.returncode != 0:
            raise DrillError(f"initdb failed: {proc.stderr[-800:]}")

        # `wal_level=replica` and a slot make the standby a real streaming
        # replica rather than a file copy. synchronous_commit stays at its
        # default (`on`, which is async-to-the-standby) BECAUSE that is the
        # configuration whose data-loss window this drill is meant to observe.
        # Setting it to `remote_apply` would measure a different system and
        # report zero rows lost by construction.
        (self.primary_dir / "postgresql.auto.conf").write_text(
            "\n".join([
                "wal_level = replica",
                "max_wal_senders = 4",
                "max_replication_slots = 4",
                "hot_standby = on",
                "listen_addresses = '127.0.0.1'",
                f"port = {self.primary_port}",
                # The drill's OWN socket directory, not the packaged default.
                #
                # PostgreSQL on Ubuntu is compiled with
                # `/var/run/postgresql` as the unix socket directory. It is
                # owned by `postgres` and, on a CI runner, the drill runs as
                # `runner` — so the postmaster died with
                # `could not create lock file ... Permission denied` while
                # every local run (as root, dropping to `postgres`) worked
                # fine. A drill that only runs where it was written is not a
                # drill.
                #
                # It is also the right thing regardless: this builds a
                # throwaway cluster and has no business writing into a
                # directory the system's own PostgreSQL uses. Every connection
                # here is TCP to 127.0.0.1, so nothing depends on where the
                # socket lands.
                f"unix_socket_directories = '{self.root}'",
            ]) + "\n", encoding="utf-8")
        with (self.primary_dir / "pg_hba.conf").open("a", encoding="utf-8") as fh:
            fh.write("host replication postgres 127.0.0.1/32 trust\n")
        self.start_primary()

    def start_primary(self) -> None:
        self._pg_ctl_start(self.primary_dir, self.primary_port, "primary")

    def build_standby(self) -> None:
        if self.standby_dir.exists():
            shutil.rmtree(self.standby_dir, ignore_errors=True)
        proc = _as_postgres([
            str(PGBIN / "pg_basebackup"), "-h", "127.0.0.1",
            "-p", str(self.primary_port), "-U", "postgres",
            "-D", str(self.standby_dir), "-R", "-X", "stream", "-c", "fast"])
        if proc.returncode != 0:
            raise DrillError(f"pg_basebackup failed: {proc.stderr[-800:]}")
        with (self.standby_dir / "postgresql.auto.conf").open("a", encoding="utf-8") as fh:
            fh.write(f"\nport = {self.standby_port}\n"
                     f"listen_addresses = '127.0.0.1'\n"
                     f"unix_socket_directories = '{self.root}'\n")
        self._pg_ctl_start(self.standby_dir, self.standby_port, "standby")

        # A pair that is not actually replicating would make the drill measure
        # nothing — the "standby" would be a stale copy and the RPO meaningless.
        if self.sql(self.standby_port, "SELECT pg_is_in_recovery()") != "t":
            raise DrillError("the standby is not in recovery; it is not a standby")
        # `pg_ctl start -w` returns once the standby accepts connections, which
        # can be before its WAL receiver has reached the primary: read once,
        # `pg_stat_replication` is sometimes still empty. Wait for it, bounded.
        state, deadline = "", time.monotonic() + STREAMING_WAIT_SECONDS
        while time.monotonic() < deadline:
            state = self.sql(self.primary_port,
                             "SELECT state FROM pg_stat_replication LIMIT 1")
            if state == "streaming":
                break
            time.sleep(0.2)
        if state != "streaming":
            raise DrillError(
                f"replication state is {state!r}, not 'streaming'. The pair is "
                f"not replicating, so any RPO this reported would be an "
                f"artefact of the setup.")

    def _pg_ctl_start(self, datadir: Path, port: int, label: str) -> None:
        proc = _as_postgres([str(PGBIN / "pg_ctl"), "-D", str(datadir),
                             "-l", str(self.root / f"{label}.log"),
                             "-w", "-t", "60", "start"])
        if proc.returncode != 0:
            log = (self.root / f"{label}.log")
            tail = log.read_text(encoding="utf-8", errors="replace")[-800:] if log.exists() else ""
            raise DrillError(f"{label} did not start: {proc.stderr[-400:]}\n{tail}")
        if not self.wait_for(port, 30):
            raise DrillError(f"{label} started but never answered on {port}")

    # ── use ──
    def sql(self, port: int, statement: str, *, timeout: int = 10) -> str:
        proc = _run([shutil.which("psql") or "psql", "-U", "postgres",
                     "-h", "127.0.0.1", "-p", str(port), "-d", "postgres",
                     "-tAqc", statement], timeout=timeout)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def wait_for(self, port: int, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.sql(port, "SELECT 1") == "1":
                return True
            time.sleep(0.1)
        return False

    def kill_primary(self) -> None:
        """SIGKILL, not a clean stop.

        A clean shutdown flushes and ships everything outstanding — a
        maintenance event, not a failure. It would report an RPO of zero by
        construction and measure nothing.
        """
        pid = int((self.primary_dir / "postmaster.pid").read_text(
            encoding="utf-8").splitlines()[0])
        os.kill(pid, signal.SIGKILL)

    def stop_all(self) -> None:
        for datadir in (self.standby_dir, self.primary_dir):
            if (datadir / "postmaster.pid").exists():
                _as_postgres([str(PGBIN / "pg_ctl"), "-D", str(datadir),
                              "-m", "immediate", "-w", "-t", "20", "stop"])


# ── The router: one stable address, a backend that moves ────────────────────

@dataclass
class Router:
    root: Path
    port: int
    admin_socket: Path
    proc: Optional[subprocess.Popen] = None

    @classmethod
    def start(cls, root: Path, primary_port: int, standby_port: int) -> "Router":
        port = _free_port()
        admin = root / "haproxy.sock"
        # The standby starts DISABLED. That is the correctness of the whole
        # drill: HAProxy left to its own health checks would move traffic to
        # the standby the moment the primary died — to a node still in
        # recovery, which serves reads and refuses writes. The orchestrator
        # enables it, and only after the promotion is verified.
        config = f"""
global
    stats socket {admin} mode 660 level admin
    maxconn 200
defaults
    mode tcp
    timeout connect 2s
    timeout client 30s
    timeout server 30s
listen pg
    bind 127.0.0.1:{port}
    server primary 127.0.0.1:{primary_port} check inter 200ms fall 2 rise 1
    server standby 127.0.0.1:{standby_port} check inter 200ms fall 2 rise 1 disabled
"""
        cfg = root / "haproxy.cfg"
        cfg.write_text(config, encoding="utf-8")
        haproxy = shutil.which("haproxy")
        if not haproxy:
            raise DrillError(
                "haproxy is not installed. The drill needs a router to have a "
                "stable client address at all; without one there is nothing to "
                "redirect and any number here would be promotion time again.")
        proc = subprocess.Popen(
            [haproxy, "-f", str(cfg), "-db"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        router = cls(root=root, port=port, admin_socket=admin, proc=proc)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if admin.exists():
                return router
            if proc.poll() is not None:
                # `stderr` is Optional in the type stubs even though it is a
                # pipe here; read it defensively so a router that died without
                # saying why still produces a message rather than a traceback
                # about the message.
                why = proc.stderr.read()[-500:] if proc.stderr else "(no output)"
                raise DrillError(f"haproxy exited: {why}")
            time.sleep(0.05)
        raise DrillError("haproxy never created its admin socket")

    def command(self, line: str) -> str:
        proc = _run(["socat", "-", f"UNIX-CONNECT:{self.admin_socket}"],
                    input=line + "\n", timeout=10)
        if proc.returncode != 0:
            raise DrillError(f"router command {line!r} failed: {proc.stderr[-300:]}")
        return proc.stdout.strip()

    def point_at_standby(self) -> None:
        """Move the address. The client is not told and does not change."""
        self.command("enable server pg/standby")
        self.command("set server pg/standby state ready")
        self.command("set server pg/primary state maint")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ── The plan the orchestrator drives ────────────────────────────────────────

class PostgresFailoverPlan:
    """A real `FailoverPlan`: pg_ctl promote, a real verification, a real router.

    `verified()` requires BOTH that the node has left recovery and that it
    accepts a write. The first alone is not enough — and this is exactly where
    a composition that looked finished would still be broken, because
    `pg_ctl promote` returns as soon as it has written the signal file.
    """

    def __init__(self, cluster: Cluster, router: Router) -> None:
        self.cluster = cluster
        self.router = router
        self.promoted_at: Optional[float] = None

    async def promote(self) -> None:
        self.promoted_at = time.monotonic()
        proc = _as_postgres([str(PGBIN / "pg_ctl"), "-D",
                             str(self.cluster.standby_dir), "promote"])
        if proc.returncode != 0:
            raise DrillError(f"pg_ctl promote failed: {proc.stderr[-400:]}")

    async def verified(self) -> bool:
        if self.cluster.sql(self.cluster.standby_port,
                            "SELECT pg_is_in_recovery()") != "f":
            return False
        # Still in doubt until it takes a write. `pg_is_in_recovery()` going
        # false is necessary and not sufficient.
        return self.cluster.sql(
            self.cluster.standby_port,
            "CREATE TABLE IF NOT EXISTS failover_writable (n int); "
            "INSERT INTO failover_writable VALUES (1) RETURNING 1") == "1"

    async def redirect(self) -> None:
        self.router.point_at_standby()


# ── One run ─────────────────────────────────────────────────────────────────

async def _count(port: int, *, at_most: Optional[int] = None,
                 above: Optional[int] = None) -> int:
    """Count surviving probe rows, with the bound passed as a PARAMETER.

    The first version took a `predicate` string and interpolated it into the
    statement. Bandit flagged it (B608) and the flag was worth acting on rather
    than baselining: a counting helper that accepts a SQL fragment is one
    refactor away from accepting one that came from somewhere else. The bounds
    are integers, so they can simply be bound.
    """
    import asyncpg

    conn = await asyncio.wait_for(
        asyncpg.connect(host="127.0.0.1", port=port, user="postgres",
                        database="postgres"), timeout=10)
    try:
        if at_most is not None:
            return int(await conn.fetchval(
                "SELECT COUNT(*) FROM failover_probe WHERE n <= $1", at_most))
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM failover_probe WHERE n > $1 AND n < 999999",
            above))
    finally:
        await conn.close(timeout=2)


@dataclass
class RunResult:
    run: int
    rto_seconds: float
    #: Kill to DECLARATION. Distinct from the detector's own
    #: `threshold_detection_ms`, which starts at the first failing probe and so
    #: cannot see the interval this measures.
    time_to_detect_seconds: Optional[float]
    rows_committed: int
    rows_survived: int
    rows_lost: int
    #: Rows the server made durable and never acknowledged, because it died
    #: between the commit and the reply. Reported, never netted off the loss.
    unacknowledged_but_durable: int
    detection_observations: int
    orchestrator: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run": self.run,
            "rto_seconds": round(self.rto_seconds, 3),
            "time_to_detect_seconds": (round(self.time_to_detect_seconds, 3)
                                       if self.time_to_detect_seconds else None),
            "rows_acknowledged": self.rows_committed,
            "rows_survived": self.rows_survived,
            "rpo_committed_rows_lost": self.rows_lost,
            "unacknowledged_but_durable": self.unacknowledged_but_durable,
            "detection_observations": self.detection_observations,
            "orchestrator": self.orchestrator,
        }


async def _client_write(port: int, statement: str, *, timeout: float) -> bool:
    """A REAL client, through the router, on a NEW connection each time.

    New every call on purpose: the point of redirection is that a client which
    reconnects to the same unchanged address lands on the new primary. Reusing
    a pooled connection would prove nothing, because the interesting connection
    is the one made after the old backend died.
    """
    import asyncpg

    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(host="127.0.0.1", port=port, user="postgres",
                            database="postgres"), timeout=timeout)
    except Exception:
        return False
    try:
        await asyncio.wait_for(conn.execute(statement), timeout=timeout)
        return True
    except Exception:
        return False
    finally:
        try:
            await conn.close(timeout=1)
        except Exception:
            pass


async def _writer(port: int, acked: list[int]) -> None:
    """Commit rows one transaction at a time until cancelled or the server dies.

    Each row is its own transaction, so an entry in `acked` is a promise
    PostgreSQL made to a client about that specific row — which is what makes
    "committed but did not survive" a meaningful count rather than an estimate.

    It holds ONE connection, unlike the probe, because that is what a real
    client does: the interesting rows are the ones acknowledged microseconds
    before the postmaster died, and reconnecting per row would leave the
    connection idle for most of the window.
    """
    import asyncpg

    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(host="127.0.0.1", port=port, user="postgres",
                            database="postgres"), timeout=10)
    except Exception:
        return
    n = 0
    try:
        while True:
            n += 1
            await conn.execute(
                "INSERT INTO failover_probe (n) VALUES ($1)", n)
            acked.append(n)
    except (asyncio.CancelledError, Exception):  # noqa: B014 — the kill lands here
        return
    finally:
        try:
            await conn.close(timeout=1)
        except Exception:
            pass


def _break_replication(cluster: "Cluster") -> None:
    """Cut the standby off from the primary's WAL, and PROVE it is cut off.

    Two earlier versions of this did not work, and both failed silently — which
    is the failure mode this whole programme is about, arriving inside the tool
    built to detect it.

    1. **SIGSTOP the WAL receiver.** Promotion ends recovery by shutting the
       receiver down, and a stopped process never handles the signal, so
       `pg_ctl promote` timed out at 60s. The drill refused to report a number:
       the right refusal, the wrong reason.
    2. **SIGSTOP it and resume after the kill.** The receiver is stopped; the
       primary's walsender is not, and it keeps writing into the TCP socket.
       The kernel buffers it. On resume the receiver drained the buffer and
       wrote every "missing" record to disk — 544 rows acknowledged, 544
       survived, zero lost. The plant applied and had no effect.

    So this repoints `primary_conninfo` at a port with nothing behind it and
    terminates the current receiver. Reconnection attempts go nowhere, no WAL is
    buffered anywhere, and recovery can still be ended cleanly by promotion.

    Then it CHECKS: `pg_stat_replication` on the primary must go empty. Without
    that check this is a third silent no-op waiting to happen.
    """
    dead_port = _free_port()
    cluster.sql(cluster.standby_port,
                f"ALTER SYSTEM SET primary_conninfo = "
                f"'host=127.0.0.1 port={dead_port} user=postgres'")
    cluster.sql(cluster.standby_port, "SELECT pg_reload_conf()")
    cluster.sql(cluster.standby_port,
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE backend_type = 'walreceiver'")

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not cluster.sql(cluster.primary_port,
                           "SELECT 1 FROM pg_stat_replication LIMIT 1"):
            return
        time.sleep(0.1)
    raise DrillError(
        "the standby is STILL replicating after the connection was broken, so "
        "no replication gap exists. A zero data-loss window measured now would "
        "be an artefact of a plant that did not apply.")


async def one_run(run: int, root: Path, *, lag_rows: int = 0) -> RunResult:
    cluster = Cluster.create(root)
    router: Optional[Router] = None
    try:
        cluster.init_primary()
        cluster.build_standby()
        router = Router.start(root, cluster.primary_port, cluster.standby_port)

        # ── a client writing CONTINUOUSLY, through the router ──
        #
        # The first version of this drill committed its rows, paused, and then
        # killed the primary. It reported an RPO of zero on every run — and
        # that zero was an artefact of the drill's shape, not an observation.
        # With nothing in flight, async replication has already shipped
        # everything, so there is no data-loss window to see. A drill that
        # cannot lose data is not measuring whether data is lost.
        #
        # So the writer keeps committing right up to the SIGKILL, and
        # `committed` is read AFTER the kill — every acknowledgement in the
        # list is then one PostgreSQL made before it died, with no race.
        cluster.sql(cluster.primary_port,
                    "CREATE TABLE failover_probe (n bigint primary key)")
        acked: list[int] = []
        writer = asyncio.create_task(_writer(router.port, acked))
        try:
            baseline = time.monotonic() + 15
            while len(acked) < ROWS and time.monotonic() < baseline:
                await asyncio.sleep(0.02)
            if len(acked) < ROWS:
                raise DrillError(
                    f"only {len(acked)}/{ROWS} rows committed before the kill. "
                    f"The drill has not established a baseline, so any RPO it "
                    f"reported would be measuring its own setup.")

            # ── the failure ──
            detector = FailureDetector()
            orchestrator = FailoverOrchestrator(
                plan=PostgresFailoverPlan(cluster, router), detector=detector,
                verify_interval=0.05)

            # ── optional: make the standby fall behind ──
            #
            # THE SELF-TEST'S HOOK, and the reason it exists. On loopback this
            # drill reports an RPO of zero every run, and a zero that cannot be
            # anything else is not a measurement — it is a constant with a unit
            # attached. This stops the standby's WAL receiver, lets the writer
            # commit into the gap, and kills the primary while the standby is
            # behind. Those rows were acknowledged and are gone.
            #
            # It proves the RPO figure is derived from what survived, so the
            # zero above is an observation about loopback rather than an
            # arithmetic guarantee.
            if lag_rows:
                _break_replication(cluster)
                target = len(acked) + lag_rows
                gap = time.monotonic() + 15
                while len(acked) < target and time.monotonic() < gap:
                    await asyncio.sleep(0.02)
                if len(acked) < target:
                    raise DrillError(
                        "the writer could not commit into the replication gap")

            cluster.kill_primary()
            t_kill = time.monotonic()

        finally:
            writer.cancel()
            try:
                await writer
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass

        # Read after the kill: nothing can be acknowledged by a dead postmaster,
        # so this list is now final and `committed` is exact.
        committed = max(acked) if acked else 0

        # ── detection → promotion → redirection, driven by PRODUCT CODE ──
        #
        # The probe feeds the real FailureDetector; the real
        # FailoverOrchestrator reads it and decides. Nothing here calls
        # `promote` directly, which is the difference between this and
        # `failover-drill.sh`.
        observations = 0
        outcome = None
        t_declared: Optional[float] = None
        deadline = t_kill + DEADLINE_SECONDS
        while time.monotonic() < deadline:
            ok = await _client_write(router.port, "SELECT 1",
                                     timeout=PROBE_TIMEOUT)
            detector.observe(DEPENDENCY, ok)
            observations += 1
            # THE TIME-TO-DETECT, measured against the moment the dependency
            # ACTUALLY died rather than against the first failing probe.
            #
            # The SLO row for this objective carried `baseline=None` and the
            # caveat "NOTHING HAS BEEN MEASURED against this objective ...
            # nobody has yet killed a dependency and timed the declaration
            # against the moment it died". This is that measurement: t_kill is
            # a SIGKILL this process sent, so the death is known exactly, which
            # is the thing `threshold_detection_ms` could never include.
            if (t_declared is None
                    and detector.status(DEPENDENCY).health
                    is DependencyHealth.FAILED):
                t_declared = time.monotonic()
            outcome = await orchestrator.evaluate(enabled=True)
            if outcome.triggered:
                break
            await asyncio.sleep(PROBE_INTERVAL)

        if outcome is None or not outcome.triggered:
            raise DrillError(
                f"no failover was triggered within {DEADLINE_SECONDS}s after "
                f"the primary was killed. detector={detector.status(DEPENDENCY)} "
                f"last_outcome={outcome.as_dict() if outcome else None}")
        if outcome.stage is not FailoverStage.REDIRECTED:
            raise DrillError(f"the sequence stopped at {outcome.stage.value}")

        # ── THE MEASUREMENT: a client write through the unchanged address ──
        t_write = None
        while time.monotonic() < deadline:
            if await _client_write(
                    router.port,
                    "INSERT INTO failover_probe (n) VALUES (999999)",
                    timeout=PROBE_TIMEOUT):
                t_write = time.monotonic()
                break
            await asyncio.sleep(0.02)
        if t_write is None:
            raise DrillError(
                "the failover completed but no client could write through the "
                "router. Redirection to a node that does not take writes is "
                "the failure this drill exists to catch.")

        rto = t_write - t_kill

        # ── what survived ──
        #
        # Counted ONLY among acknowledged rows, and that bound is not tidiness.
        # The first version counted every row below the sentinel and reported
        # `rpo_committed_rows_lost: -1` — more rows survived than the client was
        # ever told were committed.
        #
        # That is a real phenomenon, not an arithmetic slip: the writer's last
        # INSERT reached the server, was committed and replicated, and the
        # postmaster died before the acknowledgement got back. The row is
        # durable and the client never learned it. That is the opposite of data
        # loss, and folding it into the loss count made a durable write cancel
        # out a lost one.
        #
        # So the RPO counts promises broken — rows at or below the last
        # acknowledgement that are gone — and the unacknowledged-but-durable
        # rows are reported separately rather than netted off.
        survived = await _count(cluster.standby_port, at_most=committed)
        beyond_ack = await _count(cluster.standby_port, above=committed)
        lost = committed - survived
        if lost < 0:
            raise DrillError(
                f"survived ({survived}) exceeds acknowledged ({committed}) even "
                f"after bounding the count. The accounting is wrong and no RPO "
                f"can be reported from it.")

        return RunResult(
            run=run, rto_seconds=rto,
            time_to_detect_seconds=(t_declared - t_kill) if t_declared else None,
            rows_committed=committed,
            rows_survived=survived, rows_lost=lost,
            unacknowledged_but_durable=beyond_ack,
            detection_observations=observations,
            orchestrator=outcome.as_dict())
    finally:
        if router is not None:
            router.stop()
        cluster.stop_all()


# ── Entry point ─────────────────────────────────────────────────────────────

async def main_async(runs: int, out: Optional[Path], lag_rows: int) -> int:
    results: list[RunResult] = []
    for run in range(1, runs + 1):
        # A FRESH cluster per run, not a rebuilt one. After a promotion the
        # pair has diverged, and a "rebuild" that reuses the old primary's data
        # directory measures a recovery path nobody deploys.
        with tempfile.TemporaryDirectory(prefix=f"ira-drill-{run}-") as tmp:
            root = Path(tmp)
            # The postgres binaries refuse to run as root, so when this runs as
            # root the cluster is built by an unprivileged `postgres` (see
            # `_as_postgres`) and that user has to be able to write here.
            #
            # GIVE IT THE DIRECTORY rather than making the directory
            # world-writable. The first version used `chmod 0o777`, which
            # bandit flagged (B103) and was right to: a world-writable
            # directory in a predictable location is a place any local user can
            # drop files into a privileged process's working tree. Ownership
            # grants exactly the one account that needs it.
            if os.geteuid() == 0:
                shutil.chown(root, "postgres", "postgres")
            print(f"── run {run}/{runs} ──", flush=True)
            result = await one_run(run, root, lag_rows=lag_rows)
            results.append(result)
            print(json.dumps(result.as_dict(), indent=2), flush=True)

    rtos = [r.rto_seconds for r in results]
    lost = [r.rows_lost for r in results]
    detects = [r.time_to_detect_seconds for r in results
               if r.time_to_detect_seconds is not None]
    summary: dict[str, Any] = {
        "runs": len(results),
        "time_to_detect_seconds": ({
            "min": round(min(detects), 3),
            "median": round(statistics.median(detects), 3),
            "max": round(max(detects), 3),
        } if detects else None),
        "rto_seconds": {
            "min": round(min(rtos), 3),
            "median": round(statistics.median(rtos), 3),
            "max": round(max(rtos), 3),
        },
        "rpo_committed_rows_lost": {
            "min": min(lost), "median": statistics.median(lost), "max": max(lost),
        },
        "detection_threshold_observations": FAILURE_THRESHOLD,
        "measured": [r.as_dict() for r in results],
    }

    print("\n" + "=" * 70)
    print("ENGINEERING RTO — kill to a client write through the unchanged "
          "address")
    print(f"  runs         {summary['runs']}")
    print(f"  min          {summary['rto_seconds']['min']}s")
    print(f"  median       {summary['rto_seconds']['median']}s")
    print(f"  max          {summary['rto_seconds']['max']}s")
    if detects:
        print("TIME TO DETECT — kill to the detector DECLARING it failed")
        print(f"  min/median/max  {round(min(detects), 3)}s / "
              f"{round(statistics.median(detects), 3)}s / "
              f"{round(max(detects), 3)}s")
    print("OBSERVED DATA-LOSS WINDOW — acknowledged rows that did not survive")
    print(f"  min/median/max  {min(lost)} / {statistics.median(lost)} / {max(lost)}")
    unacked = [r.unacknowledged_but_durable for r in results]
    print(f"  (also: {min(unacked)}–{max(unacked)} rows per run were made "
          f"durable and never")
    print("   acknowledged — the server died between the commit and the reply.")
    print("   That is not loss and is deliberately not netted off the figure")
    print("   above.)")
    print("=" * 70)
    print("This is an ENGINEERING measurement of this topology on one host. It")
    print("is NOT the commercial RTO/RPO target, which is the owner's. Loopback")
    print("has no network, and async replication's data-loss window is a")
    print("function of link latency and write rate — a zero here means this")
    print("mechanism did not lose data under these conditions, not that it")
    print("cannot.")

    if out is not None:
        out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3,
                        help="how many times to run the whole sequence")
    parser.add_argument("--out", type=Path, default=None,
                        help="write the JSON summary here")
    parser.add_argument(
        "--induce-replication-lag", type=int, default=0, metavar="ROWS",
        help="FOR THE SELF-TEST. Stop the standby's WAL receiver and commit "
             "this many more rows before the kill, so the drill has to report "
             "a NON-ZERO data-loss window. Proves the RPO figure is derived "
             "from what survived rather than being a constant.")
    args = parser.parse_args()

    if os.environ.get("IRA_TEST_ALLOW_DESTRUCTIVE") != "1":
        print("refusing: this SIGKILLs a postmaster and promotes a standby.\n"
              "set IRA_TEST_ALLOW_DESTRUCTIVE=1 if that is acceptable here.",
              file=sys.stderr)
        return 2
    if args.runs < 1:
        print("--runs must be at least 1", file=sys.stderr)
        return 2

    try:
        return asyncio.run(main_async(args.runs, args.out,
                                      args.induce_replication_lag))
    except DrillError as exc:
        # UNKNOWN, never a good number. A drill that could not be set up has
        # measured nothing, and printing a plausible RTO anyway is the exact
        # failure class this programme has spent six batches removing.
        print(f"\n::error::the drill could not complete: {exc}\n"
              f"No RTO is reported. A drill that did not run measures nothing; "
              f"it does not measure zero.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
