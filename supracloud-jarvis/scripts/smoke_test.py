#!/usr/bin/env python3
"""Exercise a running IRA end to end and write down what actually happened.

Run it after `start-ira.sh` / `start-ira.ps1` on any platform. It talks to the
real backend through the same origin a browser uses (the frontend on :3000,
which proxies /api and /auth), with the owner login from `.env`:

    python3 scripts/smoke_test.py                       # report to stdout
    python3 scripts/smoke_test.py --out report.json     # and keep the evidence

Checks, each recorded PASS / FAIL with what was observed:

  health      /health answers
  login       the owner signs in with the password from .env
  trust       the Trust Console reports local-only processing
  chat        a real model answers a real prompt (model name and latency kept)
  memory      a synthetic memory is saved and found again
  continuity  a follow-up in the same session refers back to the first answer
  recall      chat is asked about that memory (recorded; a small model may miss)
  forget      deleting it without approval is refused; with approval it goes
  auth-gate   the API refuses a request with no token

Standard library only: it must run on a fresh machine before anything else
works, on Windows as well as macOS and Linux. It never prints a secret — the
password is read from .env and sent only in the login request.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]


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

    def call(self, method: str, path: str, *, json_body=None, form=None, auth=True,
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
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            status = exc.code
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # No answer at all (a slow CPU-only model, or IRA stopped): record it as
            # this check's result, and let the run go on to clean up after itself.
            return None, f"no response ({getattr(exc, 'reason', exc)})"
        try:
            return status, json.loads(body)
        except ValueError:
            return status, body


def _http(status, body) -> str:
    return f"HTTP {status}" if status is not None else str(body)


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=APP_ROOT, capture_output=True,
                              text=True, timeout=10).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="End-to-end smoke test of a running IRA.")
    ap.add_argument("--base", default=None, help="default http://127.0.0.1:$FRONTEND_PORT")
    ap.add_argument("--env-file", default=str(APP_ROOT / ".env"))
    ap.add_argument("--chat-timeout", type=float, default=600.0,
                    help="seconds to wait for a CPU-only model to answer")
    ap.add_argument("--out", help="also write the JSON report here")
    args = ap.parse_args(argv)

    env = _env(Path(args.env_file))
    base = args.base or f"http://127.0.0.1:{env.get('FRONTEND_PORT', '3000')}"
    c = Client(base, timeout=60)
    results: list[dict] = []

    def record(name: str, ok: bool, observed: str, hard: bool = True, **extra) -> bool:
        results.append({"check": name, "result": "PASS" if ok else ("FAIL" if hard else "MISS"),
                        "observed": observed, **extra})
        print(f"  {'PASS' if ok else ('FAIL' if hard else 'MISS'):4}  {name:10} {observed}")
        return ok

    print(f"IRA smoke test against {base}")
    status, body = c.call("GET", "/health", auth=False)
    record("health", status == 200, _http(status, body))

    status, body = c.call("GET", "/api/v1/memory", auth=False)
    record("auth-gate", status in (401, 403), f"no token -> HTTP {status}")

    user = env.get("IRA_ADMIN_USERNAME", "admin").split("#")[0].strip() or "admin"
    status, body = c.call("POST", "/auth/token", auth=False,
                          form={"username": user, "password": env.get("IRA_ADMIN_PASSWORD", "")})
    c.token = body.get("access_token") if isinstance(body, dict) else None
    if not record("login", status == 200 and bool(c.token), f"{_http(status, body)} as {user!r}"):
        return _finish(results, args, base)

    status, body = c.call("GET", "/api/v1/trust/status")
    mode = body.get("status") if isinstance(body, dict) else None
    record("trust", status == 200 and mode == "local_only",
           f"HTTP {status}, status={mode!r}", trust=body)

    t0 = time.monotonic()
    status, body = c.call("POST", "/api/v1/chat", timeout=args.chat_timeout,
                          json_body={"message": "In one short sentence: what is two plus two?"})
    took = round(time.monotonic() - t0, 1)
    reply = body.get("response", "") if isinstance(body, dict) else ""
    model = body.get("model_used") if isinstance(body, dict) else None
    session = body.get("session_id") if isinstance(body, dict) else None
    record("chat", status == 200 and bool(reply.strip()),
           f"{_http(status, body)} in {took}s, model={model!r}: {reply.strip()[:120]!r}",
           seconds=took, model=model, agent=body.get("agent_used") if isinstance(body, dict) else None)

    # Continuity: the same session should remember its own previous turn.
    if session:
        status, body = c.call("POST", "/api/v1/chat", timeout=args.chat_timeout, json_body={
            "message": "What number did you just give me? Answer with the number only.",
            "session_id": session})
        reply = body.get("response", "") if isinstance(body, dict) else ""
        record("continuity", status == 200 and "4" in reply,
               f"{_http(status, body)}, same session: {reply.strip()[:120]!r}", hard=False)

    marker = f"smoke-{uuid.uuid4().hex[:8]}"
    fact = f"The synthetic test project codename is {marker}."
    status, body = c.call("POST", "/api/v1/memory", json_body={"content": fact, "kind": "projects"})
    mem_id = body.get("id") if isinstance(body, dict) else None
    saved = status in (200, 201) and bool(mem_id)
    status2, listed = c.call("GET", "/api/v1/memory?q=" + urllib.parse.quote(marker))
    found = isinstance(listed, dict) and any(marker in m.get("content", "")
                                             for m in listed.get("memories", []))
    record("memory", saved and found, f"save HTTP {status}, list HTTP {status2}, found={found}")

    status, body = c.call("POST", "/api/v1/chat", timeout=args.chat_timeout,
                          json_body={"message": "What is the synthetic test project codename?"})
    reply = body.get("response", "") if isinstance(body, dict) else ""
    record("recall", marker in reply, f"{_http(status, body)}: {reply.strip()[:120]!r}",
           hard=False)

    if mem_id:
        status, draft = c.call("DELETE", f"/api/v1/memory/{mem_id}")
        token = None
        if isinstance(draft, dict) and draft.get("status") == "confirmation_required":
            token = draft.get("token")
        _, still = c.call("GET", "/api/v1/memory?q=" + urllib.parse.quote(marker))
        kept = isinstance(still, dict) and any(marker in m.get("content", "")
                                               for m in still.get("memories", []))
        gated = kept and bool(token)
        status3 = None
        if token:
            status3, _ = c.call("DELETE", f"/api/v1/memory/{mem_id}?confirm_token="
                                + urllib.parse.quote(token))
        _, after = c.call("GET", "/api/v1/memory?q=" + urllib.parse.quote(marker))
        gone = isinstance(after, dict) and not any(marker in m.get("content", "")
                                                   for m in after.get("memories", []))
        record("forget", gated and gone,
               f"unapproved delete HTTP {status} kept={kept}; approved HTTP {status3} gone={gone}")
    return _finish(results, args, base)


def _finish(results: list[dict], args, base: str) -> int:
    hard_fail = [r for r in results if r["result"] == "FAIL"]
    report = {
        "when_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "commit": _git_sha(),
        "platform": {"os": platform.system(), "release": platform.release(),
                     "arch": platform.machine(), "python": platform.python_version()},
        "base_url": base,
        "results": results,
        "verdict": "PASS" if not hard_fail else "FAIL",
    }
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    passed = sum(r["result"] == "PASS" for r in results)
    missed = [r["check"] for r in results if r["result"] == "MISS"]
    note = f", missed: {', '.join(missed)}" if missed else ""
    print(f"\n{report['verdict']}: {passed}/{len(results)} checks passed{note} "
          f"(commit {report['commit'][:12]}, {report['platform']['os']} {report['platform']['arch']})")
    return 0 if not hard_fail else 1


if __name__ == "__main__":
    sys.exit(main())
