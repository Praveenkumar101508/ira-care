"""scripts/owner_flow_check.py: the owner-flow dry run reports what IRA did.

The script is what the owner runs before a demo, so a PASS from it has to mean
the gates held. These tests run the real script against a small stand-in for
IRA's HTTP API and its model server on 127.0.0.1. One stand-in behaves; the
others each break one promise the flow checks (a memory saved before the owner
approved it, a note deleted without a token, a token that works twice, a recall
that misses), and the script must report exactly that step as FAIL.

It must also never print the password or an approval token, and must remove
its synthetic memory even when a step fails.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2]          # supracloud-jarvis/
PASSWORD = "synthetic-owner-password"
TOKEN_PREFIX = "approve-once-"


def _flow_module():
    spec = importlib.util.spec_from_file_location(
        "owner_flow_check_script", APP_ROOT / "scripts" / "owner_flow_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeIra(BaseHTTPRequestHandler):
    """IRA's API (and /api/ps of its model server) as the flow uses them."""

    memories: dict[str, str] = {}
    notes: dict[str, str] = {}
    used_tokens: set[str] = set()
    ledger: list[str] = []
    history: list[dict] = []
    chats: list[str] = []
    valid_tokens: set[str] = set()
    issued = 0
    expire_token_after_recall = False
    stream_error = False
    stream_without_done = False
    # The planted defects; each test turns one on.
    save_before_approval = False
    delete_without_token = False
    reusable_token = False
    recall_misses = False

    def log_message(self, *args) -> None:
        pass

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, out: dict) -> None:
        """The chat page's stream: token events, then one done event."""
        words = out["response"].split(" ")
        events = [{"token": w + (" " if i < len(words) - 1 else "")} for i, w in enumerate(words)]
        if self.stream_error:
            events = events[:1] + [{"error": "Stream interrupted: model unavailable"}]
        elif not self.stream_without_done:
            events.append({"done": True, "agent": out["agent_used"], "model": "qwen3-fast",
                           "memory_decision": out.get("memory_decision")})
        data = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        return self.headers.get("Authorization", "").removeprefix("Bearer ") in self.valid_tokens

    def _json(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/health":
            return self._send(200, {"status": "ok"})
        if url.path == "/api/ps":
            return self._send(200, {"models": [{"name": "qwen3:4b"}]})
        if not self._authed():
            return self._send(401, {"detail": "not signed in"})
        if url.path == "/api/v1/trust/status":
            return self._send(200, {"status": "local_only",
                                    "model": {"backend": "ollama", "local": True}})
        if url.path == "/api/v1/memory":
            q = urllib.parse.parse_qs(url.query).get("q", [""])[0].lower()
            hits = [{"id": i, "content": c} for i, c in self.memories.items() if q in c.lower()]
            return self._send(200, {"memories": hits})
        if url.path.startswith("/api/v1/notes/"):
            note_id = urllib.parse.unquote(url.path.rsplit("/", 1)[-1])
            if note_id in self.notes:
                return self._send(200, {"id": note_id, "title": self.notes[note_id]})
            return self._send(404, {"detail": "not found"})
        if url.path == "/api/v1/trust/ledger":
            return self._send(200, {"status": "OK", "verified": True,
                                    "records": [{"kind": k} for k in self.ledger]})
        if url.path == "/api/v1/chat/history":
            return self._send(200, {"messages": self.history})
        return self._send(404, {})

    def do_POST(self) -> None:
        if self.path == "/auth/token":
            length = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode())
            ok = form.get("password") == [PASSWORD]
            if not ok:
                return self._send(401, {})
            FakeIra.issued += 1
            token = f"t{FakeIra.issued}"
            self.valid_tokens.add(token)
            return self._send(200, {"access_token": token})
        if not self._authed():
            self._json()
            return self._send(401, {"detail": "token expired"})
        body = self._json()
        if self.path in ("/api/v1/chat", "/api/v1/chat/stream"):
            message = body["message"]
            self.chats.append(message)
            if message.startswith("Remember that "):
                content = message[len("Remember that "):]
                if self.save_before_approval:
                    self.memories["m1"] = content
                out = {"response": "Shall I save this?", "agent_used": "memory_guard",
                       "model_used": "deterministic",
                       "memory_decision": {"status": "needs_owner_approval",
                                           "pending": {"content": content}}}
            elif "codename" in message:
                known = next(iter(self.memories.values()), "")
                answer = "I do not know." if self.recall_misses else known.split()[-1].rstrip(".")
                if self.expire_token_after_recall:          # a slow answer outlasted the token
                    self.valid_tokens.clear()
                out = {"response": answer, "agent_used": "conversational", "model_used": "qwen3:4b"}
            else:
                answer = "The capital of France is Paris."
                self.history[:] = [{"role": "user", "content": message},
                                   {"role": "assistant", "content": answer}]
                out = {"response": answer, "agent_used": "conversational", "model_used": "qwen3:4b"}
            if self.path == "/api/v1/chat":
                return self._send(200, out)
            return self._sse(out)
        if self.path == "/api/v1/chat/memory/resolve":
            self.memories["m1"] = body["pending"]["content"]
            return self._send(200, {"status": "saved"})
        if self.path == "/api/v1/notes":
            self.notes["n1"] = body["title"]
            return self._send(200, {"status": "created", "id": "n1"})
        return self._send(404, {})

    def do_DELETE(self) -> None:
        if not self._authed():
            return self._send(401, {"detail": "token expired"})
        url = urllib.parse.urlsplit(self.path)
        item = urllib.parse.unquote(url.path.rsplit("/", 1)[-1])
        token = urllib.parse.parse_qs(url.query).get("confirm_token", [None])[0]
        store = self.notes if url.path.startswith("/api/v1/notes/") else self.memories
        if token is None and not self.delete_without_token:
            self.ledger.append("staged")
            return self._send(200, {"status": "confirmation_required",
                                    "token": f"{TOKEN_PREFIX}{len(self.ledger)}"})
        if token is not None and token in self.used_tokens and not self.reusable_token:
            return self._send(409, {"detail": "Confirmation token invalid, already used, or expired."})
        if token is not None:
            self.used_tokens.add(token)
        store.pop(item, None)
        self.ledger.append("executed")
        return self._send(200, {"status": "deleted"})


@pytest.fixture
def fake_ira():
    FakeIra.memories, FakeIra.notes, FakeIra.used_tokens = {}, {}, set()
    FakeIra.ledger, FakeIra.history, FakeIra.chats = [], [], []
    FakeIra.valid_tokens, FakeIra.issued, FakeIra.expire_token_after_recall = set(), 0, False
    FakeIra.stream_error = FakeIra.stream_without_done = False
    FakeIra.save_before_approval = FakeIra.delete_without_token = False
    FakeIra.reusable_token = FakeIra.recall_misses = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeIra)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _run(base: str, tmp_path: Path, *extra: str) -> tuple[int, dict[str, dict]]:
    env = tmp_path / ".env"
    env.write_text(f"IRA_ADMIN_PASSWORD={PASSWORD}\nOLLAMA_MODEL_FAST=qwen3:4b\n", encoding="utf-8")
    out = tmp_path / "report.json"
    code = _flow_module().main(["--base", base, "--env-file", str(env), "--ollama", base,
                                "--chat-timeout", "10", "--out", str(out), *extra])
    report = json.loads(out.read_text(encoding="utf-8"))
    return code, {r["step"]: r for r in report["results"]}


def _failed(results: dict[str, dict]) -> list[str]:
    return [name for name, r in results.items() if r["result"] == "FAIL"]


def test_a_flow_whose_gates_hold_passes_every_step_and_cleans_up(
        fake_ira: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, results = _run(fake_ira, tmp_path)

    assert _failed(results) == [], results
    assert list(results) == [
        "health", "auth-gate", "sign-in", "trust", "chat", "local-model", "memory-proposed",
        "memory-approved", "recall", "note-created", "delete-staged", "delete-confirmed",
        "token-single-use", "audit", "history", "forget"]
    assert code == 0
    assert FakeIra.memories == {} and FakeIra.notes == {}
    printed = capsys.readouterr().out
    report = (tmp_path / "report.json").read_text(encoding="utf-8")
    assert len(FakeIra.used_tokens) == 2                 # the note's and the memory's
    for text in (printed, report):
        assert PASSWORD not in text and TOKEN_PREFIX not in text


def test_a_memory_saved_before_the_owner_approves_fails_that_step(
        fake_ira: str, tmp_path: Path) -> None:
    FakeIra.save_before_approval = True
    code, results = _run(fake_ira, tmp_path)
    assert _failed(results) == ["memory-proposed"]
    assert "in vault before approval=True" in results["memory-proposed"]["observed"]
    assert code == 1
    assert FakeIra.memories == {}


def test_a_note_deleted_without_a_token_fails_the_gate_steps(
        fake_ira: str, tmp_path: Path) -> None:
    FakeIra.delete_without_token = True
    code, results = _run(fake_ira, tmp_path)
    assert "delete-staged" in _failed(results)
    assert results["delete-confirmed"]["observed"].startswith("not run:")
    assert code == 1


def test_a_token_that_works_twice_fails_single_use(fake_ira: str, tmp_path: Path) -> None:
    FakeIra.reusable_token = True
    code, results = _run(fake_ira, tmp_path)
    assert _failed(results) == ["token-single-use"]
    assert code == 1


def test_a_missed_recall_fails_and_the_memory_is_still_removed(
        fake_ira: str, tmp_path: Path) -> None:
    FakeIra.recall_misses = True
    code, results = _run(fake_ira, tmp_path)
    assert _failed(results) == ["recall"]
    assert results["forget"]["result"] == "PASS"
    assert FakeIra.memories == {}
    assert code == 1


def test_a_wrong_password_stops_after_sign_in(fake_ira: str, tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("IRA_ADMIN_PASSWORD=not-the-password\n", encoding="utf-8")
    out = tmp_path / "report.json"
    code = _flow_module().main(["--base", fake_ira, "--env-file", str(env), "--out", str(out)])
    steps = [r["step"] for r in json.loads(out.read_text(encoding="utf-8"))["results"]]
    assert steps == ["health", "auth-gate", "sign-in"]
    assert code == 1


def test_no_think_changes_only_the_questions_to_the_model(fake_ira: str, tmp_path: Path) -> None:
    code, results = _run(fake_ira, tmp_path, "--no-think")
    assert code == 0, results
    question, remember, recall = FakeIra.chats
    assert question.endswith(" /no_think") and recall.endswith(" /no_think")
    # The Remember line is saved verbatim: the switch must never reach the vault.
    assert remember.startswith("Remember that ") and "/no_think" not in remember
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["no_think"] is True


def test_without_no_think_the_questions_are_sent_as_written(fake_ira: str, tmp_path: Path) -> None:
    _run(fake_ira, tmp_path)
    assert not any("/no_think" in m for m in FakeIra.chats)


def test_an_access_token_that_expires_mid_run_is_renewed_by_signing_in_again(
        fake_ira: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Found on the first real run, 31 October 2026: a CPU-only answer outlasted the
    # 30-minute access token, and every later step read HTTP 401.
    FakeIra.expire_token_after_recall = True
    code, results = _run(fake_ira, tmp_path)
    assert _failed(results) == [], results
    assert code == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["sign_ins"] == 2
    assert FakeIra.memories == {} and FakeIra.notes == {}
    assert PASSWORD not in capsys.readouterr().out


def test_stream_mode_walks_the_same_flow_through_the_chat_pages_endpoint(
        fake_ira: str, tmp_path: Path) -> None:
    code, results = _run(fake_ira, tmp_path, "--stream")
    assert _failed(results) == [], results
    assert code == 0
    assert "The capital of France is Paris." in results["chat"]["observed"]
    assert json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))["stream"] is True
    assert FakeIra.memories == {} and FakeIra.notes == {}


def test_a_stream_that_reports_an_error_fails_the_chat(fake_ira: str, tmp_path: Path) -> None:
    FakeIra.stream_error = True
    code, results = _run(fake_ira, tmp_path, "--stream")
    assert results["chat"]["result"] == "FAIL"
    assert "model unavailable" in results["chat"]["observed"]
    assert code == 1


def test_a_stream_that_never_finishes_fails_the_chat(fake_ira: str, tmp_path: Path) -> None:
    FakeIra.stream_without_done = True
    code, results = _run(fake_ira, tmp_path, "--stream")
    assert results["chat"]["result"] == "FAIL"
    assert "without a done event" in results["chat"]["observed"]
    assert code == 1
