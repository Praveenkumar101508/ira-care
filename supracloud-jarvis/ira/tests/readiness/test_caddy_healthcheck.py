"""Caddy's container health check can pass (round 7).

The check used to fetch http://127.0.0.1:80/health. Caddy redirects every
plain-HTTP request for the site to HTTPS, so wget followed the redirect to
https://127.0.0.1/health, and the TLS handshake failed: there is no
certificate for an IP address. The container was never healthy, even while it
served every request correctly.

The check now asks Caddy's own admin API (localhost:2019 inside the container,
never published) whether the site's HTTP server is loaded. It was verified
against the caddy:2-alpine image: healthy with the repository's Caddyfile;
the same command fails (HTTP 400) when Caddy runs with no site loaded; HTTPS
answers for real traffic (redirect, status codes, HSTS) were identical before
and after.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
DEPLOY = ROOT / "deploy"


def _caddy() -> dict:
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())["services"]["caddy"]


def _command(test) -> str:
    assert isinstance(test, list) and test[0] in ("CMD", "CMD-SHELL"), test
    return " ".join(test[1:])


def check_is_sound(test) -> list[str]:
    """What is wrong with a Caddy health check command; empty when nothing is."""
    cmd = _command(test)
    problems = []
    if "http://127.0.0.1:2019/" not in cmd and "http://localhost:2019/" not in cmd:
        problems.append("does not ask Caddy's admin API on localhost:2019")
    if "/config/apps/http/servers" not in cmd:
        problems.append("does not check that the site's HTTP server is loaded")
    if "grep -q" not in cmd:
        problems.append("does not look at the answer, so an empty config would pass")
    for bad in ("127.0.0.1:80", "127.0.0.1/", "https://", ":443"):
        if bad in cmd:
            problems.append(f"goes through the site ({bad}): plain HTTP is redirected to HTTPS, which has no certificate for an IP")
    return problems


def test_the_caddy_health_check_asks_the_admin_api_not_the_redirected_site():
    hc = _caddy()["healthcheck"]
    assert check_is_sound(hc["test"]) == []
    assert hc["test"][0] == "CMD-SHELL", "a pipe needs a shell"


def test_the_admin_api_is_not_published_or_turned_off():
    svc = _caddy()
    assert all("2019" not in str(p) for p in svc.get("ports", [])), "the admin API stays inside the container"
    caddyfile = (DEPLOY / "Caddyfile").read_text()
    assert "admin off" not in caddyfile, "the health check needs the admin API"
    assert "admin " not in caddyfile.split("{$IRA_CARE_DOMAIN}")[0], "the admin API keeps its default, localhost only"


def test_the_round_6_command_is_refused_by_this_check():
    """The negative control: the command that could never pass."""
    old = ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:80/health"]
    assert check_is_sound(old) != []
