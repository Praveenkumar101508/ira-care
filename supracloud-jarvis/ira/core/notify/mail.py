"""Outbound mail (Phase 2 batch 9).

`054` minted verification tokens and dropped them on the floor, with a comment
saying so: "There is no mail transport yet, so it is dropped rather than sent —
and dropped SILENTLY, not logged: a verification token in a log file is a bearer
credential for that address." This is the transport, and it keeps that rule.

── The rule that shapes everything here ────────────────────────────────────

**A link in a log file is the credential.** Verification and reset links are
bearer secrets: anybody holding one can prove an address or set a password. So
no function in this module ever logs a message body, a link, or a token, and
`test_no_transport_logs_a_link` asserts it against every transport rather than
against the one that happened to be configured.

That is also why failures are reported by RETURN VALUE and not by exception
text. An exception carrying "could not send https://…/reset?token=abc" ends up
in a traceback, and a traceback ends up in a log.

── Three transports, and why the default is the loud one ───────────────────

    smtp      real delivery
    console   writes the RECIPIENT and SUBJECT to the log, never the body, and
              stores the message in memory for tests
    null      accepts and discards

`null` is the default, and it is the honest one. A deployment that has not
configured mail cannot deliver a reset link, and pretending otherwise — by
falling back to `console` and calling it delivered — is how a registration flow
ships looking complete while every user who needs it is stranded. `null`
reports `delivered=False` with a reason, and the callers surface that as
"temporarily unavailable" rather than as success.

── What a caller may do with the result ────────────────────────────────────

Nothing user-visible that depends on it. `request_password_reset` answers the
same way whether the address exists or not, so it must also answer the same way
whether the mail was sent or not — otherwise the response time and body become
the enumeration oracle the identical answer exists to prevent.
"""
from __future__ import annotations

import logging
import os
import ssl
from dataclasses import dataclass, field

logger = logging.getLogger("ira.notify.mail")

#: Messages the console transport has accepted, newest last. Tests read this;
#: nothing in production does. It is bounded so a long-running process with the
#: console transport configured cannot grow without limit.
_OUTBOX: list["Message"] = []
_OUTBOX_LIMIT = 200


@dataclass(frozen=True, slots=True)
class Message:
    to: str
    subject: str
    body: str
    headers: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Delivery:
    """What happened. Never carries the body or any part of it."""

    delivered: bool
    transport: str
    #: Safe to log and safe to show an operator. Says why, never what.
    reason: str = ""


def outbox() -> list[Message]:
    """The console transport's accepted messages. For tests."""
    return list(_OUTBOX)


def clear_outbox() -> None:
    _OUTBOX.clear()


def configured_transport() -> str:
    """Which transport is in effect.

    `IRA_MAIL_TRANSPORT` wins when set. Otherwise an SMTP host implies `smtp`,
    and everything else is `null` — see the module docstring on why the absence
    of configuration is not quietly upgraded to something that looks like
    delivery.
    """
    explicit = (os.getenv("IRA_MAIL_TRANSPORT") or "").strip().lower()
    if explicit in {"smtp", "console", "null"}:
        return explicit
    if explicit:
        logger.warning("unknown IRA_MAIL_TRANSPORT %r — falling back to null",
                       explicit)
        return "null"
    if _setting("smtp_host"):
        return "smtp"
    return "null"


def _setting(name: str) -> str:
    """A settings value, falling back to the environment.

    Mail configuration arrived after several deployments were already running
    from environment variables alone, so both are read rather than requiring a
    settings migration to send a single message.
    """
    try:
        from config import get_settings

        value = getattr(get_settings(), name, None)
        if value:
            return str(value)
    except Exception:  # noqa: BLE001 — settings are optional here
        pass
    return (os.getenv(name.upper()) or "").strip()


async def send(message: Message) -> Delivery:
    """Deliver one message. Never raises for a delivery failure.

    Raising would put the failure into a traceback, and a traceback goes to a
    log — which is exactly where a message carrying a reset link must not end
    up. Callers get a `Delivery` and decide.
    """
    transport = configured_transport()
    try:
        if transport == "smtp":
            return await _send_smtp(message)
        if transport == "console":
            return _send_console(message)
        return Delivery(
            delivered=False, transport="null",
            reason="no mail transport is configured, so nothing was sent")
    except Exception as exc:  # noqa: BLE001
        # `type(exc).__name__` only. An SMTP library is entitled to put the
        # recipient, the headers or the payload into its exception text, and
        # this module's whole contract is that none of that reaches a log.
        logger.warning("mail delivery failed via %s: %s",
                       transport, type(exc).__name__)
        return Delivery(delivered=False, transport=transport,
                        reason=f"the mail transport failed ({type(exc).__name__})")


def _send_console(message: Message) -> Delivery:
    """Record the message and log that it happened — never what it said."""
    _OUTBOX.append(message)
    if len(_OUTBOX) > _OUTBOX_LIMIT:
        del _OUTBOX[:-_OUTBOX_LIMIT]
    # Recipient and subject only. The subject is written by this codebase and
    # contains no secret; the body is where every link lives.
    logger.info("mail (console transport) to %s: %s", message.to, message.subject)
    return Delivery(delivered=True, transport="console")


async def _send_smtp(message: Message) -> Delivery:
    import asyncio
    import smtplib
    from email.message import EmailMessage

    host = _setting("smtp_host")
    if not host:
        return Delivery(delivered=False, transport="smtp",
                        reason="the smtp transport is selected but no host is set")

    port = int(_setting("smtp_port") or 587)
    username = _setting("smtp_username")
    password = _setting("smtp_password")
    sender = _setting("smtp_from") or username or "no-reply@localhost"
    use_tls = (_setting("smtp_use_tls") or "true").lower() not in {"0", "false", "no"}

    envelope = EmailMessage()
    envelope["To"] = message.to
    envelope["From"] = sender
    envelope["Subject"] = message.subject
    for key, value in (message.headers or {}).items():
        envelope[key] = value
    envelope.set_content(message.body)

    def _deliver() -> None:
        # `smtplib` is synchronous, so it runs in a thread. Blocking the event
        # loop on a remote SMTP handshake would stall every other request, and
        # this is called from an unauthenticated endpoint an attacker can hit.
        with smtplib.SMTP(host, port, timeout=10) as client:
            if use_tls:
                client.starttls(context=ssl.create_default_context())
            if username and password:
                client.login(username, password)
            client.send_message(envelope)

    await asyncio.to_thread(_deliver)
    return Delivery(delivered=True, transport="smtp")


__all__ = [
    "Delivery",
    "Message",
    "clear_outbox",
    "configured_transport",
    "outbox",
    "send",
]
