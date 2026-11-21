"""scripts/smoke_test.py: a chat that never answers is recorded, not a crash.

On a CPU-only machine a chat can take longer than the smoke test waits. The
script must then record that check, carry on, and still delete the synthetic
memory it saved, so a user's Memory Vault is not left holding test data.

The test runs the real script against a small stand-in for IRA's HTTP API on
127.0.0.1, whose "recall" chat answers only after the script has given up.
"""
from __future__ import annotations

import importlib.util
import json
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2]          # supracloud-jarvis/
CHAT_TIMEOUT = 0.5
RECALL_DELAY = 2.0


def _smoke_module():
    spec = importlib.util.spec_from_file_location(
        "smoke_test_script", APP_ROOT / "scripts" / "smoke_test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeIra(BaseHTTPRequestHandler):
    memories: dict[str, str] = {}

    def log_message(self, *args) -> None:  # keep pytest output clean
        pass

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the script stopped waiting, which is what these tests arrange

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/health":
            return self._send(200, {"status": "ok"})
        if "Authorization" not in self.headers:
            return self._send(401, {"detail": "not signed in"})
        if url.path == "/api/v1/trust/status":
            return self._send(200, {"status": "local_only"})
        if url.path == "/api/v1/memory":
            q = urllib.parse.parse_qs(url.query).get("q", [""])[0]
            hits = [{"id": i, "content": c} for i, c in self.memories.items() if q in c]
            return self._send(200, {"memories": hits})
        return self._send(404, {})

    def do_POST(self) -> None:
        body = self._body()
        if self.path == "/auth/token":
            return self._send(200, {"access_token": "test-token"})
        if self.path == "/api/v1/memory":
            mem_id = f"m{len(self.memories) + 1}"
            self.memories[mem_id] = json.loads(body)["content"]
            return self._send(200, {"id": mem_id})
        if self.path == "/api/v1/chat":
            message = json.loads(body)["message"]
            if "codename" in message:                    # the recall check
                time.sleep(RECALL_DELAY)
                return self._send(200, {"response": "too late"})
            reply = "4" if "number" in message else "Two plus two is four."
            return self._send(200, {"response": reply, "session_id": "s1",
                                    "model_used": "fake"})
        return self._send(404, {})

    def do_DELETE(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        mem_id = url.path.rsplit("/", 1)[-1]
        if "confirm_token" not in urllib.parse.parse_qs(url.query):
            return self._send(200, {"status": "confirmation_required", "token": "approve"})
        self.memories.pop(mem_id, None)
        return self._send(200, {"status": "deleted"})


@pytest.fixture
def fake_ira():
    FakeIra.memories = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeIra)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_a_chat_that_times_out_is_recorded_and_the_test_memory_is_still_removed(
        fake_ira: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = tmp_path / ".env"
    env.write_text("IRA_ADMIN_USERNAME=admin\nIRA_ADMIN_PASSWORD=synthetic\n", encoding="utf-8")
    out = tmp_path / "report.json"

    code = _smoke_module().main(["--base", fake_ira, "--env-file", str(env),
                                 "--chat-timeout", str(CHAT_TIMEOUT), "--out", str(out)])

    results = {r["check"]: r for r in json.loads(out.read_text(encoding="utf-8"))["results"]}
    assert results["recall"]["result"] == "MISS"
    assert results["recall"]["observed"].startswith("no response")
    assert results["forget"]["result"] == "PASS", results["forget"]
    assert FakeIra.memories == {}, "the synthetic memory was left in the vault"
    assert [r for r in results.values() if r["result"] == "FAIL"] == []
    assert code == 0
    # The summary line must not count the miss as a pass.
    assert "PASS: 8/9 checks passed, missed: recall" in capsys.readouterr().out


def test_a_first_chat_that_times_out_fails_the_run(fake_ira: str, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("IRA_ADMIN_PASSWORD=synthetic\n", encoding="utf-8")
    out = tmp_path / "report.json"
    original = FakeIra.do_POST

    def slow_chat(self) -> None:
        if self.path == "/api/v1/chat":
            self._body()
            time.sleep(RECALL_DELAY)
            return self._send(200, {"response": "too late"})
        return original(self)

    monkeypatch.setattr(FakeIra, "do_POST", slow_chat)
    code = _smoke_module().main(["--base", fake_ira, "--env-file", str(env),
                                 "--chat-timeout", str(CHAT_TIMEOUT), "--out", str(out)])

    results = {r["check"]: r for r in json.loads(out.read_text(encoding="utf-8"))["results"]}
    assert results["chat"]["result"] == "FAIL"
    assert "no response" in results["chat"]["observed"]
    assert FakeIra.memories == {}
    assert code == 1


def test_a_stopped_ira_is_reported_not_a_crash(tmp_path: Path) -> None:
    with socket.socket() as probe:                       # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = tmp_path / ".env"
    env.write_text("IRA_ADMIN_PASSWORD=synthetic\n", encoding="utf-8")
    out = tmp_path / "report.json"

    code = _smoke_module().main(["--base", f"http://127.0.0.1:{port}",
                                 "--env-file", str(env), "--out", str(out)])

    results = {r["check"]: r for r in json.loads(out.read_text(encoding="utf-8"))["results"]}
    assert results["health"]["result"] == "FAIL"
    assert results["health"]["observed"].startswith("no response")
    assert code == 1
