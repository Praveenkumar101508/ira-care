"""Provider-neutral realtime transport, and the refusal that is the default.

IRA does not implement an SFU. A live avatar conversation needs one, so the
media path runs through somebody else's infrastructure — and that is a fact
about the user's privacy, not an implementation detail. This module is written
so that fact is impossible to lose track of:

  * the interface names no vendor;
  * the DEFAULT implementation is `DenyingTransport`, which refuses;
  * a provider is used only when it has been explicitly configured with real
    credentials, and a misconfigured provider refuses rather than half-working;
  * every grant carries the provider name and region, which get persisted, so
    the disclosure surface can say who carried the media.

── Fail closed, specifically ────────────────────────────────────────────────

`build_transport()` returns `DenyingTransport` when:

    * IRA_REALTIME_PROVIDER is unset or 'none';
    * it names a provider this build does not implement;
    * it names one that is implemented but whose credentials are missing or
      blank.

The third case is the one worth stating: a deployment that *intended* to enable
LiveKit but typo'd the secret gets a clean refusal and a session that falls to
the text floor, not a transport that appears to work and fails at the first
media packet.

── What is NOT claimed ──────────────────────────────────────────────────────

`LiveKitTransport` mints a LiveKit access token: a JWT signed HS256 over the
documented claim set. The token's structure and signature are verified in
tests. It has **not** been exchanged with a live LiveKit server in this
environment — the `livekit` SDK is not installed and no server is reachable
from CI — so "IRA connects to LiveKit" is not a claim Phase 6 makes. What is
claimed is that the token IRA issues is well-formed, correctly signed, narrowly
scoped and short-lived.

Media carried over this transport is **not** end-to-end encrypted. It is
encrypted in transit (DTLS-SRTP, as WebRTC requires) but the SFU terminates
that and can observe the media. Anything that says otherwise is wrong.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Final, Optional, Protocol, runtime_checkable

logger = logging.getLogger("ira.realtime.transport")

#: How long a join token is valid. Short: the token is handed to a browser, and
#: its only job is to get through the door once. A session that outlives it
#: renegotiates rather than holding a long-lived credential in a tab.
TOKEN_TTL_SECONDS: Final[int] = 300

#: Regions the schema will accept. A free-text region is a place to hide an
#: identifier, and anything finer than this is not needed to be truthful about
#: jurisdiction.
KNOWN_REGIONS: Final[frozenset[str]] = frozenset(
    {"unknown", "eu", "uk", "us", "apac", "other"}
)


class TransportError(RuntimeError):
    """Base for every reason a live session could not be opened."""


class TransportUnavailable(TransportError):
    """No usable transport. The caller must fall to the text floor.

    Distinct from `TransportError` because it is EXPECTED: a private-edition
    install with no provider configured raises this on every attempt, and that
    is the correct behaviour rather than an incident.
    """


@dataclass(frozen=True, slots=True)
class TransportGrant:
    """Permission to join one room, for one participant, for a few minutes.

    `token` and `url` are secrets in the sense that matters here: they are
    handed to exactly one browser and never persisted. `redacted()` is what
    goes anywhere near a log or the database.
    """

    provider: str
    region: str
    room: str
    identity: str
    url: str = field(repr=False)
    token: str = field(repr=False)
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def redacted(self) -> dict[str, object]:
        """Everything about this grant that is safe to store or log."""
        return {
            "provider": self.provider,
            "region": self.region,
            "room": self.room,
            "expires_at": self.expires_at.isoformat(),
        }

    def __str__(self) -> str:  # pragma: no cover - defensive
        return f"TransportGrant(provider={self.provider}, room={self.room})"


@runtime_checkable
class RealtimeTransport(Protocol):
    """What a realtime provider must offer. Deliberately tiny.

    Anything larger would start encoding one vendor's model of a session into
    the interface, and the point of the interface is that the vendor is
    replaceable — including by nobody, which is `DenyingTransport`.
    """

    name: str
    region: str

    def is_configured(self) -> bool:
        """True only when this transport could actually carry media."""
        ...

    def open(self, *, room: str, identity: str, can_publish: bool) -> TransportGrant:
        """Mint a join grant, or raise `TransportUnavailable`."""
        ...


class DenyingTransport:
    """The default. Refuses every session, and says why.

    This is not a stub awaiting an implementation — it is the correct transport
    for a private-edition install, which has no external provider by design and
    must never acquire one by accident.
    """

    name = "none"
    region = "unknown"

    def __init__(self, reason: str = "no realtime provider is configured") -> None:
        self._reason = reason

    def is_configured(self) -> bool:
        return False

    def open(self, *, room: str, identity: str, can_publish: bool) -> TransportGrant:
        raise TransportUnavailable(
            f"{self._reason}; the session stays on the text-only rung"
        )


class LiveKitTransport:
    """LiveKit adapter. Mints a scoped, short-lived access token.

    The token is a JWT signed HS256 with the API secret, carrying LiveKit's
    documented `video` grant. `can_publish` is passed through from the caller
    rather than defaulted to True: a participant who is only listening should
    not hold a credential that lets them publish a track.
    """

    name = "livekit"

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        api_secret: str,
        region: str = "unknown",
        ttl_seconds: int = TOKEN_TTL_SECONDS,
    ) -> None:
        self._url = (url or "").strip()
        self._api_key = (api_key or "").strip()
        self._api_secret = (api_secret or "").strip()
        self.region = region if region in KNOWN_REGIONS else "unknown"
        self._ttl = max(30, int(ttl_seconds))

    def is_configured(self) -> bool:
        """All three, non-blank. Two out of three is a misconfiguration, and a
        misconfiguration must refuse rather than produce a token nobody accepts."""
        return bool(self._url and self._api_key and self._api_secret)

    def open(self, *, room: str, identity: str, can_publish: bool) -> TransportGrant:
        if not self.is_configured():
            raise TransportUnavailable(
                "LiveKit is selected but its url/key/secret are not all set"
            )
        # PyJWT, the same library `api/middleware/auth.py` uses and the one
        # declared in requirements.txt.
        #
        # This comment used to say the opposite — "Deliberately NOT PyJWT: it is
        # not a declared dependency" — and it was correct when written. PyJWT was
        # reached for here once while only transitively installed, which worked
        # locally and refused every realtime session in CI.
        #
        # **P16-R1 changed the premise, not the rule.** The rule is still "use
        # the declared library"; python-jose was REMOVED from requirements.txt
        # because it is the only thing pulling `ecdsa` (Minerva, no fix upstream)
        # and the only thing pinning `pyasn1<0.5.0`, and PyJWT took its place as
        # a declared dependency. `tests/realtime/test_transport_fails_closed.py`
        # enforces this and was inverted in the same commit.
        import jwt

        now = int(time.time())
        expires_at = datetime.fromtimestamp(now + self._ttl, tz=timezone.utc)
        claims = {
            "iss": self._api_key,
            "sub": identity,
            "nbf": now,
            "exp": now + self._ttl,
            "jti": uuid.uuid4().hex,
            "video": {
                "room": room,
                "roomJoin": True,
                # Narrow on purpose: a participant token is not an admin token.
                "roomCreate": False,
                "roomAdmin": False,
                "roomList": False,
                "canPublish": bool(can_publish),
                "canSubscribe": True,
                "canPublishData": True,
                # Recording is off at the credential level, not just in the UI.
                # A token that cannot start an egress cannot be talked into it.
                "recorder": False,
            },
        }
        # python-jose takes `algorithm` (singular) on encode and
        # `algorithms` (plural) on decode. It returns a str.
        token = jwt.encode(claims, self._api_secret, algorithm="HS256")
        return TransportGrant(
            provider=self.name,
            region=self.region,
            room=room,
            identity=identity,
            url=self._url,
            token=token if isinstance(token, str) else token.decode("ascii"),
            expires_at=expires_at,
        )


#: Providers this build implements. A name outside this map is not a provider
#: whose support is pending — it is a name nothing knows how to honour, and it
#: resolves to the refusal.
_IMPLEMENTED: Final[frozenset[str]] = frozenset({"livekit"})


def build_transport(env: Optional[dict[str, str]] = None) -> RealtimeTransport:
    """Resolve the configured transport, or the refusal.

    Resolved from configuration, never from a request — the same rule
    `core/editions.py` states for editions, and for the same reason: a caller
    must not get to choose which infrastructure their media crosses.
    """
    source = os.environ if env is None else env
    requested = (source.get("IRA_REALTIME_PROVIDER") or "none").strip().lower()

    if requested in ("", "none", "off", "disabled"):
        return DenyingTransport("no realtime provider is configured")

    if requested not in _IMPLEMENTED:
        logger.warning(
            "IRA_REALTIME_PROVIDER=%r is not a provider this build implements "
            "(%s); refusing realtime sessions.",
            requested, ", ".join(sorted(_IMPLEMENTED)),
        )
        return DenyingTransport(f"realtime provider {requested!r} is not implemented")

    # ── Which URL, and why it matters ────────────────────────────────────────
    #
    # The URL in a grant is handed to the BROWSER, so it must be the address the
    # browser can reach. This repository already distinguishes the two, and they
    # are genuinely different hosts in the compose stack:
    #
    #   LIVEKIT_PUBLIC_URL  the browser-facing address
    #                       (docker-compose maps it to NEXT_PUBLIC_LIVEKIT_URL)
    #   LIVEKIT_URL         the internal address, e.g. ws://livekit:7880 — what
    #                       the server-side voice agent uses (voice/agent.py)
    #
    # Preferring the public one is not cosmetic: handing a browser
    # `ws://livekit:7880` produces a connection failure that looks like a
    # provider outage, on a deployment that is configured correctly. The
    # internal URL is accepted as a fallback because a single-host install often
    # sets only that one and the two addresses coincide there.
    url = (
        source.get("LIVEKIT_PUBLIC_URL", "").strip()
        or source.get("LIVEKIT_URL", "").strip()
    )
    transport = LiveKitTransport(
        url=url,
        api_key=source.get("LIVEKIT_API_KEY", ""),
        api_secret=source.get("LIVEKIT_API_SECRET", ""),
        region=(source.get("IRA_REALTIME_REGION") or "unknown").strip().lower(),
    )
    if not transport.is_configured():
        logger.warning(
            "IRA_REALTIME_PROVIDER=livekit but LIVEKIT_PUBLIC_URL (or "
            "LIVEKIT_URL) / LIVEKIT_API_KEY / LIVEKIT_API_SECRET are not all "
            "set; refusing realtime sessions rather than issuing tokens "
            "nothing will accept."
        )
        return DenyingTransport("LiveKit is selected but not fully configured")
    return transport


def token_lifetime(grant: TransportGrant) -> timedelta:
    return grant.expires_at - datetime.now(timezone.utc)


__all__ = [
    "KNOWN_REGIONS",
    "TOKEN_TTL_SECONDS",
    "DenyingTransport",
    "LiveKitTransport",
    "RealtimeTransport",
    "TransportError",
    "TransportGrant",
    "TransportUnavailable",
    "build_transport",
    "token_lifetime",
]
