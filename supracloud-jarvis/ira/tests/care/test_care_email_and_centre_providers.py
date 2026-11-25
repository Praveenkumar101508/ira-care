"""The email and monitoring-centre adapters (register G3.c, G3.d), against stand-ins on
this machine: a socket-level SMTP server and a local HTTP server. Nothing is sent to a
real mail server or a real centre.

Email: the mail server accepting a message is recorded as `accepted`, never
`delivered`; the subject and body (which name the person) are never logged; an address
that is not one is skipped, a server that is down is `failed`.

Monitoring centre: the request is signed (HMAC-SHA256 over timestamp and body) and
carries one idempotency key per alert; an acknowledgement is believed only if its
signature, secret and timestamp all check; both adapters are off unless configured.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from care import delivery, providers


# ── A tiny SMTP server: enough of the protocol for smtplib, nothing more ─────

class FakeSmtp:
    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.messages: list[dict] = []
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn):
        f = conn.makefile("rwb")

        def say(line):
            f.write((line + "\r\n").encode())
            f.flush()

        say("220 fake.smtp ready")
        rcpt, mail_from = [], ""
        while True:
            line = f.readline()
            if not line:
                break
            cmd = line.decode(errors="replace").strip()
            up = cmd.upper()
            if up.startswith(("EHLO", "HELO")):
                say("250 fake.smtp")
            elif up.startswith("MAIL FROM"):
                mail_from = cmd
                say("250 OK")
            elif up.startswith("RCPT TO"):
                rcpt.append(cmd.split(":", 1)[1].strip().strip("<>"))
                say("250 OK")
            elif up == "DATA":
                say("354 go ahead")
                data = []
                while True:
                    d = f.readline().decode(errors="replace")
                    if d in (".\r\n", ".\n", ""):
                        break
                    data.append(d)
                self.messages.append({"from": mail_from, "to": rcpt, "data": "".join(data)})
                rcpt = []
                say("250 queued")
            elif up == "QUIT":
                say("221 bye")
                break
            else:
                say("250 OK")
        conn.close()

    def close(self):
        self.sock.close()


@pytest.fixture
def smtp():
    server = FakeSmtp()
    yield server
    server.close()


def _provider(port):
    return providers.SmtpEmailProvider("127.0.0.1", port, "alerts@care.example.invalid", starttls=False, timeout=5)


def test_email_is_accepted_not_delivered_and_its_words_are_never_logged(smtp, caplog):
    caplog.set_level(logging.DEBUG)
    body = "Ada Lovelace asked for help from the watch. She is at 12 Example Road."
    d = asyncio.run(_provider(smtp.port).send_email("daughter@example.invalid", "IRA CARE alert for Ada", body))
    assert d.outcome == "accepted", "the server took it: that is all we know"
    assert d.outcome != "delivered"
    assert d.provider_ref.startswith("<"), "the Message-ID is kept as the reference"
    assert len(smtp.messages) == 1 and smtp.messages[0]["to"] == ["daughter@example.invalid"]
    assert "Example Road" in smtp.messages[0]["data"]
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "Ada" not in logged and "Example Road" not in logged and "daughter" not in logged


def test_an_address_that_is_not_one_is_skipped_and_nothing_is_sent(smtp):
    for bad in ("", "not-an-address", "a@b", "<script>@x.y", "x y@example.invalid"):
        d = asyncio.run(_provider(smtp.port).send_email(bad, "s", "b"))
        assert d.outcome == "skipped", bad
    assert smtp.messages == []


def test_a_mail_server_that_is_down_is_recorded_as_failed():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    d = asyncio.run(_provider(port).send_email("a@example.invalid", "s", "b"))
    assert d.outcome == "failed" and "mail server error" in d.detail


def test_email_is_off_unless_a_server_and_a_sender_are_set():
    assert providers.email_from_env({}).configured is False
    assert providers.email_from_env({"IRA_CARE_SMTP_HOST": "smtp.example.invalid"}).configured is False
    on = providers.email_from_env({"IRA_CARE_SMTP_HOST": "smtp.example.invalid",
                                   "IRA_CARE_SMTP_FROM": "alerts@example.invalid"})
    assert on.configured is True and isinstance(on, providers.SmtpEmailProvider)


# ── Monitoring centre ────────────────────────────────────────────────────────

class _Centre(BaseHTTPRequestHandler):
    received: list = []
    answer = 202

    def do_POST(self):  # noqa: N802 - the http.server name
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).received.append({"headers": dict(self.headers), "body": body})
        self.send_response(type(self).answer)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"alarm_id": "ARC-1"}')

    def log_message(self, *a):
        pass


@pytest.fixture
def centre():
    _Centre.received, _Centre.answer = [], 202
    server = HTTPServer(("127.0.0.1", 0), _Centre)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


SECRET = "arc-test-secret-only"


def test_the_centre_gets_a_signed_request_with_one_key_per_alert(centre):
    arc = providers.WebhookMonitoringCentre(f"http://127.0.0.1:{centre.server_port}/alarms", SECRET, timeout=5)
    for _ in range(2):  # tier 3 repeating: the same alarm, the same key
        d = asyncio.run(arc.raise_alarm(person_label="Ada", summary="No answer and no movement.",
                                        reference="alert-1", idempotency_key="ira-alert-alert-1",
                                        callback_url="https://care.example.invalid/cb"))
        assert d.outcome == "accepted" and d.provider_ref == "ARC-1"
    first, second = _Centre.received
    assert first["headers"]["Idempotency-Key"] == second["headers"]["Idempotency-Key"] == "ira-alert-alert-1"
    ts, sig = first["headers"]["X-IRA-Timestamp"], first["headers"]["X-IRA-Signature"]
    assert providers.verify_arc(SECRET, ts, first["body"], sig), "the centre can check it came from us"
    assert not providers.verify_arc("another-secret", ts, first["body"], sig)
    payload = json.loads(first["body"])
    assert payload["reference"] == "alert-1" and payload["callback_url"] == "https://care.example.invalid/cb"


def test_a_centre_that_refuses_is_recorded_as_failed(centre):
    _Centre.answer = 500
    arc = providers.WebhookMonitoringCentre(f"http://127.0.0.1:{centre.server_port}/alarms", SECRET, timeout=5)
    d = asyncio.run(arc.raise_alarm(person_label="Ada", summary="s", reference="r"))
    assert d.outcome == "failed" and "500" in d.detail


def test_an_acknowledgement_is_believed_only_when_signed_recent_and_unaltered():
    body = b'{"status": "handling"}'
    now = time.time()
    ts = str(int(now))
    sig = providers.arc_signature(SECRET, ts, body)
    assert providers.verify_arc(SECRET, ts, body, sig, now=now)
    assert not providers.verify_arc(SECRET, ts, b'{"status": "handling", "x": 1}', sig, now=now), "altered body"
    assert not providers.verify_arc(SECRET, ts, body, "0" * 64, now=now), "forged signature"
    assert not providers.verify_arc("", ts, body, sig, now=now), "no secret configured: nothing is believed"
    assert not providers.verify_arc(SECRET, ts, body, sig, now=now + 3600), "a captured one replayed an hour later"
    assert not providers.verify_arc(SECRET, "soon", body, sig, now=now)


def test_the_centre_is_off_unless_an_https_address_and_a_secret_are_set():
    assert providers.monitoring_centre_from_env({}).configured is False
    assert providers.monitoring_centre_from_env({"IRA_CARE_ARC_URL": "http://arc.example.invalid",
                                                 "IRA_CARE_ARC_SECRET": "s"}).configured is False
    assert providers.monitoring_centre_from_env({"IRA_CARE_ARC_URL": "https://arc.example.invalid"}).configured is False
    on = providers.monitoring_centre_from_env({"IRA_CARE_ARC_URL": "https://arc.example.invalid",
                                               "IRA_CARE_ARC_SECRET": "s"})
    assert on.configured is True and isinstance(on, providers.WebhookMonitoringCentre)


def test_the_callback_address_needs_a_public_https_base_and_a_secret():
    env = {"IRA_CARE_PUBLIC_BASE_URL": "https://care.example.invalid", "IRA_CARE_CALLBACK_SECRET": "cb-secret"}
    url = delivery.arc_callback_url("t", "w", "a", env=env)
    assert url.startswith("https://care.example.invalid/api/v1/care/providers/arc/status/t/w/a?t=")
    assert delivery.arc_callback_url("t", "w", "a", env=dict(env, IRA_CARE_PUBLIC_BASE_URL="http://x")) is None
    assert delivery.arc_callback_url("t", "w", "a", env={"IRA_CARE_PUBLIC_BASE_URL": "https://x"}) is None
