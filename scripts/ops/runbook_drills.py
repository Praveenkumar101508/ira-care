#!/usr/bin/env python3
"""Run the operations runbook's drills that need no outside provider (docs/ops/RUNBOOKS.md).

    python3 scripts/ops/runbook_drills.py --database ira_drill --out docs/evidence/ops/drills_round10.json

On the build machine, against a migrated and seeded PostgreSQL (scripts/demo/seed_demo.py) and
Redis, with the production entry points the containers run (deploy/entrypoint.sh): the server
`uvicorn care.server:app` and the worker `python -m care.worker`. Where the runbook says
`docker compose restart worker` or `docker compose up -d api worker`, this stops and starts those
same processes; nothing else differs. Fictional people; providers log, nothing is sent.

Drills:
  2. The worker stopped: /health/jobs fails; Help pressed on the watch during the gap opens an
     alert and nothing is sent; the worker starts again; /health/jobs turns healthy and the alert
     the gap left is sent on, without anyone touching it.
  5. Key rotation:
     a. IRA_SECRET_KEY: a new value and a restart sign everyone out; signing in again works.
     b. One account's sessions: POST /auth/logout/all ends that account's access and refresh
        tokens, and nobody else's.
     c. A watch's device token: revoked by a care manager, the old token is refused; a new pairing
        code pairs it again and the new token works.
     d. BACKUP_PASSPHRASE: a backup under the new passphrase restores with it; an older one is
        refused with the new passphrase and restores with the old one; every backup file is
        private to its owner (G2.a).
  Not run: the provider secrets (Twilio, callbacks, monitoring centre, off-site keys) need the
  providers' own consoles.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IRA = ROOT / "supracloud-jarvis" / "ira"
sys.path.insert(0, str(ROOT / "scripts"))
from demo._local_state import prepare_environment  # noqa: E402

C = "/api/v1/care"


class Drills:
    def __init__(self, a):
        self.a = a
        self.port = a.port
        self.api = f"http://127.0.0.1:{a.port}"
        self.results: list[dict] = []
        self.server: subprocess.Popen | None = None
        self.worker: subprocess.Popen | None = None
        self.info = json.loads(Path(a.credentials).read_text())
        self.logs = Path(tempfile.mkdtemp(prefix="drills-"))
        prepare_environment(edition="cloud", model="fake")
        self.env = {**os.environ, "POSTGRES_DB": a.database, "POSTGRES_HOST": "127.0.0.1", "POSTGRES_USER": "ira_app",
                    "POSTGRES_PASSWORD": "ira_app", "IRA_EDITION": "cloud", "IRA_CARE_MODEL": "fake",
                    "IRA_SECRET_KEY": secrets.token_urlsafe(32), "PYTHONPATH": str(IRA),
                    # The local Redis has no password; prepare_environment fills one in for the demo modules.
                    "REDIS_PASSWORD": os.environ.get("DRILL_REDIS_PASSWORD", "")}

    # ── recording ──
    def step(self, drill: str, name: str, ok: bool, **info) -> bool:
        rec = {"drill": drill, "step": name, "ok": bool(ok), **info}
        self.results.append(rec)
        print(json.dumps(rec), flush=True)
        return ok

    # ── processes, as deploy/entrypoint.sh starts them ──
    def start_server(self) -> bool:
        log = open(self.logs / "server.log", "a")
        self.server = subprocess.Popen([sys.executable, "-m", "uvicorn", "care.server:app", "--host", "127.0.0.1",
                                        "--port", str(self.port)], cwd=IRA, env=self.env, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        for _ in range(60):
            if self.status("/health")[0] in (200, 503):
                return True
            time.sleep(1)
        return False

    def start_worker(self) -> None:
        log = open(self.logs / "worker.log", "a")
        self.worker = subprocess.Popen([sys.executable, "-m", "care.worker"], cwd=IRA, env=self.env, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)

    @staticmethod
    def stop(p: subprocess.Popen | None) -> None:
        if p is not None and p.poll() is None:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)

    # ── HTTP ──
    def call(self, method: str, path: str, body=None, headers=None, form=None):
        data, h = None, dict(headers or {})
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.api + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - loopback only
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw.decode(errors="replace")
        except OSError:
            return 0, None

    def status(self, path: str, headers=None) -> tuple[int, object]:
        return self.call("GET", path, headers=headers)

    def login(self, key: str) -> dict:
        who = next(x for x in self.info["logins"] if x.get("key") == key)
        code, tokens = self.call("POST", "/auth/cloud/login", form={"email": who["email"], "password": who["password"]})
        if code != 200:
            raise RuntimeError(f"sign-in as {key}: {code} {tokens}")
        return tokens

    def bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "X-Workspace-ID": self.info["workspace_id"]}

    def sql(self, statement: str) -> str:
        return subprocess.run(["psql", self.a.admin_dsn, "-v", "ON_ERROR_STOP=1", "-Atc", statement],
                              capture_output=True, text=True, check=True).stdout.strip()

    def wait(self, check, seconds: float, every: float = 5) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if check():
                return True
            time.sleep(every)
        return check()

    # ── section 2 ──
    def worker_stopped(self) -> None:
        d = "2. the worker stopped"
        person = self.info["people"][0]
        watch = {"X-Device-Token": person["device_token"]}
        # Nothing runs the care jobs: wait for the server's five minutes to pass.
        t0 = time.time()
        failed = self.wait(lambda: self.status("/health/jobs")[0] == 503, 420, every=10)
        self.step(d, "health_jobs_fails_while_the_worker_is_down", failed, seconds=round(time.time() - t0),
                  answer=self.status("/health/jobs")[1])
        # Help from the watch during the gap.
        code, help_ = self.call("POST", f"{C}/devices/me/help", {"via": "button", "location": None},
                                {**watch, "X-Idempotency-Key": f"drill-{secrets.token_hex(6)}"})
        alert_id = (help_ or {}).get("id") if isinstance(help_, dict) else None
        sent_before = int(self.sql(f"SELECT count(*) FROM care_alert_attempts WHERE alert_id = '{alert_id}'")) if alert_id else -1
        time.sleep(20)
        sent_still = int(self.sql(f"SELECT count(*) FROM care_alert_attempts WHERE alert_id = '{alert_id}'")) if alert_id else -1
        self.step(d, "help_in_the_gap_is_kept_and_not_yet_sent", code < 300 and bool(alert_id) and sent_before == sent_still,
                  http=code, alert=alert_id, dispatches_at_press=sent_before, dispatches_20s_later=sent_still)
        # docker compose restart worker
        t0 = time.time()
        self.start_worker()
        healthy = self.wait(lambda: self.status("/health/jobs")[0] == 200, 180)
        self.step(d, "health_jobs_turns_healthy_after_the_restart", healthy, seconds=round(time.time() - t0))
        sent = self.wait(lambda: int(self.sql(f"SELECT count(*) FROM care_alert_attempts WHERE alert_id = '{alert_id}'")) > sent_still, 180)
        rows = self.sql(f"SELECT tier || ':' || channel || ':' || outcome FROM care_alert_attempts WHERE alert_id = '{alert_id}' ORDER BY attempted_at")
        self.step(d, "the_alert_left_by_the_gap_is_sent_on", sent, seconds=round(time.time() - t0), dispatches=rows.splitlines())
        if alert_id:
            mgr = self.login("coordinator")
            self.call("POST", f"{C}/alerts/{alert_id}/resolve", {"resolution": "Runbook drill: the worker-stopped gap.", "outcome": "false_alarm"},
                      self.bearer(mgr["access_token"]))

    # ── section 5 ──
    def secret_key(self) -> None:
        d = "5a. IRA_SECRET_KEY"
        before = self.login("family")
        ok_before = self.status(f"{C}/people", self.bearer(before["access_token"]))[0] == 200
        self.stop(self.worker); self.stop(self.server)
        self.env["IRA_SECRET_KEY"] = secrets.token_urlsafe(32)
        started = self.start_server(); self.start_worker()
        old = self.status(f"{C}/people", self.bearer(before["access_token"]))[0]
        old_refresh = self.call("POST", "/auth/refresh", headers={"Authorization": f"Bearer {before['refresh_token']}"})[0]
        after = self.login("family")
        new = self.status(f"{C}/people", self.bearer(after["access_token"]))[0]
        self.step(d, "a_new_secret_signs_everyone_out_and_signing_in_again_works",
                  ok_before and started and old == 401 and old_refresh == 401 and new == 200,
                  before=ok_before, old_access=old, old_refresh=old_refresh, signed_in_again=new)

    def logout_all(self) -> None:
        d = "5b. one account's sessions"
        a1, a2, other = self.login("family"), self.login("family"), self.login("viewer")
        code = self.call("POST", "/auth/logout/all", headers=self.bearer(a1["access_token"]))[0]
        s1 = self.status(f"{C}/people", self.bearer(a1["access_token"]))[0]
        s2 = self.status(f"{C}/people", self.bearer(a2["access_token"]))[0]
        r2 = self.call("POST", "/auth/refresh", headers={"Authorization": f"Bearer {a2['refresh_token']}"})[0]
        so = self.status(f"{C}/people", self.bearer(other["access_token"]))[0]
        self.step(d, "logout_all_ends_every_session_of_that_account_and_no_other",
                  code < 300 and s1 == 401 and s2 == 401 and r2 == 401 and so == 200,
                  logout_all=code, first_session=s1, second_session=s2, second_refresh=r2, other_account=so)

    def device_token(self) -> None:
        d = "5c. a watch's device token"
        person = self.info["people"][0]
        mgr = self.bearer(self.login("coordinator")["access_token"])
        old = {"X-Device-Token": person["device_token"]}
        me_before = self.status(f"{C}/devices/me", old)
        device_id = (me_before[1] or {}).get("id") if isinstance(me_before[1], dict) else None
        revoked = self.call("POST", f"{C}/devices/{device_id}/revoke", headers=mgr)[0] if device_id else 0
        old_after = self.status(f"{C}/devices/me", old)[0]
        code, created = self.call("POST", f"{C}/people/{person['id']}/devices", {"platform": "wearos", "model": "runbook drill"}, mgr)
        pairing = (created or {}).get("pairing_code") if isinstance(created, dict) else None
        pcode, paired = self.call("POST", f"{C}/devices/pair", {"pairing_code": pairing or "", "app_version": "drill"})
        new_token = (paired or {}).get("device_token") if isinstance(paired, dict) else None
        new_me = self.status(f"{C}/devices/me", {"X-Device-Token": new_token or ""})[0]
        self.step(d, "revoked_token_refused_and_a_new_pairing_works",
                  me_before[0] == 200 and revoked < 300 and old_after in (401, 403) and code == 201 and pcode == 200 and new_me == 200,
                  before=me_before[0], revoke=revoked, old_token_after=old_after, new_device=code, pair=pcode, new_token=new_me)
        # The seed's file is updated so later runs on this database use the new token.
        if new_token:
            person["device_token"] = new_token
            Path(self.a.credentials).write_text(json.dumps(self.info, indent=1) + "\n")
            os.chmod(self.a.credentials, 0o600)

    def backup_passphrase(self) -> None:
        d = "5d. BACKUP_PASSPHRASE"
        work = Path(tempfile.mkdtemp(prefix="drill-backups-"))
        old_p, new_p = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        dsn = self.a.admin_dsn

        def backup(passphrase: str, where: Path) -> Path:
            where.mkdir(parents=True, exist_ok=True)
            subprocess.run(["sh", str(ROOT / "deploy" / "backup.sh")], check=True, capture_output=True, text=True,
                           env={**os.environ, "BACKUP_PASSPHRASE": passphrase, "BACKUP_DIR": str(where), "BACKUP_DSN": dsn})
            return next(where.glob("ira-care-*.sql.gz.enc"))

        def restore(path: Path, passphrase: str) -> tuple[int, str]:
            target = f"ira_drill_restore_{secrets.token_hex(3)}"
            self.sql(f"CREATE DATABASE {target}")
            try:
                r = subprocess.run(["sh", str(ROOT / "deploy" / "restore.sh"), str(path), dsn.rsplit("/", 1)[0] + "/" + target],
                                   capture_output=True, text=True, env={**os.environ, "BACKUP_PASSPHRASE": passphrase})
                tables = self.sql_on(target, "SELECT count(*) FROM schema_migrations") if r.returncode == 0 else ""
                return r.returncode, tables or (r.stderr.strip().splitlines() or [""])[-1][:160]
            finally:
                self.sql(f"DROP DATABASE IF EXISTS {target}")

        older = backup(old_p, work / "before")
        newer = backup(new_p, work / "after")
        new_ok = restore(newer, new_p)
        old_with_new = restore(older, new_p)
        old_with_old = restore(older, old_p)
        migrations = self.sql("SELECT count(*) FROM schema_migrations")
        self.step(d, "new_backups_need_the_new_passphrase_older_ones_the_old",
                  new_ok[0] == 0 and new_ok[1] == migrations and old_with_new[0] != 0 and old_with_old[0] == 0 and old_with_old[1] == migrations,
                  migrations=migrations, new_with_new=new_ok, old_with_new=old_with_new, old_with_old=old_with_old)
        modes = {p.name: oct(stat.S_IMODE(p.stat().st_mode)) for p in sorted(work.rglob("ira-care-*"))}
        private = all(int(m, 8) & 0o077 == 0 for m in modes.values())
        self.step(d, "every_backup_file_is_private_to_its_owner", private and bool(modes), modes=modes)
        shutil.rmtree(work, ignore_errors=True)

    def sql_on(self, database: str, statement: str) -> str:
        dsn = self.a.admin_dsn.rsplit("/", 1)[0] + "/" + database
        return subprocess.run(["psql", dsn, "-Atc", statement], capture_output=True, text=True).stdout.strip()

    def run(self) -> int:
        t0 = time.time()
        try:
            if not self.step("setup", "server_started", self.start_server(), entry="uvicorn care.server:app"):
                return self.finish(t0)
            self.worker_stopped()
            self.secret_key()
            self.logout_all()
            self.device_token()
            self.backup_passphrase()
        finally:
            self.stop(self.worker); self.stop(self.server)
        return self.finish(t0)

    def finish(self, t0: float) -> int:
        ok = bool(self.results) and all(r["ok"] for r in self.results)
        out = {"what": "docs/ops/RUNBOOKS.md sections 2 and 5, run on the build machine", "ok": ok,
               "passed": sum(r["ok"] for r in self.results), "steps": len(self.results),
               "seconds": round(time.time() - t0), "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "processes": {"server": "python -m uvicorn care.server:app", "worker": "python -m care.worker"},
               "not_run": ["IRA_CARE_TWILIO_AUTH_TOKEN", "IRA_CARE_CALLBACK_SECRET", "IRA_CARE_ARC_SECRET",
                           "OFFSITE_S3_ACCESS_KEY / OFFSITE_S3_SECRET_KEY: each needs its provider's console"],
               "results": self.results}
        Path(self.a.out).write_text(json.dumps(out, indent=1) + "\n")
        print(json.dumps({k: out[k] for k in ("ok", "passed", "steps", "seconds")}))
        return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--database", required=True, help="a migrated, seeded database")
    ap.add_argument("--admin-dsn", default="", help="the database as its owner (default: postgres on 127.0.0.1)")
    ap.add_argument("--credentials", default=str(ROOT / ".demo" / "demo.local.json"), help="seed_demo.py --out file")
    ap.add_argument("--port", type=int, default=8020)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    a.admin_dsn = a.admin_dsn or f"postgresql://postgres:postgres@127.0.0.1:5432/{a.database}"
    return Drills(a).run()


if __name__ == "__main__":
    raise SystemExit(main())
