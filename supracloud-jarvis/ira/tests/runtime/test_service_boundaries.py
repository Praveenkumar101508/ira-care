"""Service boundaries, enforced rather than documented.

Phase 0 established by inspection that no Compose file publishes PostgreSQL,
Redis or Ollama, and that every published port is loopback-bound. That was true
on the day it was checked, and nothing stopped it becoming false the next day —
publishing a port is a one-line change that looks helpful ("so I can connect
from my host") and is the difference between a private model runtime and an
unauthenticated inference endpoint on the internet.

These tests make that regression fail in CI instead of in a disclosure. They
parse the Compose files as data; they do not start anything, and they do not
need a Docker daemon.

The properties enforced:

  * no service binds 0.0.0.0 or a public interface;
  * PostgreSQL, Redis and the model runtimes publish nothing at all where the
    edition does not require it;
  * no privileged container;
  * the Docker socket is never mounted — a container with it is root on the host;
  * no real credential is committed in a Compose file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

# tests/runtime/ -> tests/ -> ira/ -> supracloud-jarvis/
ROOT = Path(__file__).resolve().parents[3]

COMPOSE_FILES = [
    ROOT / "docker-compose.cloud.yml",
    ROOT / "docker-compose.portable.yml",
    ROOT / "docker-compose.test.yml",
]

#: Services that must never be reachable from outside the container network.
#: A published port on any of these is the finding.
PRIVATE_SERVICES = frozenset({
    "postgres", "postgresql", "db", "database",
    "redis", "cache",
    "ollama",
})


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _services(path: Path) -> dict:
    return _load(path).get("services") or {}


def _ports(service: dict) -> list[str]:
    return [str(p) for p in (service.get("ports") or [])]


def _binding_host(port_spec: str) -> str:
    """Host interface a published port binds to.

    Compose port syntax:
        "8000"                  -> container only, no host binding
        "8000:8000"             -> ALL interfaces (0.0.0.0) — public
        "127.0.0.1:8000:8000"   -> loopback only
    """
    parts = port_spec.split(":")
    if len(parts) >= 3:
        return parts[0]
    if len(parts) == 2:
        # "host:container" with no interface — Docker binds every interface.
        return "0.0.0.0"
    return ""  # not published to the host at all


LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_compose_file_exists_and_parses(path):
    assert path.is_file(), f"missing {path}"
    assert _services(path), f"{path.name} declares no services"


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_no_service_is_published_on_a_public_interface(path):
    """`"8000:8000"` binds 0.0.0.0. It must be `"127.0.0.1:8000:8000"`."""
    offenders = []
    for name, service in _services(path).items():
        for spec in _ports(service or {}):
            host = _binding_host(spec)
            if host and host not in LOOPBACK:
                offenders.append(f"{name}: {spec} binds {host}")
    assert not offenders, (
        f"{path.name} publishes on a non-loopback interface: {offenders}. "
        "Use 127.0.0.1:<host>:<container>, or drop the mapping entirely."
    )


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_datastores_and_model_runtimes_publish_no_ports(path):
    """PostgreSQL, Redis and Ollama are reachable only over the internal network.

    Ollama in particular: port 11434 has no authentication. Published, it is an
    open inference endpoint — a data-exfiltration path and an unbounded bill.
    """
    offenders = []
    for name, service in _services(path).items():
        if name.lower() in PRIVATE_SERVICES and _ports(service or {}):
            offenders.append(f"{name}: {_ports(service)}")
    assert not offenders, (
        f"{path.name} publishes a private service: {offenders}. "
        "These must be reachable only from inside the Compose network."
    )


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_no_privileged_containers(path):
    offenders = [n for n, s in _services(path).items() if (s or {}).get("privileged")]
    assert not offenders, f"{path.name} runs privileged containers: {offenders}"


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_the_docker_socket_is_never_mounted(path):
    """A container with /var/run/docker.sock is root on the host."""
    offenders = []
    for name, service in _services(path).items():
        for volume in (service or {}).get("volumes") or []:
            if "docker.sock" in str(volume):
                offenders.append(f"{name}: {volume}")
    assert not offenders, f"{path.name} mounts the Docker socket: {offenders}"


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_no_literal_credentials_in_compose(path):
    """Secrets come from the environment; a Compose file is tracked in git.

    Values of the form `${VAR}` or `${VAR:-default}` are interpolation, not
    literals, and are what the files should contain.
    """
    text = path.read_text(encoding="utf-8")
    offenders = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key_l = key.lower()
        if not any(k in key_l for k in ("password", "secret", "token", "api_key", "apikey")):
            continue
        value = value.strip().strip('"').strip("'")
        if not value:
            continue
        if value.startswith("${") or value.startswith("$"):
            continue  # environment interpolation — correct
        offenders.append(f"L{lineno}: {key.strip()}")
    assert not offenders, (
        f"{path.name} appears to contain literal credentials at {offenders}. "
        "Use ${VAR} interpolation."
    )


def test_datastores_declare_healthchecks():
    """Compose waits on a healthcheck before starting dependants.

    Without one, the API starts against a PostgreSQL that is still in recovery
    and fails its first queries — the class of startup flake that gets
    "fixed" with a sleep.
    """
    path = ROOT / "docker-compose.portable.yml"
    services = _services(path)
    for name in ("postgres", "redis", "ollama"):
        if name in services:
            assert (services[name] or {}).get("healthcheck"), \
                f"{name} in {path.name} has no healthcheck"


# ── Environment templates ────────────────────────────────────────────────────

ENV_TEMPLATES = [ROOT / ".env.cloud.example", ROOT / ".env.test.example"]


@pytest.mark.parametrize("path", ENV_TEMPLATES, ids=lambda p: p.name)
def test_env_template_exists_and_parses(path):
    assert path.is_file(), f"missing {path}"


def test_the_cloud_template_ships_no_filled_credentials():
    """`.env.cloud.example` is tracked and public. Every credential in it must
    be empty — this is the exact regression that put six real credentials into
    this repository's public history (docs/security/CREDENTIAL_ROTATION.md)."""
    path = ROOT / ".env.cloud.example"
    must_be_empty = (
        "IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
        "REDIS_PASSWORD", "VLLM_API_KEY", "IRA_DOMAIN",
    )
    filled = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() in must_be_empty and value.strip():
            filled.append(key.strip())
    assert not filled, f".env.cloud.example ships non-empty credentials: {filled}"


def test_the_cloud_template_does_not_enable_dev_mode():
    """DEV_MODE bypasses authentication for every request."""
    text = (ROOT / ".env.cloud.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("DEV_MODE="):
            assert line.strip().split("=", 1)[1].strip().lower() in ("false", ""), \
                "DEV_MODE must not be enabled in the cloud template"


def test_the_cloud_template_points_the_model_runtime_at_a_private_host():
    """A public model endpoint is refused at startup; the template must not
    suggest one."""
    from core.runtime.environment import _is_public_url

    text = (ROOT / ".env.cloud.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() in ("OLLAMA_BASE_URL", "VLLM_FAST_URL", "VLLM_DEEP_URL"):
            value = value.strip()
            if value:
                assert not _is_public_url(value), f"{key.strip()} is a public host"
