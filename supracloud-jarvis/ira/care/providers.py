"""How an alert reaches a person: the provider interface.

One interface, three channels (text message, phone call, monitoring centre),
and two kinds of implementation:

* `LogOnlyProvider`: the default. Records what WOULD have been sent, sends
  nothing, costs nothing. Every attempt is still written to
  `care_alert_attempts` with outcome `logged`, so the escalation chain can be
  proved end to end without a paid account.
* `TwilioProvider`: a real adapter, OFF unless `IRA_CARE_TWILIO_ACCOUNT_SID`,
  `IRA_CARE_TWILIO_AUTH_TOKEN` and `IRA_CARE_TWILIO_FROM` are all set. Nothing in
  this repository signs up for, buys or calls a paid service on its own.

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

    async def raise_alarm(self, *, person_label: str, summary: str, reference: str) -> Delivery: ...


class LogOnlyProvider:
    """Records, never sends. The default everywhere."""
    name = "log_only"
    configured = False

    def __init__(self) -> None:
        self.log: list[tuple[str, str, str]] = []

    async def send_sms(self, to: str, text: str) -> Delivery:
        assert_not_emergency_number(to)
        self.log.append(("sms", to, text))
        logger.info("care sms (log only) to %s: %s", _mask(to), text[:80])
        return Delivery("logged", "log-only provider: no message was sent")

    async def call(self, to: str, text: str) -> Delivery:
        assert_not_emergency_number(to)
        self.log.append(("call", to, text))
        logger.info("care call (log only) to %s: %s", _mask(to), text[:80])
        return Delivery("logged", "log-only provider: no call was placed")

    async def raise_alarm(self, *, person_label: str, summary: str, reference: str) -> Delivery:
        self.log.append(("monitoring_centre", person_label, summary))
        logger.info("care monitoring centre (log only) ref %s: %s", reference, summary[:80])
        return Delivery("logged", "no monitoring centre is configured")


class TwilioProvider:
    """A real text and voice adapter. Constructed only when its three settings
    are present; `provider_from_env` returns the log-only provider otherwise."""
    name = "twilio"
    configured = True

    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        self._sid, self._token, self._from = account_sid, auth_token, from_number

    async def send_sms(self, to: str, text: str) -> Delivery:
        assert_not_emergency_number(to)
        return await self._post("Messages.json", {"To": to, "From": self._from, "Body": text[:1500]})

    async def call(self, to: str, text: str) -> Delivery:
        assert_not_emergency_number(to)
        twiml = f"<Response><Say voice=\"alice\">{_xml(text[:600])}</Say></Response>"
        return await self._post("Calls.json", {"To": to, "From": self._from, "Twiml": twiml})

    async def _post(self, resource: str, form: dict) -> Delivery:
        import httpx
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self._sid}/{resource}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, data=form, auth=(self._sid, self._token))
            if r.status_code // 100 == 2:
                return Delivery("sent", "", str(r.json().get("sid", ""))[:200])
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
    """No monitoring-centre adapter exists yet: a contract comes first. This
    returns the log-only provider, whose `configured` is False, so tier 3
    shows the family the 'call 999 now' screen instead."""
    return LogOnlyProvider()
