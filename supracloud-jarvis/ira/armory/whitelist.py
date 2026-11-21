"""
armory/whitelist.py — the authorized-target gate.

No Armory tool runs against a target that is not explicitly authorized. This
module parses ``whitelist.txt`` (which lives encrypted in the vault) and answers
one fail-closed question: *is this target in scope?*

Supported entry forms (see the template whitelist.txt for docs):
  * exact host          ``example.com``
  * wildcard host       ``*.example.com``     (fnmatch)
  * single IP           ``192.0.2.10`` / ``::1``
  * CIDR range          ``192.0.2.0/24`` / ``2001:db8::/32``

A target string may be a bare host, an ``ip``, or a full URL; it is reduced to a
hostname/IP before matching. Anything that doesn't match at least one entry is
refused. Pure stdlib (``ipaddress``, ``fnmatch``, ``urllib``) — fully testable.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from fnmatch import fnmatch
from urllib.parse import urlparse


def extract_host(target: str) -> str:
    """Reduce a target (URL, host:port, bare host, or IP) to a host/IP token.

    ``https://sub.example.com:8443/path`` -> ``sub.example.com``;
    ``192.0.2.10:80`` -> ``192.0.2.10``; ``example.com`` -> ``example.com``.
    """
    t = (target or "").strip().lower()
    if not t:
        return ""
    if "://" in t:
        host = urlparse(t).hostname or ""
    else:
        # Strip a :port suffix, but not from a bare IPv6 literal.
        host = t
        if host.startswith("[") and "]" in host:      # [::1]:80
            host = host[1:host.index("]")]
        elif host.count(":") == 1:                      # host:port  (not IPv6)
            host = host.split(":", 1)[0]
    return host.strip().strip(".")


def _as_ip(token: str):
    try:
        return ipaddress.ip_address(token)
    except ValueError:
        return None


def _as_network(token: str):
    try:
        return ipaddress.ip_network(token, strict=False)
    except ValueError:
        return None


@dataclass
class Whitelist:
    """A parsed authorized-target scope.

    Held decrypted in RAM only while the vault is unlocked. Empty scope authorises
    nothing (fail-closed) — an empty or missing whitelist blocks every target.
    """

    hosts: frozenset[str]
    wildcards: tuple[str, ...]
    networks: tuple[Any, ...]  # ip_network objects  # noqa: F821 (Any at runtime)

    def is_empty(self) -> bool:
        return not (self.hosts or self.wildcards or self.networks)

    def authorizes(self, target: str) -> bool:
        """True iff `target` matches at least one entry. Fail-closed on empty input."""
        host = extract_host(target)
        if not host:
            return False
        ip = _as_ip(host)
        if ip is not None:
            for net in self.networks:
                try:
                    if ip in net:
                        return True
                except TypeError:
                    continue  # v4 target vs v6 network, etc.
            # An IP can also be listed as an exact host string.
            return host in self.hosts
        # Hostname path: exact match, then wildcard match.
        if host in self.hosts:
            return True
        return any(fnmatch(host, pattern) for pattern in self.wildcards)


# ``Any`` used above only as a runtime placeholder; import lazily to keep the
# annotation honest without a hard typing import at module top.
from typing import Any  # noqa: E402


def parse_whitelist(text: str | bytes) -> Whitelist:
    """Parse whitelist.txt content into a :class:`Whitelist`.

    Ignores blank lines and ``#`` comments. Case-insensitive. Unparseable entries
    are skipped (they simply authorise nothing) rather than raising, so one bad
    line cannot take the whole gate down — but the default remains fail-closed.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")

    hosts: set[str] = set()
    wildcards: list[str] = []
    networks: list[Any] = []

    for line in text.splitlines():
        entry = line.strip().lower()
        if not entry or entry.startswith("#"):
            continue
        entry = entry.strip(".")
        if "*" in entry or "?" in entry:
            wildcards.append(entry)
            continue
        if "/" in entry:
            net = _as_network(entry)
            if net is not None:
                networks.append(net)
                continue
        ip = _as_ip(entry)
        if ip is not None:
            # Store both a /32|/128 network and the literal string form.
            networks.append(_as_network(entry))
            hosts.add(entry)
            continue
        hosts.add(entry)

    return Whitelist(
        hosts=frozenset(hosts),
        wildcards=tuple(wildcards),
        networks=tuple(n for n in networks if n is not None),
    )


#: Message surfaced when a target is refused — matches the task's UX contract.
REFUSAL = ("❌ Target not in authorized scope. Update whitelist.txt to proceed.")


__all__ = ["Whitelist", "parse_whitelist", "extract_host", "REFUSAL"]
