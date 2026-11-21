"""P16-05 — FINDING AK: the Ollama probe's URL scheme is checked, not asserted.

`_default_fetch` carried `# noqa: S310 - localhost only` over a `urlopen` whose
URL is built from `IRA_OLLAMA_BASE_URL` / `OLLAMA_BASE_URL`. Nothing constrained
either. `urlopen` honours `file:`, so `IRA_OLLAMA_BASE_URL=file:///etc` made a
model-availability probe read the filesystem.

The suppression's stated reason was also false about this repository's own
deployments: `docker-compose.portable.yml` sets `http://ollama:11434/v1` and
`docker-compose.test.yml` sets `http://host.docker.internal:11434/v1`. Neither
is localhost. The comment described a property that was not true, was not
enforced, and could not have been enforced without breaking the shipped
configuration — a declaration standing in for a check, which is this phase's
recurring failure class.

So what is enforced is the constraint that is both true and load-bearing: a
network scheme.
"""
from __future__ import annotations

import urllib.request

import pytest

from reasoning import model_availability as ma


@pytest.mark.parametrize("host", [
    "file:///etc",
    "ftp://example.invalid/x",
    "data:text/plain,{}",
    "gopher://example.invalid",
])
def test_non_network_schemes_are_refused(host: str, monkeypatch) -> None:
    """The refusal happens BEFORE urlopen, not by urlopen failing."""
    def _explode(*args, **kwargs):  # pragma: no cover — must never be reached
        raise AssertionError(f"urlopen was reached with {args!r}")

    monkeypatch.setattr(urllib.request, "urlopen", _explode)

    url = ma._ollama_host({"IRA_OLLAMA_BASE_URL": host}) + "/api/tags"
    with pytest.raises(ValueError, match="refusing to fetch"):
        ma._default_fetch(url, timeout=0.1)


def test_a_refused_scheme_degrades_to_unknown_not_to_empty(monkeypatch) -> None:
    """`reachable=False` — "can't tell" — never "no models are installed".

    The router treats `reachable=False` as "don't override the configured
    model". If a bad scheme produced `reachable=True, models=frozenset()`
    instead, a misconfiguration would silently downgrade every routing
    decision — a failure mapped to a benign-looking value, which §4 forbids.
    """
    def _explode(*args, **kwargs):  # pragma: no cover
        raise AssertionError("urlopen was reached")

    monkeypatch.setattr(urllib.request, "urlopen", _explode)

    result = ma.probe_availability(env={"IRA_OLLAMA_BASE_URL": "file:///etc"},
                                   timeout=0.1)
    assert result.reachable is False
    assert result.models == frozenset()
    assert result.error and "refusing to fetch" in result.error


@pytest.mark.parametrize("host", [
    "http://localhost:11434",
    "http://ollama:11434",              # docker-compose.portable.yml
    "http://host.docker.internal:11434",  # docker-compose.test.yml
    "https://ollama.internal:11434",
])
def test_the_schemes_real_deployments_use_are_permitted(host: str, monkeypatch) -> None:
    """The control must not break the configurations this repository ships.

    A check that refused everything would pass the test above while making the
    product unable to find its own models.
    """
    seen: list[str] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"models": [{"name": "llama3:8b"}]}'

    def _fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    url = ma._ollama_host({"IRA_OLLAMA_BASE_URL": host}) + "/api/tags"
    payload = ma._default_fetch(url, timeout=0.1)

    assert payload == {"models": [{"name": "llama3:8b"}]}
    assert seen == [f"{host}/api/tags"]


def test_the_default_host_survives_the_check() -> None:
    """The module's own default must not be refused by its own control."""
    assert ma._DEFAULT_OLLAMA_HOST.split(":", 1)[0] in ma._ALLOWED_FETCH_SCHEMES
