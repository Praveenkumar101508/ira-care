"""How an alert reaches a person: the provider interface.

Four channels (text message, phone call, email, monitoring centre), and two
kinds of implementation:

* `LogOnlyProvider`: the default. Records what WOULD have been sent, sends
  nothing, costs nothing. Every attempt is still written to
  `care_alert_attempts` with outcome `logged`, so the escalation chain can be
  proved end to end without a paid account.
* `TwilioProvider`: a real adapter, OFF unless `IRA_CARE_TWILIO_ACCOUNT_SID`,
  `IRA_CARE_TWILIO_AUTH_TOKEN` and `IRA_CARE_TWILIO_FROM` are all set. Nothing in
  this repository signs up for, buys or calls a paid service on its own.
* `SmtpEmailProvider`: email through the deployment's own mail server, OFF unless
  `IRA_CARE_SMTP_HOST` and `IRA_CARE_SMTP_FROM` are set. With it off, no email is
  even queued, so nothing changes for a deployment that has no mail server.
  A member is emailed only when they have an address in the circle.
* `WebhookMonitoringCentre`: hands an unacknowledged alert to an alarm-receiving
  centre as a signed JSON request (HMAC-SHA256), OFF unless `IRA_CARE_ARC_URL`
  (https) and `IRA_CARE_ARC_SECRET` are set. No centre is contracted (register
  G3.f): this is the interface one would plug into, tested against a stand-in.

Push notifications go through the existing notifier (`worker/notifier.py`),
not through here.

**The system never dials the emergency services.** `assert_not_emergency_number`
refuses the well-known numbers before any provider is reached, and the tests
pin it. Tier 3 is a monitoring-centre adapter when one is configured, and
otherwise a clear "call 999 now" screen for the family.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional, Protocol

logger = logging.getLogger("ira.care.providers")

EMERGENCY_NUMBERS = frozenset({"999", "112", "911", "000", "111", "110", "119", "100", "101", "102", "108"})


class EmergencyNumberRefused(ValueError):
    """Raised before any provider is asked to reach an emergency number."""


def assert_not_emergency_number(number: str) -> None:
    digits = re.sub(r"\D", "", number or "")
    if digits in EMERGENCY_NUMBERS or digits.lstrip("0") in EMERGENCY_NUMBERS:
        raise EmergencyNumberRefused(
            "IRA CARE never dials the emergency services itself. Tier 3 is a monitoring-centre "
            "adapter or a 'call 999 now' screen for the family.")


@dataclass(frozen=True)
class Delivery:
    outcome: str          # logged | sent | failed | skipped
    detail: str = ""
    provider_ref: str = ""


class MessageProvider(Protocol):
    name: str

    async def send_sms(self, to: str, text: str) -> Delivery: ...
    async def call(self, to: str, text: str) -> Delivery: ...


class MonitoringCentreProvider(Protocol):
    name: str
    configured: bool

    async def raise_alarm(self, *, person_label: str, summary: str, reference: str,
                          idempotency_key: str = "", callback_url: Optional[str] = None) -> Delivery: ...


class EmailProvider(Protocol):
    name: str
    configured: bool

    async def send_email(self, to: str, subject: str, body: str) -> Delivery: ...


class LogOnlyProvider:
    """Records, never sends. The default everywhere."""
    name = "log_only"
    configured = False

    def __init__(self) -> None:
        self.log: list[tuple[str, str, str]] = []

    async def send_sms(self, to: str, text: str, *, status_callback: Optional[str] = None) -> Delivery:
        assert_not_emergency_number(to)
        self.log.append(("sms", to, text))
        # Never the message: it names the person and says what happened to them.
        logger.info("care sms (log only) to %s: %d characters, not sent", _mask(to), len(text or ""))
        return Delivery("logged", "log-only provider: no message was sent")

    async def call(self, to: str, text: str, *, status_callback: Optional[str] = None) -> Delivery:
        assert_not_emergency_number(to)
        self.log.append(("call", to, text))
        logger.info("care call (log only) to %s: %d characters, not placed", _mask(to), len(text or ""))
        return Delivery("logged", "log-only provider: no call was placed")

    async def raise_alarm(self, *, person_label: str, summary: str, reference: str, **_) -> Delivery:
        self.log.append(("monitoring_centre", person_label, summary))
        logger.info("care monitoring centre (log only) ref %s: not raised", reference)
        return Delivery("logged", "no monitoring centre is configured")


class TwilioProvider:
    """A real text and voice adapter. Constructed only when its three settings
    are present; `provider_from_env` returns the log-only provider otherwise."""
    name = "twilio"
    configured = True

    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        self._sid, self._token, self._from = account_sid, auth_token, from_number

    async def send_sms(self, to: str, text: str, *, status_callback: Optional[str] = None) -> Delivery:
        assert_not_emergency_number(to)
        form = {"To": to, "From": self._from, "Body": text[:1500]}
        if status_callback:
            form["StatusCallback"] = status_callback
        return await self._post("Messages.json", form)

    async def call(self, to: str, text: str, *, status_callback: Optional[str] = None) -> Delivery:
        assert_not_emergency_number(to)
        twiml = f"<Response><Say voice=\"alice\">{_xml(text[:600])}</Say></Response>"
        form = {"To": to, "From": self._from, "Twiml": twiml}
        if status_callback:
            form["StatusCallback"] = status_callback
            form["StatusCallbackEvent"] = "initiated ringing answered completed"
        return await self._post("Calls.json", form)

    async def _post(self, resource: str, form: dict) -> Delivery:
        import httpx
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self._sid}/{resource}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, data=form, auth=(self._sid, self._token))
            if r.status_code // 100 == 2:
                # Taken by Twilio, which is not the same as delivered: the
                # status callback (care/delivery.py) says how far it got.
                return Delivery("accepted", "accepted by the provider; delivery not yet confirmed",
                                str(r.json().get("sid", ""))[:200])
            return Delivery("failed", f"provider answered {r.status_code}")
        except Exception as exc:  # noqa: BLE001 - a provider failure is recorded, never raised
            return Delivery("failed", f"provider error: {type(exc).__name__}")


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _mask(number: str) -> str:
    return number[:-4].replace(number[:-4], "*" * max(0, len(number) - 4)) + number[-4:] if number else ""


def provider_from_env(env: Optional[dict] = None) -> MessageProvider:
    env = os.environ if env is None else env
    sid = (env.get("IRA_CARE_TWILIO_ACCOUNT_SID") or "").strip()
    token = (env.get("IRA_CARE_TWILIO_AUTH_TOKEN") or "").strip()
    sender = (env.get("IRA_CARE_TWILIO_FROM") or "").strip()
    if sid and token and sender:
        return TwilioProvider(sid, token, sender)
    return LogOnlyProvider()


def monitoring_centre_from_env(env: Optional[dict] = None) -> MonitoringCentreProvider:
    """The webhook adapter when `IRA_CARE_ARC_URL` (https) and `IRA_CARE_ARC_SECRET`
    are set; otherwise the log-only provider, whose `configured` is False, so
    tier 3 shows the family the 'call 999 now' screen instead. No centre is
    contracted (register G3.f)."""
    env = os.environ if env is None else env
    url = (env.get("IRA_CARE_ARC_URL") or "").strip()
    secret = (env.get("IRA_CARE_ARC_SECRET") or "").strip()
    if url.startswith("https://") and secret:
        return WebhookMonitoringCentre(url, secret)
    return LogOnlyProvider()


# ── Email ────────────────────────────────────────────────────────────────────

_EMAIL = re.compile(r"^[^@\s<>]{1,64}@[^@\s<>]{1,253}\.[^@\s<>]{2,63}$")


def _mask_email(address: str) -> str:
    local, _, domain = (address or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


class NoEmailProvider:
    """No mail server configured: email is not a channel at all."""
    name = "none"
    configured = False

    async def send_email(self, to: str, subject: str, body: str) -> Delivery:
        return Delivery("skipped", "no mail server is configured")


class SmtpEmailProvider:
    """Email through the deployment's own SMTP server (STARTTLS when asked for).

    The server accepting a message is not the person reading it, nor even the
    message reaching their mailbox: the state recorded is `accepted`, never
    `delivered`. The subject and body name the person and what happened to them,
    so neither is ever logged."""
    name = "smtp"
    configured = True

    def __init__(self, host: str, port: int, sender: str, *, username: str = "", password: str = "",
                 starttls: bool = True, timeout: float = 15.0) -> None:
        self._host, self._port, self._from = host, port, sender
        self._user, self._password, self._starttls, self._timeout = username, password, starttls, timeout

    async def send_email(self, to: str, subject: str, body: str) -> Delivery:
        import asyncio
        if not _EMAIL.match(to or ""):
            return Delivery("skipped", "not a usable email address")
        try:
            message_id = await asyncio.to_thread(self._send, to, subject, body)
        except Exception as exc:  # noqa: BLE001 - a provider failure is recorded, never raised
            logger.info("care email to %s failed: %s", _mask_email(to), type(exc).__name__)
            return Delivery("failed", f"mail server error: {type(exc).__name__}")
        logger.info("care email to %s: %d characters, accepted by the mail server", _mask_email(to), len(body or ""))
        return Delivery("accepted", "accepted by the mail server; delivery not confirmed", message_id[:200])

    def _send(self, to: str, subject: str, body: str) -> str:
        import smtplib
        from email.message import EmailMessage
        from email.utils import make_msgid
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self._from, to, subject[:200]
        msg["Message-ID"] = make_msgid(domain=self._from.partition("@")[2] or None)
        msg.set_content(body[:4000])
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
            if self._starttls:
                smtp.starttls()
            if self._user:
                smtp.login(self._user, self._password)
            smtp.send_message(msg)
        return str(msg["Message-ID"])


def email_from_env(env: Optional[dict] = None) -> EmailProvider:
    env = os.environ if env is None else env
    host = (env.get("IRA_CARE_SMTP_HOST") or "").strip()
    sender = (env.get("IRA_CARE_SMTP_FROM") or "").strip()
    if not (host and sender):
        return NoEmailProvider()
    return SmtpEmailProvider(
        host, int(env.get("IRA_CARE_SMTP_PORT") or 587), sender,
        username=(env.get("IRA_CARE_SMTP_USERNAME") or "").strip(),
        password=env.get("IRA_CARE_SMTP_PASSWORD") or "",
        starttls=(env.get("IRA_CARE_SMTP_STARTTLS") or "true").strip().lower() != "false")


# ── Monitoring centre (alarm-receiving centre) ───────────────────────────────

def arc_signature(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over "<timestamp>.<body>", hex. The same scheme both ways:
    our request to the centre and the centre's acknowledgement to us."""
    import hashlib
    import hmac
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify_arc(secret: str, timestamp: str, body: bytes, signature: str, *, now: Optional[float] = None,
               max_age_s: int = 300) -> bool:
    """A signature from the centre: right secret, over this body, and recent
    (a captured acknowledgement cannot be replayed later)."""
    import hmac
    import time
    if not secret or not signature or not timestamp.isdigit():
        return False
    if abs((now if now is not None else time.time()) - int(timestamp)) > max_age_s:
        return False
    return hmac.compare_digest(arc_signature(secret, timestamp, body), signature)


class WebhookMonitoringCentre:
    """Raises an alarm with an alarm-receiving centre over HTTPS.

    The request is JSON, signed (`X-IRA-Timestamp`, `X-IRA-Signature`) and carries
    an `Idempotency-Key` that is the same for every attempt on one alert, so the
    centre opens one alarm however often tier 3 repeats. It carries the person's
    display name and the alert summary the centre needs to act, never the
    person's words. The centre answers 2xx (`accepted`) and later reports how far
    it got to `callback_url` (care/routes.py, signed the same way)."""
    name = "webhook_arc"
    configured = True

    def __init__(self, url: str, secret: str, *, timeout: float = 15.0) -> None:
        self._url, self._secret, self._timeout = url, secret, timeout

    async def raise_alarm(self, *, person_label: str, summary: str, reference: str,
                          idempotency_key: str = "", callback_url: Optional[str] = None) -> Delivery:
        import json
        import time

        import httpx
        payload = {"reference": reference, "person": person_label[:80], "summary": summary[:500],
                   "callback_url": callback_url, "raised_at": int(time.time())}
        body = json.dumps(payload, separators=(",", ":")).encode()
        ts = str(int(time.time()))
        headers = {"Content-Type": "application/json", "X-IRA-Timestamp": ts,
                   "X-IRA-Signature": arc_signature(self._secret, ts, body),
                   "Idempotency-Key": idempotency_key or f"ira-alert-{reference}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(self._url, content=body, headers=headers)
        except Exception as exc:  # noqa: BLE001 - a provider failure is recorded, never raised
            logger.info("care monitoring centre ref %s failed: %s", reference, type(exc).__name__)
            return Delivery("failed", f"monitoring centre error: {type(exc).__name__}")
        if r.status_code // 100 == 2:
            ref = ""
            try:
                ref = str(r.json().get("alarm_id", ""))[:200]
            except ValueError:
                pass
            return Delivery("accepted", "accepted by the monitoring centre; not yet confirmed handled", ref)
        return Delivery("failed", f"monitoring centre answered {r.status_code}")
