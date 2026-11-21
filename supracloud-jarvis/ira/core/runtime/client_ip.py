"""Who the client is, for rate limiting and the IP blocklist (P2-03).

`Limiter(key_func=get_remote_address)` keys on the socket peer. Behind a load
balancer every request arrives from the balancer, so **every client shares one
bucket**: one attacker exhausts the login budget for everybody, and every
attacker shares one budget.

Reading `X-Forwarded-For` unconditionally is worse. It is client-supplied, so a
direct caller mints a fresh identity per request and rate limiting stops
existing rather than merely blunting.

So the header is honoured only for a **fixed, configured number of trusted
hops**, and the default is zero.

    IRA_TRUSTED_PROXY_HOPS=0   trust nothing; use the socket peer   (DEFAULT)
    IRA_TRUSTED_PROXY_HOPS=1   Client → ingress → IRA
    IRA_TRUSTED_PROXY_HOPS=2   Client → CDN → ingress → IRA

Zero is the current authoritative value because the production topology has not
been chosen. Set a positive value only when **all** of these hold:

  1. every trusted proxy rewrites the forwarding header rather than blindly
     appending to whatever the caller sent;
  2. direct public access to this backend is blocked;
  3. the number of controlled hops is fixed, not variable;
  4. infrastructure tests prove a spoofed header cannot change the derived
     address.

── The indexing, because off-by-one here is the vulnerability ──────────────

Each proxy appends **the address it received the request from**. A proxy
therefore never appears in the header it forwards — it appears as the next
hop's socket peer. For `client → p1 → p2 → IRA`:

    p1 appends the client   →  XFF: "client"
    p2 appends p1           →  XFF: "client, p1"
    IRA's socket peer is p2 — which appears nowhere in the header

With `hops` trusted proxies the client is at `chain[-hops]`. Entries to the
right of it were written by trusted proxies; everything to the **left** is
attacker-controlled, because a caller may send a partial chain of its own and
each proxy appends rather than replaces.

That is why the rule is never "the first entry" — the first entry is precisely
the part a client can forge.

**Note for review:** the brief's worked example showed a header
`client, proxy-1, proxy-2` read with two trusted hops. That shape implies three
proxies in front (the third being the socket peer). The rule implemented here is
the one that matches the two topologies the brief actually names, verified in
`tests/test_client_ip_trusted_proxies.py`. Confirm against the real topology
before setting a positive value.

── Configuration is read from the environment, not `Settings` ──────────────

Deliberately, and the same way `api/routes/brain.py::brain_enabled` does. This
function is called on **every request** from a rate-limit key function, where an
exception is a 500 rather than a degraded response — so it must not depend on
`Settings` validation succeeding, and must not raise for any input.
"""
from __future__ import annotations

import ipaddress
import logging
import os

logger = logging.getLogger("ira.client_ip")

#: Environment variable naming how many proxy hops in front of this process are
#: controlled by the operator.
TRUSTED_PROXY_HOPS_ENV = "IRA_TRUSTED_PROXY_HOPS"

#: Trust nothing. A default of 1 would be a guess about infrastructure, and a
#: wrong guess is a spoofable rate-limit identity rather than a visible
#: misconfiguration.
DEFAULT_TRUSTED_PROXY_HOPS = 0

#: Returned when there is genuinely no peer — some ASGI transports have no
#: `request.client`. A constant, so it groups rather than fragmenting into
#: unbounded keys.
UNKNOWN_CLIENT = "unknown"

#: A sane ceiling. Nobody has forty controlled proxies, and a huge value would
#: make the derivation reach arbitrarily far left into caller-controlled text.
_MAX_HOPS = 8

_warned: set[str] = set()


def _warn_once(message: str) -> None:
    """Log a configuration problem once rather than on every request.

    This runs per request; an unconditional warning would be a log flood driven
    by a single typo.
    """
    if message not in _warned:
        _warned.add(message)
        logger.warning(message)


def trusted_proxy_hops() -> int:
    """How many proxy hops in front of this process are trusted.

    Falls back to `DEFAULT_TRUSTED_PROXY_HOPS` for anything invalid — missing,
    empty, negative, non-integer, or absurdly large. The fallback direction is
    the safe one: an unreadable value must never be interpreted as permission to
    honour a client-supplied header.
    """
    raw = os.getenv(TRUSTED_PROXY_HOPS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_TRUSTED_PROXY_HOPS
    try:
        hops = int(raw.strip())
    except ValueError:
        _warn_once(
            f"{TRUSTED_PROXY_HOPS_ENV} is not an integer — falling back to "
            f"{DEFAULT_TRUSTED_PROXY_HOPS} (no forwarding headers trusted)")
        return DEFAULT_TRUSTED_PROXY_HOPS
    if hops < 0 or hops > _MAX_HOPS:
        _warn_once(
            f"{TRUSTED_PROXY_HOPS_ENV} is out of range (0..{_MAX_HOPS}) — "
            f"falling back to {DEFAULT_TRUSTED_PROXY_HOPS}")
        return DEFAULT_TRUSTED_PROXY_HOPS
    return hops


def _normalise(entry: str) -> str | None:
    """One chain entry → a validated address, or None.

    Strips whitespace, `[...]` brackets and a trailing port, then **validates**
    with `ipaddress`. Validation is what keeps unbounded caller text out of a
    rate-limit key: anything that is not an address is rejected outright rather
    than passed through.
    """
    value = entry.strip()
    if not value:
        return None

    # "[2001:db8::42]:443" and "[2001:db8::42]"
    if value.startswith("["):
        closing = value.find("]")
        if closing == -1:
            return None
        value = value[1:closing]
    elif value.count(":") == 1:
        # "203.0.113.5:41234" — a single colon means IPv4 with a port. A bare
        # IPv6 address has several, so it is left alone.
        value = value.rsplit(":", 1)[0]

    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def client_ip(request) -> str:
    """The address to attribute this request to.

    Never raises: it is called from a rate-limit key function on every request,
    where an exception would be a 500 rather than a degraded answer.
    """
    try:
        peer = getattr(getattr(request, "client", None), "host", None) or ""
        hops = trusted_proxy_hops()
        if hops <= 0:
            return peer or UNKNOWN_CLIENT

        headers = getattr(request, "headers", None) or {}
        raw = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For") or ""
        chain = [c for c in (_normalise(p) for p in raw.split(",")) if c]

        # A chain shorter than the configured hops means the request did not
        # traverse the proxies the configuration claims. Reaching further left
        # would take an attacker-controlled value; the peer is always true.
        if len(chain) < hops:
            return peer or UNKNOWN_CLIENT

        return chain[-hops]
    except Exception:  # noqa: BLE001 — a key function must not raise
        logger.debug("client_ip derivation failed; falling back to peer")
        try:
            return getattr(getattr(request, "client", None), "host", None) or UNKNOWN_CLIENT
        except Exception:  # noqa: BLE001
            return UNKNOWN_CLIENT


def rate_limit_key(request) -> str:
    """`key_func` for slowapi.

    Separate from `client_ip` so the limiter's key can gain a prefix or a scope
    later without changing what the IP blocklist considers the client to be.
    The value is a validated address, so it is bounded by construction.
    """
    return client_ip(request)


__all__ = [
    "TRUSTED_PROXY_HOPS_ENV",
    "DEFAULT_TRUSTED_PROXY_HOPS",
    "UNKNOWN_CLIENT",
    "trusted_proxy_hops",
    "client_ip",
    "rate_limit_key",
]
