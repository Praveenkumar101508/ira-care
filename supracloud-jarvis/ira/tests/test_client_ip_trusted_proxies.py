"""P2-03 — the rate limiter's identity cannot be supplied by the client.

`limiter = Limiter(key_func=get_remote_address)` keys on the socket peer. Behind
a load balancer every request arrives from the balancer, so **every client in
the world shares one rate-limit bucket**: one attacker exhausts the login budget
for everybody (denial of service), and every attacker shares one budget
(brute-force protection weakened to near-nothing).

The obvious repair — read `X-Forwarded-For` — is worse than the disease if it is
done unconditionally. That header is client-supplied. A direct caller can send
any value they like and mint a fresh rate-limit identity per request, which
removes rate limiting entirely rather than merely blunting it.

The correct answer is a **fixed, configured number of trusted hops**:

    IRA_TRUSTED_PROXY_HOPS=0   trust nothing; use the socket peer  (DEFAULT)
    IRA_TRUSTED_PROXY_HOPS=1   Client → ingress → IRA
    IRA_TRUSTED_PROXY_HOPS=2   Client → CDN → ingress → IRA

Zero is the default and the current authoritative value, because the production
topology has not been chosen. A positive value is a claim about infrastructure —
that direct access to the backend is blocked and every trusted proxy rewrites
the header — and it must not be inferred from the shape of a request.

── The indexing, stated precisely, because off-by-one here is the bug ──────

Each proxy appends **the address it received the request from**, so a proxy
never appears in the header it forwards; it appears as the next hop's socket
peer. For `client → p1 → p2 → IRA`:

    p1 appends the client   →  XFF: "client"
    p2 appends p1           →  XFF: "client, p1"
    IRA's socket peer is p2

So with `hops` trusted proxies the client is at `chain[-hops]`, and the entries
to the right of it were written by trusted proxies. Everything to the **left**
is attacker-controlled — a client may send a partial chain of its own invention
and every proxy will append to it rather than replace it.

That is why the rule is `chain[-hops]` and never "the first entry": the first
entry is exactly the part a client can forge.
"""
from __future__ import annotations

import os
import types

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from core.runtime.client_ip import (  # noqa: E402
    DEFAULT_TRUSTED_PROXY_HOPS,
    TRUSTED_PROXY_HOPS_ENV,
    client_ip,
    rate_limit_key,
    trusted_proxy_hops,
)


def _request(peer: str | None = "203.0.113.9", **headers):
    """A stand-in carrying only what the derivation reads."""
    lowered = {k.lower().replace("_", "-"): v for k, v in headers.items()}
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host=peer) if peer is not None else None,
        headers=lowered,
    )


@pytest.fixture
def hops(monkeypatch):
    def _set(value):
        monkeypatch.setenv(TRUSTED_PROXY_HOPS_ENV, str(value))
    return _set


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(TRUSTED_PROXY_HOPS_ENV, raising=False)


# ─────────────────────────────────────────────────────────────────────────────
# The default
# ─────────────────────────────────────────────────────────────────────────────


def test_the_default_is_zero_trusted_hops():
    """Zero is the authoritative value until a topology is chosen.

    A default of 1 would be a guess about infrastructure, and a wrong guess is
    a spoofable rate-limit identity rather than a misconfiguration anyone would
    notice.
    """
    assert DEFAULT_TRUSTED_PROXY_HOPS == 0
    assert trusted_proxy_hops() == 0


def test_a_direct_client_cannot_forge_its_identity_by_default():
    """The whole finding, in one assertion.

    With no trusted hops the header is not read at all, so a caller inventing
    one changes nothing about which bucket it lands in.
    """
    forged = _request(peer="198.51.100.7", x_forwarded_for="1.2.3.4")
    honest = _request(peer="198.51.100.7")

    assert client_ip(forged) == "198.51.100.7"
    assert client_ip(forged) == client_ip(honest), (
        "a client-supplied X-Forwarded-For changed the derived address with "
        "zero trusted hops — rate limiting can be escaped by sending a header")


def test_a_forged_chain_cannot_mint_a_new_bucket_per_request():
    """Rate limiting is only meaningful if the key is stable per attacker."""
    keys = {
        rate_limit_key(_request(peer="198.51.100.7",
                                x_forwarded_for=f"10.0.0.{n}"))
        for n in range(1, 20)
    }
    assert keys == {"198.51.100.7"}, (
        "nineteen forged headers produced more than one rate-limit key")


# ─────────────────────────────────────────────────────────────────────────────
# Configured hops
# ─────────────────────────────────────────────────────────────────────────────


def test_one_trusted_hop_selects_the_client(hops):
    """`Client → ingress → IRA`. The ingress appends the client, so the chain
    is one entry long and the socket peer is the ingress."""
    hops(1)
    request = _request(peer="10.0.0.1", x_forwarded_for="203.0.113.5")
    assert client_ip(request) == "203.0.113.5"


def test_two_trusted_hops_select_the_client(hops):
    """`Client → CDN → ingress → IRA`. The CDN appends the client; the ingress
    appends the CDN. The client is the second entry from the right."""
    hops(2)
    request = _request(peer="10.0.0.1",
                       x_forwarded_for="203.0.113.5, 198.51.100.30")
    assert client_ip(request) == "203.0.113.5"


def test_the_leftmost_entry_is_never_taken_blindly(hops):
    """The forged-prefix attack, and the reason the rule is not "the first one".

    A client sends its own partial chain; every proxy appends rather than
    replaces. With one trusted hop the only entry a trusted proxy wrote is the
    rightmost, and everything left of it is the caller's invention.
    """
    hops(1)
    request = _request(peer="10.0.0.1",
                       x_forwarded_for="9.9.9.9, 8.8.8.8, 203.0.113.5")
    assert client_ip(request) == "203.0.113.5", (
        "an attacker-supplied prefix was selected over the address the trusted "
        "proxy actually wrote")


def test_a_chain_shorter_than_the_configured_hops_falls_back_to_the_peer(hops):
    """Fail safe, not fail useful.

    A short chain means the request did not traverse the proxies the
    configuration claims. Reaching further left to find *something* would take
    an attacker-controlled value; the socket peer is always true.
    """
    hops(2)
    assert client_ip(_request(peer="10.0.0.1",
                              x_forwarded_for="203.0.113.5")) == "10.0.0.1"
    assert client_ip(_request(peer="10.0.0.1")) == "10.0.0.1"
    assert client_ip(_request(peer="10.0.0.1", x_forwarded_for="")) == "10.0.0.1"


# ─────────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────────


def test_ipv6_is_handled(hops):
    hops(1)
    assert client_ip(_request(peer="10.0.0.1",
                              x_forwarded_for="2001:db8::42")) == "2001:db8::42"
    # Bracketed with a port, as some proxies emit.
    assert client_ip(_request(peer="10.0.0.1",
                              x_forwarded_for="[2001:db8::42]:443")) == "2001:db8::42"


def test_ports_and_whitespace_are_normalised(hops):
    hops(1)
    for raw, expected in (
        ("  203.0.113.5  ", "203.0.113.5"),
        ("203.0.113.5:41234", "203.0.113.5"),
        ("\t203.0.113.5\n", "203.0.113.5"),
    ):
        assert client_ip(_request(peer="10.0.0.1", x_forwarded_for=raw)) == expected


def test_a_malformed_entry_falls_back_to_the_peer(hops):
    """Anything that is not an address is not an address.

    Accepting a hostname or a fragment would put unvalidated request text into
    a rate-limit key, which is the unbounded-key problem below.
    """
    hops(1)
    for junk in ("not-an-ip", "evil.example.com", "999.999.999.999",
                 "203.0.113.5 203.0.113.6", "'; DROP TABLE", "-", "::::"):
        assert client_ip(_request(peer="10.0.0.1", x_forwarded_for=junk)) == "10.0.0.1", (
            f"{junk!r} was accepted as a client address")


def test_the_key_is_never_raw_header_content(hops):
    """A rate-limit key becomes a Redis key. Unbounded caller-controlled text
    in it is a memory-growth problem as well as a correctness one."""
    hops(1)
    # A long chain whose rightmost valid entry is known, followed by junk. The
    # junk is dropped by validation, so with one trusted hop the rightmost
    # VALID entry is selected — that is correct, and it is the property to
    # assert. An earlier version of this test expected the socket peer and was
    # simply wrong about the rule.
    huge = ", ".join(f"10.0.0.{n}" for n in range(200))
    huge += ", 203.0.113.5, " + "A" * 10000
    key = rate_limit_key(_request(peer="10.0.0.1", x_forwarded_for=huge))

    assert key == "203.0.113.5", "the rightmost valid entry was not selected"
    assert len(key) < 64, "the rate-limit key is not bounded"
    assert "A" * 100 not in key, "raw header content reached the key"

    # And an entirely unparseable chain yields the peer, not a fragment of it.
    junk_only = "A" * 10000
    assert rate_limit_key(_request(peer="10.0.0.1", x_forwarded_for=junk_only)) == "10.0.0.1"


# ─────────────────────────────────────────────────────────────────────────────
# Configuration failure modes
# ─────────────────────────────────────────────────────────────────────────────


def test_invalid_configuration_fails_closed(monkeypatch):
    """Invalid, negative or nonsense values fall back to zero — the safe end.

    Falling back to a positive number would honour a header on the strength of
    a typo.
    """
    for bad in ("-1", "abc", "1.5", "", "  ", "one", "0x2", "999999999999999999999"):
        monkeypatch.setenv(TRUSTED_PROXY_HOPS_ENV, bad)
        assert trusted_proxy_hops() == 0, f"{bad!r} did not fall back to 0"


def test_a_valid_positive_value_is_honoured(monkeypatch):
    """Positive control — without it, a function returning 0 unconditionally
    would satisfy every assertion above."""
    monkeypatch.setenv(TRUSTED_PROXY_HOPS_ENV, "2")
    assert trusted_proxy_hops() == 2


def test_no_client_at_all_is_handled(hops):
    """`request.client` is None for some ASGI transports. It must not raise
    inside a rate-limit key function, where an exception is a 500 on every
    request."""
    hops(0)
    assert isinstance(client_ip(_request(peer=None)), str)
    assert isinstance(rate_limit_key(_request(peer=None)), str)
    hops(1)
    assert isinstance(
        client_ip(_request(peer=None, x_forwarded_for="203.0.113.5")), str)


# ─────────────────────────────────────────────────────────────────────────────
# Wiring and edition-independence
# ─────────────────────────────────────────────────────────────────────────────


def test_the_limiter_and_the_ip_blocklist_share_one_derivation():
    """Two consumers of "the client IP", and they must not disagree.

    `_IPBlockMiddleware` read `request.client.host` directly. If the limiter
    started honouring a proxy header and the blocklist did not, a blocked
    address would still be blocked by peer while its rate-limit identity came
    from somewhere else — two different answers to the same question.
    """
    import pathlib
    import re

    source = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "app/middleware.py").read_text(encoding="utf-8")

    assert "rate_limit_key" in source, (
        "the limiter no longer keys on the shared derivation")
    assert not re.search(r"key_func\s*=\s*get_remote_address", source), (
        "the limiter is back on the raw socket peer, so every client behind a "
        "proxy shares one bucket")
    assert "client_ip(" in source, (
        "the IP blocklist no longer uses the shared derivation")


def test_the_derivation_does_not_branch_on_edition():
    """Cloud and private must derive the client address identically.

    An edition-specific branch here would mean the private edition was tested
    and the cloud one was not, on the code path that decides who gets rate
    limited.
    """
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "core/runtime/client_ip.py").read_text(encoding="utf-8")

    assert "EDITION_CLOUD" not in source and "current_edition" not in source, (
        "client-IP derivation branches on the edition")


def test_the_forwarding_chain_is_not_logged_wholesale():
    """A full chain is caller-controlled text of unbounded length, and it goes
    into logs that are shipped and retained."""
    import pathlib
    import re

    source = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "core/runtime/client_ip.py").read_text(encoding="utf-8")

    for call in re.findall(r"logger\.\w+\([^)]*\)", source):
        assert "raw" not in call and "chain" not in call and "header" not in call, (
            f"the forwarding chain reaches a log line: {call}")
