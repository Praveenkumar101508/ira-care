"""Unit tests for the Armory authorized-target whitelist (fail-closed)."""
import pytest

from armory.whitelist import extract_host, parse_whitelist


def test_extract_host_from_forms():
    assert extract_host("https://sub.example.com:8443/path") == "sub.example.com"
    assert extract_host("example.com") == "example.com"
    assert extract_host("192.0.2.10:80") == "192.0.2.10"
    assert extract_host("[::1]:9050") == "::1"
    assert extract_host("HTTP://Example.COM/") == "example.com"


def test_empty_whitelist_authorizes_nothing():
    wl = parse_whitelist("# only comments\n\n")
    assert wl.is_empty()
    assert wl.authorizes("example.com") is False
    assert wl.authorizes("127.0.0.1") is False


def test_exact_and_wildcard_hosts():
    wl = parse_whitelist("example.com\n*.internal.test\n")
    assert wl.authorizes("example.com") is True
    assert wl.authorizes("https://example.com/x") is True
    assert wl.authorizes("api.internal.test") is True
    assert wl.authorizes("internal.test") is False       # wildcard needs a label
    assert wl.authorizes("evil.com") is False


def test_ip_and_cidr():
    wl = parse_whitelist("127.0.0.1\n10.0.0.0/24\n2001:db8::/32\n")
    assert wl.authorizes("127.0.0.1") is True
    assert wl.authorizes("10.0.0.55") is True
    assert wl.authorizes("10.0.1.1") is False
    assert wl.authorizes("2001:db8::dead") is True
    assert wl.authorizes("2001:dbf::1") is False


def test_case_insensitive_and_trailing_dot():
    wl = parse_whitelist("Example.COM\n")
    assert wl.authorizes("example.com.") is True


def test_target_not_matching_any_entry_refused():
    wl = parse_whitelist("localhost\n")
    assert wl.authorizes("attacker.example") is False
    assert wl.authorizes("") is False
