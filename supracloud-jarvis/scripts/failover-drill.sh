#!/usr/bin/env bash
#
# Blocker B4 / readiness R07 — a real database failover drill.
#
# Builds a streaming standby from a running primary, commits a known number of
# transactions, **hard-kills the primary**, promotes the standby, and reports
# what it cost: how long until the promoted node accepted a write (RTO) and how
# many committed rows did not survive (RPO).
#
# ── Why this exists as a script and not only as a test ───────────────────────
#
# R07 read `not-verified` with the gap "no replica, no WAL archiving, and no
# failover mechanism". A test could not fix that, because there was nothing to
# fail over TO. This builds the replica, so the mechanism exists and can be
# exercised on demand by anybody, on any machine, without waiting for CI.
#
# ── This is DESTRUCTIVE. Read before running ─────────────────────────────────
#
#   * It SIGKILLs the primary postmaster. Every open connection dies.
#   * It promotes the standby, after which primary and standby have DIVERGED
#     and the standby must be rebuilt from scratch before the pair is usable
#     again — `--rebuild` does that.
#   * Run it against a database you can afford to lose. Not a shared one, and
#     not production.
#
# It refuses to run without IRA_TEST_ALLOW_DESTRUCTIVE=1 for that reason.
#
# ── What the numbers do and do not mean ──────────────────────────────────────
#
# The RTO here is **PostgreSQL's promotion time only**. A real recovery time
# also includes detecting the failure and pointing clients at the new primary —
# in most real outages those dominate, and promotion is the fast part.
#
# When this was written neither existed. **Detection now does**: P14-02 added
# `core/runtime/failure_detector.py`, which the readiness path feeds on every
# evaluation, and `/readyz?verbose=true` reports each dependency's state and how
# long it has been failing. Its `threshold_detection_ms` is the interval between
# the first failing PROBE and the declaration — not a time-to-detect, because it
# excludes everything before that probe and is bounded below by the probe
# interval. Do not add it to the RTO below and call the sum a recovery time.
#
# **Pointing clients at a different backend now exists too**, as of P15-04:
# `infra/terraform/redirection/` puts an HAProxy router in front of two
# PostgreSQL instances, and a CI job kills one and proves a new client lands on
# the other with nothing about the client changed. `utils/db.py::init_pool`
# still reads its DSN once at startup — the point is that it no longer has to
# re-read it, because the address is stable and what is behind it moves.
#
# **The number this script prints is still promotion time alone**, and that has
# not changed — but the reason has. It used to be that nothing composed the
# three mechanisms. **P14-R2 composes them**, in
# `ira/scripts/failover_e2e_drill.py`:
#
#   * it builds this script's topology — a primary and a real streaming standby
#     — BEHIND a router, so the redirection is to a promoted primary rather than
#     to an independent second instance;
#   * `core/runtime/failover_orchestrator.py` consumes the detector. A
#     declaration now causes a promotion, and the router is repointed only after
#     the promotion is VERIFIED — HAProxy's own health check is deliberately not
#     allowed to move traffic, because it would move it to a node still in
#     recovery, which serves reads and refuses writes;
#   * it measures from the SIGKILL to a client completing a WRITE through the
#     unchanged address, repeatedly.
#
# Measured there: **1.39-1.53 s over 10 runs**, of which ~1.21 s is detection.
# **0 acknowledged rows lost** in every run, and the self-test proves that zero
# can be non-zero by breaking replication first.
#
# So: read THAT number as the engineering RTO, and this one as what it says it
# is. This script still measures promotion alone, which is the fast part.
#
# The RPO here is measured over a **loopback pair on one machine**. Async
# replication has a real, non-zero RPO whenever the network has latency; a
# measurement taken with no network says little about one that has one. Read a
# zero here as "the mechanism did not lose data under these conditions", never
# as "this deployment cannot lose data".
#
# Usage:
#   IRA_TEST_ALLOW_DESTRUCTIVE=1 ./failover-drill.sh --build     # make a standby
#   IRA_TEST_ALLOW_DESTRUCTIVE=1 ./failover-drill.sh --drill     # fail over
#   IRA_TEST_ALLOW_DESTRUCTIVE=1 ./failover-drill.sh --rebuild   # restore the pair

set -uo pipefail

PRIMARY_PORT="${IRA_PRIMARY_PORT:-5432}"
STANDBY_PORT="${IRA_STANDBY_PORT:-5433}"
PRIMARY_DIR="${IRA_PRIMARY_DIR:-/var/lib/postgresql/iratest}"
STANDBY_DIR="${IRA_STANDBY_DIR:-/var/lib/postgresql/irastandby}"
PGBIN="${IRA_PGBIN:-/usr/lib/postgresql/16/bin}"
ROWS="${IRA_DRILL_ROWS:-400}"

if [[ "${IRA_TEST_ALLOW_DESTRUCTIVE:-}" != "1" ]]; then
  echo "refusing: this SIGKILLs a PostgreSQL primary and promotes a standby." >&2
  echo "set IRA_TEST_ALLOW_DESTRUCTIVE=1 if this database is disposable." >&2
  exit 2
fi

as_postgres() { su postgres -c "$1"; }

wait_for() {  # port, seconds
  local port=$1 limit=${2:-30} i
  for ((i = 0; i < limit * 2; i++)); do
    psql -U postgres -p "$port" -tAc "SELECT 1" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  return 1
}

build_standby() {
  as_postgres "$PGBIN/pg_ctl -D $STANDBY_DIR -m immediate stop" >/dev/null 2>&1
  rm -rf "$STANDBY_DIR"
  # -R writes standby.signal and primary_conninfo; -X stream keeps the WAL
  # flowing during the basebackup so the standby starts already caught up.
  as_postgres "pg_basebackup -h 127.0.0.1 -p $PRIMARY_PORT -U postgres \
               -D $STANDBY_DIR -R -X stream -c fast" >/dev/null 2>&1 || {
    echo "pg_basebackup failed — is the primary up and does pg_hba allow replication?" >&2
    exit 1
  }
  as_postgres "$PGBIN/pg_ctl -D $STANDBY_DIR -l /tmp/pg-standby.log -o '-p $STANDBY_PORT' start" >/dev/null 2>&1
  wait_for "$STANDBY_PORT" 30 || { echo "standby did not start" >&2; exit 1; }

  local recovering streaming
  recovering=$(psql -U postgres -p "$STANDBY_PORT" -tAc "SELECT pg_is_in_recovery()")
  streaming=$(psql -U postgres -p "$PRIMARY_PORT" -tAc \
    "SELECT state FROM pg_stat_replication LIMIT 1")
  echo "standby_in_recovery=$recovering"
  echo "replication_state=${streaming:-NONE}"
  [[ "$recovering" == "t" && "$streaming" == "streaming" ]] || {
    echo "the pair is not actually replicating — drill would prove nothing" >&2
    exit 1
  }
}

drill() {
  psql -U postgres -p "$PRIMARY_PORT" -q \
    -c "DROP TABLE IF EXISTS failover_probe" \
    -c "CREATE TABLE failover_probe (n bigint primary key)" >/dev/null 2>&1

  # Separate transactions, so "committed" is unambiguous: each row that comes
  # back is a promise PostgreSQL made to a client.
  local committed=0 i
  for ((i = 1; i <= ROWS; i++)); do
    psql -U postgres -p "$PRIMARY_PORT" -q -tAc \
      "INSERT INTO failover_probe (n) VALUES ($i)" >/dev/null 2>&1 || break
    committed=$i
  done
  echo "committed_on_primary=$committed"

  # SIGKILL, not a clean shutdown. A clean stop flushes and ships everything,
  # which is a maintenance event rather than a failure — it would measure
  # nothing.
  local pgpid
  pgpid=$(head -1 "$PRIMARY_DIR/postmaster.pid")
  kill -9 "$pgpid" 2>/dev/null
  sleep 2
  if psql -U postgres -p "$PRIMARY_PORT" -tAc "SELECT 1" >/dev/null 2>&1; then
    echo "the primary is still accepting connections — drill invalid" >&2
    exit 1
  fi
  echo "primary_killed=yes"

  # Timed to the first successful WRITE, not to "the process started". A
  # standby that is up but still read-only has not recovered anything.
  local start end rto
  start=$(date +%s.%N)
  as_postgres "$PGBIN/pg_ctl -D $STANDBY_DIR promote" >/dev/null 2>&1
  end=""
  for ((i = 0; i < 300; i++)); do
    if psql -U postgres -p "$STANDBY_PORT" -q -c \
         "INSERT INTO failover_probe (n) VALUES (999999)" >/dev/null 2>&1; then
      end=$(date +%s.%N); break
    fi
    sleep 0.1
  done
  [[ -n "$end" ]] || { echo "standby never accepted a write after promote" >&2; exit 1; }
  rto=$(echo "$end - $start" | bc)

  local survived lost
  survived=$(psql -U postgres -p "$STANDBY_PORT" -tAc \
    "SELECT COUNT(*) FROM failover_probe WHERE n < 999999")
  lost=$((committed - survived))

  echo "rto_seconds_promotion_only=$rto"
  echo "rows_committed=$committed"
  echo "rows_survived=$survived"
  echo "rpo_committed_rows_lost=$lost"
  echo "standby_now_primary=$(psql -U postgres -p "$STANDBY_PORT" -tAc 'SELECT NOT pg_is_in_recovery()')"

  if (( lost > 0 )); then
    echo
    echo "NOTE: $lost committed transaction(s) did not survive the failover."
    echo "That is async replication behaving as designed, not a bug — and it is"
    echo "exactly the number an RPO target has to be set against."
  fi
}

rebuild() {
  as_postgres "$PGBIN/pg_ctl -D $STANDBY_DIR -m immediate stop" >/dev/null 2>&1
  rm -rf "$STANDBY_DIR"
  as_postgres "$PGBIN/pg_ctl -D $PRIMARY_DIR -l /tmp/pg-primary.log -o '-p $PRIMARY_PORT' start" >/dev/null 2>&1
  wait_for "$PRIMARY_PORT" 60 || { echo "primary did not come back" >&2; exit 1; }
  build_standby
  echo "pair_restored=yes"
}

case "${1:---drill}" in
  --build)   build_standby ;;
  --drill)   drill ;;
  --rebuild) rebuild ;;
  *) echo "usage: $0 [--build|--drill|--rebuild]" >&2; exit 2 ;;
esac
