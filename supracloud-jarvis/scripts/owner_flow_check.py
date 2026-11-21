#!/usr/bin/env python3
"""Walk the owner's core flow through a running IRA and print PASS/FAIL per step.

For a dry run before a demo, after `start-ira.sh` / `start-ira.ps1`:

    python3 scripts/owner_flow_check.py                     # report to stdout
    python3 scripts/owner_flow_check.py --out report.json   # and keep the evidence
    python3 scripts/owner_flow_check.py --no-think          # slow CPU: skip qwen3's thinking
    python3 scripts/owner_flow_check.py --stream            # chat as the chat page sends it

It talks to the real backend through the origin a browser uses (the frontend on
:3000, which proxies /api and /auth), with the owner login from `.env`, and to
the local model server only to ask which model is loaded. Steps, in order:

  health            /health answers
  auth-gate         the API refuses a request with no token
  sign-in           the owner signs in with the password from .env
  trust             the Trust Console reports local-only processing
  chat              the local model answers a synthetic question
  local-model       the model server has the configured chat model loaded
  memory-proposed   "Remember that ..." in chat is held for approval, not saved
  memory-approved   the owner approves it, and the Memory Vault holds it
  recall            a later chat, in a new conversation, answers from that memory
  note-created      a synthetic note is created
  delete-staged     deleting it without approval is refused: a token is issued,
                    the note is still there
  delete-confirmed  with the token the note is deleted
  token-single-use  the same token cannot delete again
  audit             the Trust Ledger holds the delete, and its chain verifies
  history           the first conversation is stored: question and answer
  forget            the synthetic memory is removed, through the same approval

A step that depends on an earlier one that failed is recorded as FAIL with the
reason, never skipped silently. Whatever happens, the synthetic memory and note
are removed at the end, through their approval gates.

Standard library only, like scripts/smoke_test.py. It never prints a secret or
a token: the password is read from .env and sent only in the login request.
Timing is not reported as a figure: on a shared or CPU-only machine it measures
the machine, not IRA.
"""
from __future__ import annotations

import argparse
import json
import platform
import random
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
MARK = "SYNTHETIC"


def _env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, val = line.partition("=")
            val = val.split(" #", 1)[0].strip().strip('"')
            out[key.strip()] = val
    return out


class Client:
    def __init__(self, base: str, timeout: float) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.token: str | None = None
        # Signs in again when the access token has expired (30 minutes by default),
        # as the browser refreshes it: one slow CPU answer can outlast a token.
        self.relogin = None
        self.sign_ins = 0

    def call(self, method: str, path: str, *, json_body=None, form=None, auth=True,
             timeout: float | None = None, _retried: bool = False):
        status, body = self._call(method, path, json_body=json_body, form=form, auth=auth,
                                  timeout=timeout)
        if status == 401 and auth and self.token and self.relogin and not _retried:
            if self.relogin():
                return self.call(method, path, json_body=json_body, form=form, auth=auth,
                                 timeout=timeout, _retried=True)
        return status, body

    def _call(self, method: str, path: str, *, json_body=None, form=None, auth=True,
              timeout: float | None = None):
        headers = {"Accept": "application/json"}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        url = path if path.startswith("http") else self.base + path
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            status = exc.code
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            return None, f"no response ({getattr(exc, 'reason', exc)})"
        try:
            return status, json.loads(body)
        except ValueError:
            return status, body


def _read_stream(c: "Client", path: str, body: dict, timeout: float):
    """POST to a server-sent-events chat endpoint, as the chat page does.

    Returns (status, body) shaped like POST /api/v1/chat's answer: the "token"
    events joined as `response`, and the done event's agent, model label and
    memory_decision. `timeout` bounds each read, not the whole answer, so a slow
    CPU that keeps sending tokens is waited for, as the browser waits for it.
    """
    headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
    if c.token:
        headers["Authorization"] = f"Bearer {c.token}"
    req = urllib.request.Request(c.base + path, data=json.dumps(body).encode(), method="POST",
                                 headers=headers)
    tokens: list[str] = []
    done: dict = {}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("error"):
                    return status, {"error": str(event["error"]), "response": "".join(tokens)}
                if "token" in event:
                    tokens.append(str(event["token"]))
                if event.get("done") is True:
                    done = event
                    break
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return None, f"no response ({getattr(exc, 'reason', exc)})"
    if not done:
        return None, "stream ended without a done event"
    return status, {"response": "".join(tokens), "agent_used": done.get("agent"),
                    "model_used": done.get("model"), "memory_decision": done.get("memory_decision"),
                    "session_id": done.get("session_id")}


def _http(status, body) -> str:
    return f"HTTP {status}" if status is not None else str(body)


def _short(text, n: int = 160) -> str:
    return " ".join(str(text or "").split())[:n]


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=APP_ROOT, capture_output=True,
                              text=True, timeout=10).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _vault_has(c: Client, marker: str) -> tuple[bool, str | None]:
    """Is a memory containing `marker` in the vault? Returns (found, id)."""
    status, body = c.call("GET", "/api/v1/memory?q=" + urllib.parse.quote(marker))
    if status != 200 or not isinstance(body, dict):
        return False, None
    for m in body.get("memories", []):
        if marker.lower() in str(m.get("content", "")).lower():
            return True, m.get("id")
    return False, None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Walk the owner's core flow through a running IRA.")
    ap.add_argument("--base", default=None, help="default http://127.0.0.1:$FRONTEND_PORT")
    ap.add_argument("--env-file", default=str(APP_ROOT / ".env"))
    ap.add_argument("--ollama", default=None,
                    help="model server, default OLLAMA_BASE_URL from .env without /v1")
    ap.add_argument("--chat-timeout", type=float, default=900.0,
                    help="seconds to wait for each answer from a CPU-only model")
    ap.add_argument("--out", help="also write the JSON report here")
    ap.add_argument("--stream", action="store_true",
                    help="send chat through /api/v1/chat/stream, the endpoint the chat page uses")
    ap.add_argument("--no-think", action="store_true",
                    help="end the two questions to the model with qwen3's /no_think switch, so a "
                         "slow CPU answers without the thinking phase (the Remember line is "
                         "never changed)")
    args = ap.parse_args(argv)
    ask = (lambda text: f"{text} /no_think") if args.no_think else (lambda text: text)

    env = _env(Path(args.env_file))
    base = args.base or f"http://127.0.0.1:{env.get('FRONTEND_PORT', '3000')}"
    ollama = (args.ollama or env.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")
    ollama = ollama[:-3] if ollama.endswith("/v1") else ollama
    model = env.get("OLLAMA_MODEL_FAST", "")
    c = Client(base, timeout=60)
    results: list[dict] = []
    run = uuid.uuid4().hex[:6]
    marker = "Bluefin-" + "".join(random.choice(string.digits) for _ in range(4))
    state: dict = {}

    def record(name: str, ok: bool, observed: str, **extra) -> bool:
        results.append({"step": name, "result": "PASS" if ok else "FAIL",
                        "observed": observed, **extra})
        print(f"  {'PASS' if ok else 'FAIL':4}  {name:17} {observed}", flush=True)
        return ok

    def blocked(name: str, why: str) -> None:
        record(name, False, f"not run: {why}")

    def send_chat(message: str, session_id: str):
        body = {"message": message, "session_id": session_id}
        if not args.stream:
            return c.call("POST", "/api/v1/chat", timeout=args.chat_timeout, json_body=body)
        status, out = _read_stream(c, "/api/v1/chat/stream", body, args.chat_timeout)
        if status == 401 and c.relogin and c.relogin():
            status, out = _read_stream(c, "/api/v1/chat/stream", body, args.chat_timeout)
        return status, out

    print(f"IRA owner flow against {base} (run {run}, memory marker {marker})", flush=True)
    status, body = c.call("GET", "/health", auth=False)
    record("health", status == 200, _http(status, body))

    status, body = c.call("GET", "/api/v1/memory", auth=False)
    record("auth-gate", status in (401, 403), f"no token -> {_http(status, body)}")

    user = env.get("IRA_ADMIN_USERNAME", "admin").split("#")[0].strip() or "admin"

    def sign_in():
        status, body = c.call("POST", "/auth/token", auth=False,
                              form={"username": user, "password": env.get("IRA_ADMIN_PASSWORD", "")})
        token = body.get("access_token") if isinstance(body, dict) else None
        if status == 200 and token:
            c.token, c.sign_ins = token, c.sign_ins + 1
        return status, body, token

    status, body, c.token = sign_in()
    c.relogin = lambda: bool(sign_in()[2])
    if not record("sign-in", status == 200 and bool(c.token), f"{_http(status, body)} as {user!r}"):
        return _finish(results, args, base, c.sign_ins)

    status, body = c.call("GET", "/api/v1/trust/status")
    trust = body if isinstance(body, dict) else {}
    mdl = trust.get("model") or {}
    record("trust", status == 200 and trust.get("status") == "local_only" and mdl.get("local") is True,
           f"HTTP {status}, status={trust.get('status')!r}, model backend={mdl.get('backend')!r} "
           f"local={mdl.get('local')!r}")

    # ── A real question to the local model ───────────────────────────────────
    session = f"owner-flow-{run}-a"
    question = ask(f"{MARK} question: in one short sentence, what is the capital of France?")
    status, body = send_chat(question, session)
    chat = body if isinstance(body, dict) else {}
    reply = str(chat.get("response", ""))
    state["answer"] = reply
    error = f", error={_short(chat['error'])!r}" if chat.get("error") else ""
    record("chat", status == 200 and bool(reply.strip()) and not chat.get("error")
           and chat.get("agent_used") != "memory_guard"
           and chat.get("model_used") not in (None, "", "deterministic"),
           f"{_http(status, body)}, agent={chat.get('agent_used')!r}, model={chat.get('model_used')!r}"
           f"{error}: {_short(reply)!r}", agent=chat.get("agent_used"), model=chat.get("model_used"))

    status, body = c.call("GET", ollama + "/api/ps", auth=False)
    loaded = [m.get("name") for m in body.get("models", [])] if isinstance(body, dict) else []
    record("local-model", status == 200 and bool(model) and model in loaded,
           f"{ollama}/api/ps -> {_http(status, body)}, loaded={loaded}, configured={model!r}")

    # ── Memory: proposed in chat, approved by the owner, recalled later ──────
    remember = f"Remember that the {MARK} demo codename is {marker}."
    status, body = send_chat(remember, f"owner-flow-{run}-b")
    decision = (body.get("memory_decision") or {}) if isinstance(body, dict) else {}
    found_early, _ = _vault_has(c, marker)
    proposed = (status == 200 and decision.get("status") == "needs_owner_approval"
                and not found_early)
    record("memory-proposed", proposed,
           f"{_http(status, body)}, decision={decision.get('status')!r}, "
           f"in vault before approval={found_early}")

    mem_id = None
    if decision.get("status") == "needs_owner_approval" and decision.get("pending"):
        status, body = c.call("POST", "/api/v1/chat/memory/resolve",
                              json_body={"resolution": "save", "pending": decision["pending"]})
        found, mem_id = _vault_has(c, marker)
        record("memory-approved", status == 200 and found,
               f"resolve {_http(status, body)}, in vault after approval={found}")
    else:
        blocked("memory-approved", "no memory proposal to approve")

    if mem_id:
        status, body = send_chat(ask(f"{MARK} check: what is the demo codename? Reply with the codename "
                                     "only."), f"owner-flow-{run}-c")
        reply = str(body.get("response", "")) if isinstance(body, dict) else ""
        record("recall", status == 200 and marker.lower() in reply.lower(),
               f"{_http(status, body)}, new conversation: {_short(reply)!r}")
    else:
        blocked("recall", "the memory was not saved")

    # ── An approval-gated action: delete a note ──────────────────────────────
    status, body = c.call("POST", "/api/v1/notes", json_body={
        "title": f"{MARK} demo note {run}",
        "body": f"{MARK}: created by owner_flow_check.py to exercise the delete approval.",
        "tags": [MARK.lower()]})
    note_id = body.get("id") if isinstance(body, dict) else None
    record("note-created", status == 200 and bool(note_id) and body.get("status") == "created",
           f"{_http(status, body)}, id={note_id!r}")

    token = None
    if note_id:
        note_path = "/api/v1/notes/" + urllib.parse.quote(note_id)
        status, draft = c.call("DELETE", note_path)
        if isinstance(draft, dict) and draft.get("status") == "confirmation_required":
            token = draft.get("token")
        still, _ = c.call("GET", note_path)
        record("delete-staged", status == 200 and bool(token) and still == 200,
               f"unapproved delete {_http(status, draft)}, "
               f"status={draft.get('status') if isinstance(draft, dict) else None!r}, "
               f"token issued={bool(token)}, note still there={still == 200}")
        if token:
            q = "?confirm_token=" + urllib.parse.quote(token)
            status, done = c.call("DELETE", note_path + q)
            gone, _ = c.call("GET", note_path)
            record("delete-confirmed", status == 200 and gone == 404,
                   f"approved delete {_http(status, done)}, read afterwards -> HTTP {gone}")
            status, again = c.call("DELETE", note_path + q)
            record("token-single-use", status not in (None, 200),
                   f"same token again -> {_http(status, again)}")
        else:
            blocked("delete-confirmed", "no approval token was issued")
            blocked("token-single-use", "no approval token was issued")

        status, body = c.call("GET", "/api/v1/trust/ledger?action_id="
                              + urllib.parse.quote(f"note-delete-{note_id}"))
        led = body if isinstance(body, dict) else {}
        kinds = [r.get("kind") for r in led.get("records", []) if isinstance(r, dict)]
        record("audit", status == 200 and led.get("status") == "OK" and len(kinds) >= 2
               and led.get("verified") is True,
               f"{_http(status, body)}, status={led.get('status')!r}, records={kinds}, "
               f"chain verified={led.get('verified')!r}")
    else:
        for name in ("delete-staged", "delete-confirmed", "token-single-use", "audit"):
            blocked(name, "no note was created")

    status, body = c.call("GET", "/api/v1/chat/history?session_id=" + urllib.parse.quote(session))
    msgs = body.get("messages", []) if isinstance(body, dict) else []
    asked = any(m.get("role") == "user" and question in m.get("content", "") for m in msgs)
    answered = any(m.get("role") == "assistant" and m.get("content", "").strip()
                   and m.get("content", "").strip() in state["answer"] for m in msgs)
    record("history", status == 200 and asked and answered,
           f"{_http(status, body)}, {len(msgs)} messages, question stored={asked}, "
           f"answer stored={answered}")

    # ── Clean up: the synthetic memory goes through the forget approval ──────
    _, mem_id = _vault_has(c, marker)
    if mem_id:
        mem_path = "/api/v1/memory/" + urllib.parse.quote(str(mem_id))
        status, draft = c.call("DELETE", mem_path)
        tok = draft.get("token") if isinstance(draft, dict) else None
        kept, _ = _vault_has(c, marker)
        status2 = None
        if tok:
            status2, _ = c.call("DELETE", mem_path + "?confirm_token=" + urllib.parse.quote(tok))
        gone = not _vault_has(c, marker)[0]
        record("forget", bool(tok) and kept and gone,
               f"unapproved forget {_http(status, draft)} kept={kept}; approved HTTP {status2} "
               f"gone={gone}")
    else:
        record("forget", "memory-approved" not in {r["step"] for r in results if r["result"] == "PASS"},
               "nothing to forget: no synthetic memory is in the vault")
    return _finish(results, args, base, c.sign_ins)


def _finish(results: list[dict], args, base: str, sign_ins: int = 0) -> int:
    failed = [r["step"] for r in results if r["result"] == "FAIL"]
    report = {
        "when_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "commit": _git_sha(),
        "platform": {"os": platform.system(), "release": platform.release(),
                     "arch": platform.machine(), "python": platform.python_version()},
        "base_url": base,
        "no_think": bool(getattr(args, "no_think", False)),
        "stream": bool(getattr(args, "stream", False)),
        "sign_ins": sign_ins,
        "results": results,
        "verdict": "PASS" if not failed else "FAIL",
    }
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    passed = sum(r["result"] == "PASS" for r in results)
    note = f", failed: {', '.join(failed)}" if failed else ""
    print(f"\n{report['verdict']}: {passed}/{len(results)} steps passed{note} "
          f"(commit {report['commit'][:12]}, {report['platform']['os']} "
          f"{report['platform']['arch']})", flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
