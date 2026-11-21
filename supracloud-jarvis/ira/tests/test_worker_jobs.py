"""The worker runs only the jobs ``IRA_WORKER_JOBS`` names (``worker/jobs.py``).

It used to register every job it knew, so starting it at all meant starting
the model-calling jobs, the IP blocker and the 90-day Memory Vault purge too.
And because jobs were stored in PostgreSQL, a job switched off still ran: a
stored job is reloaded whether or not this start registered it.

The first group needs no APScheduler, so it runs in CI and in the pre-push
hook, which do not install it. The second group builds the real scheduler and
is skipped where APScheduler is absent; its results are recorded in
``docs/testing/evidence/worker-job-allowlist-2026-10-23/``.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from worker.jobs import DEFAULT_JOBS, KNOWN_JOBS, enabled_jobs

_IRA = Path(__file__).resolve().parents[1]
_ROOT = _IRA.parent
_SCHEDULER = _IRA / "worker" / "scheduler.py"


# ── Parsing IRA_WORKER_JOBS ──────────────────────────────────────────────────

def test_unset_or_empty_means_the_default(monkeypatch):
    monkeypatch.delenv("IRA_WORKER_JOBS", raising=False)
    assert enabled_jobs() == DEFAULT_JOBS
    assert enabled_jobs("") == DEFAULT_JOBS
    assert enabled_jobs("   ") == DEFAULT_JOBS


def test_none_and_all(monkeypatch):
    assert enabled_jobs("none") == frozenset()
    assert enabled_jobs(" NONE ") == frozenset()
    assert enabled_jobs("all") == KNOWN_JOBS
    monkeypatch.setenv("IRA_WORKER_JOBS", "none")
    assert enabled_jobs() == frozenset()


def test_a_list_is_exactly_what_it_names(monkeypatch):
    assert enabled_jobs(" Reminder_Check , heartbeat,, ") == {"reminder_check", "heartbeat"}
    monkeypatch.setenv("IRA_WORKER_JOBS", "memory_retention")
    assert enabled_jobs() == {"memory_retention"}


def test_a_misspelt_job_is_refused_not_ignored():
    # Ignored, it would switch reminder_check off without a word.
    with pytest.raises(ValueError, match="remindr_check") as err:
        enabled_jobs("remindr_check,heartbeat")
    assert "reminder_check" in str(err.value)  # the known names are listed


def test_the_default_calls_no_model_and_deletes_no_memory():
    # Changing this set changes what every install runs: it is the owner's
    # decision (docs/operations/WORKER_JOBS_2026-10-23.md), so it is pinned.
    assert DEFAULT_JOBS == {"reminder_check", "model_perf_retention", "token_version_retention"}
    assert DEFAULT_JOBS <= KNOWN_JOBS


# ── worker/scheduler.py registers nothing around the allowlist ───────────────

def _build_scheduler_calls():
    tree = ast.parse(_SCHEDULER.read_text(encoding="utf-8"))
    build = next(n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "build_scheduler")
    calls = [n for n in ast.walk(build) if isinstance(n, ast.Call)]
    return build, calls


def test_every_job_goes_through_the_allowlist():
    build, calls = _build_scheduler_calls()
    via_add, direct = [], []
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        if isinstance(call.func, ast.Name) and call.func.id == "add":
            assert isinstance(kw.get("id"), ast.Constant), ast.unparse(call)
            via_add.append(kw["id"].value)
        elif isinstance(call.func, ast.Attribute) and call.func.attr == "add_job":
            direct.append(ast.unparse(kw.get("id", ast.Constant(None))))
    # Only two add_job calls bypass add(): add()'s own, and the liveness tick.
    assert sorted(direct) == ["LIVENESS_JOB_ID", "id"], direct
    assert len(via_add) == len(set(via_add)), "a job id is registered twice"
    # heartbeat and weekly_reflection register through their own modules.
    assert set(via_add) | {"heartbeat", "weekly_reflection"} == KNOWN_JOBS


def test_heartbeat_and_reflection_register_only_when_named():
    source = _SCHEDULER.read_text(encoding="utf-8")
    assert re.search(r'if "heartbeat" in jobs:\s*\n\s*register_heartbeat\(scheduler\)', source)
    assert re.search(r'if "weekly_reflection" in jobs:\s*\n\s*register_reflection\(scheduler\)', source)
    # ... and the ids their modules register are the names checked here.
    assert 'id="heartbeat"' in (_IRA / "worker" / "heartbeat.py").read_text(encoding="utf-8")
    assert 'id="weekly_reflection"' in (_IRA / "worker" / "reflection.py").read_text(encoding="utf-8")


def test_jobs_are_not_persisted():
    # A persisted job is reloaded at start whether or not it was registered, so
    # switching it off did not stop it; and the table needs CREATE on public,
    # which 042 revokes.
    source = _SCHEDULER.read_text(encoding="utf-8")
    assert "SQLAlchemyJobStore" not in source
    assert '"default": MemoryJobStore()' in source


# ── The launchers start and stop the worker ──────────────────────────────────

def test_start_ira_starts_the_worker_unless_none():
    text = (_ROOT / "start-ira.sh").read_text(encoding="utf-8")
    api = text.index("# ── 5. IRA backend")
    section = text[text.index("# ── 5b. Background worker"):text.index("# ── 6. IRA frontend")]
    assert text.index("# ── 5b. Background worker") > api   # after migrations and the API
    assert '"$PY" -m worker.main' in section
    assert '"$RUN_DIR/worker.pid"' in section
    assert "IRA_WORKER_JOBS" in section and '== "none"' in section
    # It reports what it switched on, and does not fail the start if it cannot.
    assert "Worker jobs on:" in section and 'fail "worker"' not in section


def test_stop_ira_stops_the_worker():
    text = (_ROOT / "stop-ira.sh").read_text(encoding="utf-8")
    assert re.search(r'^stop_pidfile "worker"\s+"\$RUN_DIR/worker\.pid"', text, re.M)


def test_env_example_documents_the_setting():
    text = (_ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^#?\s*IRA_WORKER_JOBS=", text, re.M)


# ── The real scheduler (skipped where APScheduler is not installed) ──────────

@pytest.fixture
def scheduler_module(monkeypatch):
    pytest.importorskip("apscheduler")
    import worker.scheduler as mod
    return mod


def _ids(scheduler) -> set[str]:
    return {job.id for job in scheduler.get_jobs()}


def test_none_registers_only_the_liveness_tick(scheduler_module):
    s = scheduler_module.build_scheduler(frozenset())
    assert _ids(s) == {scheduler_module.LIVENESS_JOB_ID}
    assert scheduler_module.LIVENESS_JOB_ID not in KNOWN_JOBS


def test_the_default_registers_the_default(scheduler_module, monkeypatch):
    monkeypatch.delenv("IRA_WORKER_JOBS", raising=False)
    s = scheduler_module.build_scheduler()
    assert _ids(s) == DEFAULT_JOBS | {scheduler_module.LIVENESS_JOB_ID}


def test_all_registers_every_known_job(scheduler_module, monkeypatch):
    monkeypatch.setenv("IRA_HEARTBEAT", "true")
    monkeypatch.setenv("IRA_REFLECTION", "true")
    s = scheduler_module.build_scheduler(KNOWN_JOBS)
    assert _ids(s) == KNOWN_JOBS | {scheduler_module.LIVENESS_JOB_ID}


def test_heartbeat_named_but_switched_off_stays_off(scheduler_module, monkeypatch):
    monkeypatch.setenv("IRA_HEARTBEAT", "false")
    s = scheduler_module.build_scheduler(frozenset({"heartbeat", "reminder_check"}))
    assert _ids(s) == {"reminder_check", scheduler_module.LIVENESS_JOB_ID}


def test_an_unclassified_job_stops_the_start(scheduler_module, monkeypatch):
    # A job added to scheduler.py but not to KNOWN_JOBS could never be named,
    # so it would never run, silently. The start refuses instead.
    monkeypatch.setattr(scheduler_module, "KNOWN_JOBS", KNOWN_JOBS - {"calendar_sync"})
    with pytest.raises(ValueError, match="calendar_sync"):
        scheduler_module.build_scheduler(frozenset())


def test_the_liveness_tick_writes_the_healthcheck_file(scheduler_module, monkeypatch, tmp_path):
    tick = tmp_path / "ira_worker_tick"
    monkeypatch.setattr(scheduler_module, "WORKER_HEARTBEAT_FILE", str(tick))
    asyncio.run(scheduler_module._liveness_tick())
    assert tick.read_text(encoding="utf-8").startswith("20")


def test_the_start_announcement_lists_what_is_on(scheduler_module, monkeypatch):
    # It used to promise threat alerts and an 08:00 briefing whatever ran.
    monkeypatch.setattr(scheduler_module, "_scheduler",
                        scheduler_module.build_scheduler(frozenset({"reminder_check", "calendar_sync"})))
    assert scheduler_module.running_jobs() == ["calendar_sync", "reminder_check"]
    main_source = (_IRA / "worker" / "main.py").read_text(encoding="utf-8")
    assert "jobs = running_jobs()" in main_source and "Jobs on: {" in main_source
    assert "morning briefing at 08:00" not in main_source


# ── Regressions found in review ──────────────────────────────────────────────

@pytest.mark.parametrize("value", [",", " , ", ",,"])
def test_a_list_that_names_nothing_is_refused(value):
    # Not empty, so not the default, and not "none": it names no job. Taken as
    # an empty list it ran nothing without a word, which is what a misspelt
    # name is refused for.
    with pytest.raises(ValueError, match="names no job"):
        enabled_jobs(value)



def _start_section() -> str:
    """start-ira.sh's helpers and its worker section, runnable on their own."""
    text = (_ROOT / "start-ira.sh").read_text(encoding="utf-8")
    match = re.search(r"^wait_until\(\) \{.*?^\}\n\nenv_or\(\) \{.*?^\}\n", text, re.M | re.S)
    assert match, "start-ira.sh no longer defines wait_until() followed by env_or()"
    helpers = match.group(0)
    section = text[text.index("# ── 5b. Background worker"):text.index("# ── 6. IRA frontend")]
    return ("set -uo pipefail\nok() { echo \"OK $1 $2\"; }\nwarn() { echo \"WARN $1 $2\"; }\n"
            'ROOT="$1"; IRA_DIR="$ROOT/ira"; RUN_DIR="$ROOT/.ira-run"; PY="$2"\n'
            + helpers + section)


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash") or not shutil.which("ps"),
                    reason="needs bash and ps")
def test_a_stale_pid_file_naming_another_process_does_not_count_as_the_worker(tmp_path):
    # A PID file left by a crash or a reboot can name a process that is not the
    # worker. kill -0 alone took it as the worker: the start reported the old
    # log's job list and started nothing, and stop-ira.sh then killed that
    # other process.
    root = tmp_path / "root"
    (root / "ira" / "worker").mkdir(parents=True)
    (root / "ira" / "worker" / "__init__.py").write_text("", encoding="utf-8")
    (root / "ira" / "worker" / "main.py").write_text(
        "import sys, time\nprint('Worker jobs on: reminder_check. Off: none.', flush=True)\n"
        "time.sleep(60)\n", encoding="utf-8")
    run_dir = root / ".ira-run"
    run_dir.mkdir()
    (run_dir / "worker.log").write_text("Worker jobs on: from_an_old_run. Off: none.\n", encoding="utf-8")
    script = tmp_path / "start5b.sh"
    script.write_text(_start_section(), encoding="utf-8")

    other = subprocess.Popen(["sleep", "60"])
    worker_pid = None
    try:
        (run_dir / "worker.pid").write_text(f"{other.pid}\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "IRA_WORKER_JOBS"}
        out = subprocess.run(["bash", str(script), str(root), sys.executable], env=env,
                             capture_output=True, text=True, timeout=60).stdout
        worker_pid = int((run_dir / "worker.pid").read_text(encoding="utf-8"))
        assert worker_pid != other.pid, out          # a worker was started
        assert "OK worker jobs: reminder_check" in out, out
        assert "from_an_old_run" not in out, out
        assert other.poll() is None                  # the other process is untouched
    finally:
        other.kill()
        if worker_pid and worker_pid != other.pid:
            try:
                os.killpg(worker_pid, signal.SIGKILL)
            except OSError:
                pass
