"""P2-06: start-ira.sh's Ollama warm-up no longer cancels a slow first load.

Found on 1 November 2026 by a clean-install rehearsal of main @ f81e422.
start-ira.sh warmed each model with

    curl -sf --max-time 120 -X POST "$OLLAMA_BASE/api/generate" ... >/dev/null 2>&1 || true

After a reboot, the first load of qwen3:4b (2.5 GB) took more than 120 s on a
4-vCPU machine with no GPU. curl gave up, Ollama logged "client connection
closed before llama-server finished loading, aborting load", and the script
still printed [OK] ollama. It warmed the fast and the deep tier each, so with
both set to one model (what setup writes) it loaded that model twice; that
second call is the only reason the rehearsal recovered.

These tests run start-ira.sh's own Ollama section, with its own helpers,
against a stand-in for Ollama: a stdlib HTTP server on a free loopback port
that lists the models, takes a set time to "load" one, and records whether the
client hung up before the load finished. The section's waits are rewritten to
a few seconds before it runs. No real Ollama is started, stopped or asked to
load anything: the stand-in lists every model, so the section never calls
`ollama`, and a stub first on PATH would record it if it did.

Evidence: supracloud-jarvis/docs/testing/evidence/start-warmup-timeout-2026-11-01/.

P2-07, found by the final clean install of 1 November 2026: the warm-up sent
`"prompt":"ok"`, so after the load the model also wrote a reply. On the
install's second start, with the model already loaded, that reply alone held
the start for about a minute on 4 vCPUs. Ollama loads a model and writes
nothing when /api/generate has no prompt, or an empty one, and answers
{"done":true,"done_reason":"load"} (docs/api.md "Load a model", and
server/routes.go GenerateHandler at v0.40.13). The stand-in now answers as
Ollama does: load only for a request with no prompt, a reply for one with a
prompt, and it records which. Evidence:
supracloud-jarvis/docs/testing/evidence/warmup-load-only-2026-11-01/.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2]          # supracloud-jarvis/
START_SH = APP_ROOT / "start-ira.sh"
START_PS1 = APP_ROOT / "start-ira.ps1"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash") or not shutil.which("curl"),
    reason="needs bash and curl")

# main's Ollama section as it was until this fix (f81e422, unchanged since the
# warm-up line arrived in 216c438d), verbatim. The control test runs it on the
# same stand-in, to show the stand-in sees the cancellation these tests rule out.
MAIN_SECTION = r'''# ── 3. Ollama (serve, pull + warm the fast/deep tiers) ────────────────────────
OLLAMA_BASE="${OLLAMA_BASE_URL:-http://localhost:11434/v1}"; OLLAMA_BASE="${OLLAMA_BASE%/v1}"
KEEP_ALIVE=$(env_or OLLAMA_KEEP_ALIVE 30m)
FAST_MODEL=$(env_or OLLAMA_MODEL_FAST qwen3:8b)
DEEP_MODEL=$(env_or OLLAMA_MODEL_DEEP qwen3:14b)
if ! http_up "$OLLAMA_BASE/api/tags"; then
    if command -v ollama >/dev/null 2>&1; then
        OLLAMA_KEEP_ALIVE="$KEEP_ALIVE" nohup ollama serve > "$RUN_DIR/ollama.log" 2>&1 &
        echo $! > "$RUN_DIR/ollama.pid"
    fi
fi
if wait_until "http_up $OLLAMA_BASE/api/tags" 30; then
    for m in "$FAST_MODEL" "$DEEP_MODEL"; do
        if ! curl -sf "$OLLAMA_BASE/api/tags" | grep -q "\"$m\""; then
            echo "         ollama pull $m ... (first time only, this can take a while)"
            ollama pull "$m" 2>/dev/null || warn "ollama" "could not pull $m — pull it manually"
        fi
        # warm the model so the first chat isn't cold (best effort)
        curl -sf --max-time 120 -X POST "$OLLAMA_BASE/api/generate" \
            -H "Content-Type: application/json" \
            -d "{\"model\":\"$m\",\"prompt\":\"ok\",\"stream\":false,\"keep_alive\":\"$KEEP_ALIVE\"}" >/dev/null 2>&1 || true
    done
    ok "ollama" "$OLLAMA_BASE ($FAST_MODEL + $DEEP_MODEL, keep_alive=$KEEP_ALIVE)"
    # A model bigger than this machine's memory loads slowly or not at all.
    for m in "$FAST_MODEL" "$DEEP_MODEL"; do
        _fit="$("$PY" "$ROOT/scripts/detect_hardware.py" --check "$m" 2>/dev/null)" || warn "ollama" "$_fit"
    done
else
    fail "ollama" "no response at $OLLAMA_BASE — install from https://ollama.com and run: ollama serve"
fi

'''


# ── A stand-in for Ollama ────────────────────────────────────────────────────


class StandIn(ThreadingHTTPServer):
    """Answers /api/tags and /api/generate as Ollama does, for the warm-up.

    A model's first /api/generate "loads" it for `load_s` seconds (one number,
    or one per model), a later attempt after a cancelled one for `reload_s`
    (the page cache makes the second read faster). If the client closes the connection before the load
    is over, the load is cancelled, as Ollama does ("client connection closed
    before llama-server finished loading, aborting load"), and the model stays
    unloaded. `errors` maps a model to the error Ollama answers 500 with, or to
    None to close the connection with no answer at all (Ollama exiting).

    Once the model is loaded (or was, in `loaded`), a request with no prompt or
    an empty one is answered at once with done_reason "load", as Ollama does,
    and `answers` records "load only". A request with a prompt gets a reply
    that takes `reply_s` to write: `answers` records "reply" when it starts and
    "reply written" when it is sent; a client that hangs up cancels it.
    """

    daemon_threads = True

    def __init__(self, models: list[str], load_s: float | dict[str, float],
                 reload_s: float | None = None, errors: dict[str, str | None] | None = None,
                 loaded: list[str] | None = None, reply_s: float = 0.0) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.models = models
        self.load_s = load_s
        self.reload_s = reload_s
        self.errors = errors or {}
        self.reply_s = reply_s
        self.requests: list[str] = []                    # /api/generate, in order
        self.bodies: list[dict] = []                     # their JSON bodies, in order
        self.outcomes: list[tuple[str, str]] = []        # (model, loaded | cancelled | ...)
        self.answers: list[tuple[str, str]] = []         # (model, load only | reply | reply written)
        self._loaded: set[str] = set(loaded or [])
        self._tried: set[str] = set()
        self.changed = threading.Condition()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def load_time(self, model: str, retry: bool) -> float:
        if retry and self.reload_s is not None:
            return self.reload_s
        return self.load_s[model] if isinstance(self.load_s, dict) else self.load_s

    def note(self, kind: str, model: str, outcome: str = "") -> None:
        with self.changed:
            if kind == "request":
                self.requests.append(model)
            elif kind == "answer":
                self.answers.append((model, outcome))
            else:
                self.outcomes.append((model, outcome))
            self.changed.notify_all()

    def wait_for(self, predicate: Callable[[], bool], timeout: float) -> bool:
        with self.changed:
            return self.changed.wait_for(predicate, timeout)

    def handle_error(self, request: object, client_address: object) -> None:
        pass   # a client that hung up mid-reply is what some tests are about


class _Handler(BaseHTTPRequestHandler):
    server: StandIn

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - BaseHTTPRequestHandler's name
        pass

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()   # as Ollama (Go) writes it
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        if self.path == "/api/tags":
            self._reply(200, {"models": [{"name": m, "model": m} for m in self.server.models]})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        if self.path != "/api/generate":
            self._reply(404, {"error": "not found"})
            return
        srv = self.server
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        model = body["model"]
        with srv.changed:
            srv.bodies.append(body)
        srv.note("request", model)
        if model in srv.errors:
            if srv.errors[model] is None:
                srv.note("outcome", model, "no answer")
                self.close_connection = True
                return
            srv.note("outcome", model, "error")
            self._reply(500, {"error": srv.errors[model]})
            return
        with srv.changed:
            loaded = model in srv._loaded
            load_s = srv.load_time(model, retry=model in srv._tried)
            srv._tried.add(model)
        if not loaded:
            if self._client_hung_up_within(load_s):
                srv.note("outcome", model, "cancelled")
                self.close_connection = True
                return
            with srv.changed:
                srv._loaded.add(model)
            srv.note("outcome", model, "loaded")
        else:
            srv.note("outcome", model, "already loaded")
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if not body.get("prompt"):
            # Ollama's GenerateHandler: `if req.Prompt == ""` after the load.
            srv.note("answer", model, "load only")
            self._reply(200, {"model": model, "created_at": created_at, "response": "", "done": True,
                              "done_reason": "load"})
            return
        srv.note("answer", model, "reply")
        if self._client_hung_up_within(srv.reply_s):
            self.close_connection = True
            return
        srv.note("answer", model, "reply written")
        self._reply(200, {"model": model, "created_at": created_at, "response": "ok", "done": True,
                          "done_reason": "stop"})

    def _client_hung_up_within(self, seconds: float) -> bool:
        """Sleep through the load, watching the connection. The request has
        been read in full, so the socket turns readable only when the client
        closes it."""
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            readable, _, _ = select.select([self.connection], [], [], min(left, 0.05))
            if readable:
                try:
                    if not self.connection.recv(1, socket.MSG_PEEK):
                        return True
                except OSError:
                    return True
        return False


@pytest.fixture
def standin() -> Iterator[Callable[..., StandIn]]:
    servers: list[StandIn] = []

    def start(models: list[str], load_s: float | dict[str, float], reload_s: float | None = None,
              errors: dict[str, str | None] | None = None, loaded: list[str] | None = None,
              reply_s: float = 0.0) -> StandIn:
        srv = StandIn(models, load_s, reload_s, errors, loaded, reply_s)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


# ── Running start-ira.sh's Ollama section ────────────────────────────────────


def _helpers(text: str) -> str:
    http_up = re.search(r"^http_up\(\) \{.*\}$", text, re.M)
    rest = re.search(r"^wait_until\(\) \{.*?^\}\n\nenv_or\(\) \{.*?^\}\n", text, re.M | re.S)
    assert http_up and rest, "start-ira.sh no longer defines http_up, wait_until and env_or as read here"
    return http_up.group(0) + "\n" + rest.group(0)


def _ollama_section(text: str) -> str:
    return text[text.index("# ── 3. Ollama"):text.index("# ── 4. Web-research")]


def _scaled(section: str, wait_s: int, max_time_s: int) -> str:
    """The section's own waits, in test seconds. WARM_WAIT and WARM_MAX_TIME are
    the fix's. `--max-time 120` is main's, scaled to the start's wait, so main's
    section (and a negative control that puts it back) runs on the same clock."""
    section, n_wait = re.subn(r"^(\s*)WARM_WAIT=\d+$", rf"\g<1>WARM_WAIT={wait_s}", section, flags=re.M)
    section, n_cap = re.subn(r"^(\s*)WARM_MAX_TIME=\d+$", rf"\g<1>WARM_MAX_TIME={max_time_s}", section,
                             flags=re.M)
    section, n_main = re.subn(r"--max-time 120\b", f"--max-time {wait_s}", section)
    assert n_wait + n_cap + n_main, "found no warm-up wait to scale in the Ollama section"
    return section


class Run:
    def __init__(self, proc: subprocess.Popen, out: str, root: Path) -> None:
        self.proc, self.out, self.root = proc, out, root

    @property
    def warm_log(self) -> str:
        path = self.root / ".ira-run" / "ollama-warm.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def warnings(self) -> list[str]:
        return [ln for ln in self.out.splitlines() if ln.startswith("WARN ")]


@pytest.fixture
def run_section(tmp_path: Path) -> Iterator[Callable[..., Run]]:
    """Run an Ollama section (start-ira.sh's own by default) against a stand-in.

    The script runs in its own session, so whatever it leaves running (the
    background warm-up) is in one process group, which is stopped at the end.
    """
    groups: list[int] = []

    def run(srv: StandIn, fast: str, deep: str, *, wait_s: int, max_time_s: int = 60,
            section: str | None = None, signals_after_request: tuple[int, ...] = ()) -> Run:
        text = START_SH.read_text(encoding="utf-8")
        body = _scaled(section if section is not None else _ollama_section(text), wait_s, max_time_s)
        root = tmp_path / "root"
        (root / ".ira-run").mkdir(parents=True, exist_ok=True)
        stub_bin = tmp_path / "bin"
        stub_bin.mkdir(exist_ok=True)
        stub = stub_bin / "ollama"
        stub.write_text(f'#!/bin/sh\necho "$*" >> "{tmp_path}/ollama-calls"\nexit 1\n', encoding="utf-8")
        stub.chmod(0o755)
        script = tmp_path / "ollama-section.sh"
        script.write_text(
            "set -uo pipefail\n"
            'ok() { echo "OK $1 ${2:-}"; }\nwarn() { echo "WARN $1 ${2:-}"; }\n'
            'fail() { echo "FAIL $1 ${2:-}"; }\n'
            'ROOT="$1"; RUN_DIR="$ROOT/.ira-run"; PY=true\n'
            + _helpers(text) + body, encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if not k.lower().endswith("_proxy") and not k.startswith("OLLAMA_")}
        env.update(OLLAMA_BASE_URL=f"http://127.0.0.1:{srv.port}/v1", OLLAMA_MODEL_FAST=fast,
                   OLLAMA_MODEL_DEEP=deep, OLLAMA_KEEP_ALIVE="5m",
                   PATH=f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}")
        proc = subprocess.Popen(["bash", str(script), str(root)], env=env, start_new_session=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        groups.append(proc.pid)
        if signals_after_request:
            assert srv.wait_for(lambda: bool(srv.requests), 30), "the warm-up never reached the stand-in"
            for sig in signals_after_request:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, sig)
        out, _ = proc.communicate(timeout=120)
        assert not (tmp_path / "ollama-calls").exists(), "the section ran `ollama`"
        return Run(proc, out, root)

    yield run
    for pgid in groups:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)


def _outcomes(srv: StandIn) -> list[tuple[str, str]]:
    with srv.changed:
        return list(srv.outcomes)


def _answers(srv: StandIn) -> list[tuple[str, str]]:
    with srv.changed:
        return list(srv.answers)


def _prompts(srv: StandIn) -> list[object]:
    with srv.changed:
        return [body.get("prompt", "") for body in srv.bodies]


def _eventually(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


# ── main's warm-up, on the same stand-in ─────────────────────────────────────


@needs_bash
def test_mains_warm_up_cancels_a_slow_first_load_and_says_nothing(standin, run_section):
    """The control: what the rehearsal saw, scaled. Both of main's calls hang
    up before the load is over, and the start still says OK."""
    srv = standin(["qwen3:4b"], load_s=4)

    run = run_section(srv, "qwen3:4b", "qwen3:4b", wait_s=1, section=MAIN_SECTION)

    assert srv.requests == ["qwen3:4b", "qwen3:4b"], "main warmed one model once per tier"
    assert _outcomes(srv) == [("qwen3:4b", "cancelled"), ("qwen3:4b", "cancelled")]
    assert "OK ollama" in run.out and not run.warnings(), run.out


# ── start-ira.sh's warm-up ───────────────────────────────────────────────────


@needs_bash
def test_a_slow_first_load_is_not_cancelled(standin, run_section):
    srv = standin(["qwen3:4b"], load_s=5)

    run = run_section(srv, "qwen3:4b", "qwen3:4b", wait_s=1)

    # The start stopped waiting before the load was over, and said so...
    assert ("qwen3:4b", "loaded") not in _outcomes(srv), "the start waited past its WARM_WAIT"
    assert "OK ollama" in run.out, run.out
    still = [w for w in run.warnings() if "qwen3:4b still loading after 1s" in w]
    assert still and "ollama-warm.log" in still[0], run.out
    # ...and the load went on to the end.
    assert srv.wait_for(lambda: bool(srv.outcomes), 30), "the load never finished"
    assert _outcomes(srv) == [("qwen3:4b", "loaded")], "the warm-up hung up before the load was over"
    assert _eventually(lambda: run.warm_log.splitlines()[-2:] == ["warm: qwen3:4b", "done"], 10), \
        run.warm_log


@needs_bash
@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGINT], ids=["SIGHUP-window-closed", "SIGINT-ctrl-c"])
def test_closing_the_terminal_or_ctrl_c_during_the_wait_does_not_cancel_the_load(standin, run_section, sig):
    """Closing the window sends SIGHUP to the start's whole process group, and
    Ctrl-C sends SIGINT. Either one ended main's foreground curl, and the load.
    A background job ignores SIGINT itself, but the curl it runs does not
    unless the job says so: the first version of this fix missed that."""
    srv = standin(["qwen3:4b"], load_s=4)

    run_section(srv, "qwen3:4b", "qwen3:4b", wait_s=30, signals_after_request=(sig,))

    assert srv.wait_for(lambda: bool(srv.outcomes), 30), "the load never finished"
    assert _outcomes(srv) == [("qwen3:4b", "loaded")], f"{sig.name} to the start cancelled the load"


@needs_bash
def test_a_load_that_finishes_within_the_wait_is_warm_and_quiet(standin, run_section):
    srv = standin(["qwen3:4b"], load_s=0.3)

    run = run_section(srv, "qwen3:4b", "qwen3:4b", wait_s=30)

    assert _outcomes(srv) == [("qwen3:4b", "loaded")], "the start returned before the load was over"
    assert "OK ollama" in run.out and not run.warnings(), run.out
    assert run.warm_log.splitlines() == ["loading: qwen3:4b", "warm: qwen3:4b", "done"], run.warm_log
    assert not list((run.root / ".ira-run").glob("ollama-warm.log.*")), "scratch files were left behind"


@needs_bash
def test_a_failed_warm_up_is_a_warning_with_ollamas_reason(standin, run_section):
    reason = "model requires more system memory (10.2 GiB) than is available (3.1 GiB)"
    srv = standin(["qwen3:14b"], load_s=0.1, errors={"qwen3:14b": reason})

    run = run_section(srv, "qwen3:14b", "qwen3:14b", wait_s=30)

    assert any(f'could not warm qwen3:14b (HTTP 500) {{"error":"{reason}"}}' in w
               for w in run.warnings()), run.out


@needs_bash
def test_ollama_closing_the_connection_is_a_warning(standin, run_section):
    srv = standin(["qwen3:8b"], load_s=0.1, errors={"qwen3:8b": None})

    run = run_section(srv, "qwen3:8b", "qwen3:8b", wait_s=30)

    assert any("could not warm qwen3:8b (HTTP 000) curl: (52)" in w for w in run.warnings()), run.out


@needs_bash
def test_one_model_for_both_tiers_is_warmed_once(standin, run_section):
    srv = standin(["qwen3:4b"], load_s=0.2)

    run = run_section(srv, "qwen3:4b", "qwen3:4b", wait_s=30)

    assert srv.requests == ["qwen3:4b"], f"warmed {len(srv.requests)} times: {srv.requests}"
    assert not run.warnings(), run.out


@needs_bash
def test_two_models_are_each_warmed_once(standin, run_section):
    srv = standin(["qwen3:8b", "qwen3:14b"], load_s=0.2)

    run = run_section(srv, "qwen3:8b", "qwen3:14b", wait_s=30)

    assert srv.requests == ["qwen3:8b", "qwen3:14b"], srv.requests
    assert _outcomes(srv) == [("qwen3:8b", "loaded"), ("qwen3:14b", "loaded")]
    assert not run.warnings(), run.out


@needs_bash
def test_the_model_still_loading_is_the_one_named(standin, run_section):
    """Two models, the wait over during the second's load: the warning names
    the second, and both loads finish."""
    srv = standin(["qwen3:8b", "qwen3:14b"], load_s={"qwen3:8b": 0.2, "qwen3:14b": 6})

    run = run_section(srv, "qwen3:8b", "qwen3:14b", wait_s=2)

    warnings = run.warnings()
    assert len(warnings) == 1 and "qwen3:14b still loading after 2s" in warnings[0], run.out
    assert srv.wait_for(lambda: len(srv.outcomes) == 2, 30), _outcomes(srv)
    assert _outcomes(srv) == [("qwen3:8b", "loaded"), ("qwen3:14b", "loaded")]


# ── P2-07: the warm-up loads the model and asks for nothing else ─────────────

# The request line of main's warm-up as it was until P2-07's fix (52ec6779),
# verbatim. The control puts it back into start-ira.sh's own section.
MAIN_WARM_UP_REQUEST = '-d "{\\"model\\":\\"$m\\",\\"prompt\\":\\"ok\\",\\"stream\\":false,\\"keep_alive\\":\\"$KEEP_ALIVE\\"}" \\'


def _with_mains_warm_up_request(section: str) -> str:
    section, n = re.subn(r'^(\s*)-d "\{\\"model\\":\\"\$m\\".*$',
                         lambda mo: mo.group(1) + MAIN_WARM_UP_REQUEST, section, count=1, flags=re.M)
    assert n == 1, "start-ira.sh's warm-up no longer sends its body with -d \"{\\\"model\\\":\\\"$m\\\"...\""
    return section


@needs_bash
def test_mains_warm_up_request_has_the_model_write_a_reply_and_the_start_waits_for_it(standin, run_section):
    """The control for P2-07: start-ira.sh's section with main's request line
    put back, on the clean install's second start (the model already loaded).
    The stand-in is asked for a reply, writes it, and the start waits for it.
    This shows the stand-in sees what the tests below rule out."""
    srv = standin(["qwen3:4b"], load_s=5, loaded=["qwen3:4b"], reply_s=1)
    section = _with_mains_warm_up_request(_ollama_section(START_SH.read_text(encoding="utf-8")))

    run = run_section(srv, "qwen3:4b", "qwen3:4b", wait_s=30, section=section)

    assert _prompts(srv) == ["ok"]
    assert _answers(srv) == [("qwen3:4b", "reply"), ("qwen3:4b", "reply written")], _answers(srv)
    assert run.warm_log.splitlines() == ["loading: qwen3:4b", "warm: qwen3:4b", "done"], run.warm_log


@needs_bash
def test_the_warm_up_asks_ollama_only_to_load_the_model(standin, run_section):
    """No prompt, so Ollama loads the model and writes nothing. keep_alive
    still pins it, and Ollama's load-only answer (HTTP 200, done_reason
    "load", an empty response) still reads as warm."""
    srv = standin(["qwen3:4b"], load_s=0.3)

    run = run_section(srv, "qwen3:4b", "qwen3:4b", wait_s=30)

    assert _prompts(srv) == [""], f"the warm-up asked the model for a reply: {srv.bodies}"
    assert _answers(srv) == [("qwen3:4b", "load only")], _answers(srv)
    assert [body.get("keep_alive") for body in srv.bodies] == ["5m"], srv.bodies
    assert _outcomes(srv) == [("qwen3:4b", "loaded")]
    assert "OK ollama" in run.out and not run.warnings(), run.out
    assert run.warm_log.splitlines() == ["loading: qwen3:4b", "warm: qwen3:4b", "done"], run.warm_log


@needs_bash
def test_a_start_with_the_model_already_loaded_does_not_wait_for_a_reply(standin, run_section):
    """The clean install's second start: the model was loaded by the first.
    A reply here would take longer than the start waits (60 s against 10 s);
    the start would then warn "still loading" about a model that is loaded,
    and the reply would hold the CPU when the owner's first question comes."""
    srv = standin(["qwen3:4b"], load_s=5, loaded=["qwen3:4b"], reply_s=60)

    run = run_section(srv, "qwen3:4b", "qwen3:4b", wait_s=10)

    assert _answers(srv) == [("qwen3:4b", "load only")], _answers(srv)
    assert _outcomes(srv) == [("qwen3:4b", "already loaded")]
    assert "OK ollama" in run.out and not run.warnings(), run.out
    assert run.warm_log.splitlines() == ["loading: qwen3:4b", "warm: qwen3:4b", "done"], run.warm_log


# ── The numbers, as shipped ──────────────────────────────────────────────────


def test_the_warm_up_cap_is_far_above_a_cold_load_and_the_wait_no_longer_than_mains():
    """The rehearsal's cold load took more than 120 s. The start's wait is no
    longer than main's two calls of up to 120 s could make it."""
    section = _ollama_section(START_SH.read_text(encoding="utf-8"))
    cap = re.search(r"^\s*WARM_MAX_TIME=(\d+)$", section, re.M)
    wait = re.search(r"^\s*WARM_WAIT=(\d+)$", section, re.M)
    assert cap and wait, "start-ira.sh's Ollama section no longer sets WARM_MAX_TIME and WARM_WAIT"
    assert int(cap.group(1)) >= 900, f"the warm-up cap is {cap.group(1)} s"
    assert int(wait.group(1)) <= 240, f"the start waits up to {wait.group(1)} s for the warm-up"
    code = "\n".join(ln for ln in section.splitlines() if not ln.lstrip().startswith("#"))
    assert '--max-time "$WARM_MAX_TIME"' in code
    assert "--max-time 120" not in code


def _ps1_ollama_section() -> str:
    text = START_PS1.read_text(encoding="utf-8")
    return text[text.index("# -- 3. Ollama"):text.index("# -- 4. Cortex")]


def test_start_ps1_does_not_cancel_a_slow_load_and_reports_a_failed_one():
    """start-ira.ps1 had the same three faults. It is read here, not run:
    its PowerShell tests skip where pwsh is not installed."""
    section = _ps1_ollama_section()
    call = re.search(r"Invoke-RestMethod [^\n]*/api/generate[^\n]*-TimeoutSec (\d+)", section)
    assert call, "start-ira.ps1 no longer warms with Invoke-RestMethod ... -TimeoutSec"
    assert int(call.group(1)) >= 900, f"start-ira.ps1 cancels a load after {call.group(1)} s"
    after = section[call.end():]
    assert not re.match(r"[^\n]*\n\s*\}\s*catch\s*\{\s*\}", after), "a failed warm-up is swallowed"
    assert 'Set-Status "ollama" "WARN"' in section, "a failed warm-up is not reported"
    assert "$deepModel -ne $fastModel" in section, "one model for both tiers is warmed twice"


def test_start_ps1_warm_up_asks_ollama_only_to_load_the_model():
    """P2-07 in start-ira.ps1: the warm-up's body has no prompt (or an empty
    one), so Ollama loads the model and writes nothing; it still names the
    model and keep_alive. Read, not run, as above."""
    section = _ps1_ollama_section()
    found = re.search(r"\$body = @\{([^}]*)\}[^\n]*\n\s*Invoke-RestMethod [^\n]*/api/generate", section)
    assert found, "start-ira.ps1 no longer builds the warm-up's body as a hashtable just before /api/generate"
    fields = {key.strip(): value.strip()
              for key, _, value in (entry.partition("=") for entry in found.group(1).split(";"))
              if key.strip()}
    assert fields.get("prompt", '""') in ('""', "''"), f"start-ira.ps1's warm-up asks for a reply: {fields}"
    assert fields.get("model") == "$m" and fields.get("keep_alive") == "$keepAlive", fields
