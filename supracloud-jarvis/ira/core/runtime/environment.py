"""Which environment this process is running in, and what that forbids.

Four environments, resolved ONCE from configuration and never from a request:

    development  local work; permissive defaults are acceptable
    test         automated tests; like development, with placeholders allowed
    staging      production-shaped; production rules apply
    production   the real thing; every rule below is enforced

Unset means ``development``, so an existing private-edition install keeps its
current behaviour exactly. Nothing here activates until ``IRA_ENVIRONMENT`` is
set.

── Why this fails closed rather than warning ────────────────────────────────

`app/lifespan.py` already validates configuration at startup, and every finding
it produces is a `logger.warning`. That is right for a single-owner install on a
laptop: the owner reads the log, sees the warning and fixes it. It is wrong for
a cloud deployment, where nobody reads the startup log of the instance that
autoscaled up at 3am — a warning that a fixed JWT secret is in use is
indistinguishable, operationally, from no warning at all.

So the warnings stay (they remain useful in development), and this module adds a
second check that **raises** in staging and production. A misconfigured cloud
instance must fail to start rather than start insecurely: an instance that
refuses to boot is a page, and an instance that boots with a default admin
password is a breach.

── What is forbidden in production ─────────────────────────────────────────

Each rule below exists because the opposite has been a real incident somewhere:

    DEV_MODE                 bypasses auth globally
    placeholder secrets      the six credentials in this repo's own history
    default admin password   the first thing an attacker tries
    weak/short JWT secret    forge any token
    permissive CORS          any origin reads authenticated responses
    exposed model runtime    an unauthenticated inference endpoint on the internet

The check is deliberately cheap and deterministic — no network calls, no
filesystem access — so it can run in a container's startup path without adding
latency or a new failure mode of its own.
"""
from __future__ import annotations

import os
import re
from enum import Enum
from typing import Iterable

#: Environment variable that selects the environment.
ENV_VAR = "IRA_ENVIRONMENT"


class Environment(str, Enum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_production_like(self) -> bool:
        """Staging is production-shaped, so it obeys production rules.

        A staging environment that is allowed to be sloppy is not a rehearsal
        for production — it is a second development environment with a
        misleading name.
        """
        return self in (Environment.STAGING, Environment.PRODUCTION)


class ConfigurationError(RuntimeError):
    """Raised when configuration is unsafe for the resolved environment.

    Carries the *names* of the offending settings and never their values, so the
    message is safe to log, safe to surface in a health probe and safe to paste
    into an incident channel.
    """

    def __init__(self, environment: Environment, problems: Iterable[str]) -> None:
        self.environment = environment
        self.problems = list(problems)
        detail = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(
            f"Refusing to start in {environment.value}: "
            f"{len(self.problems)} configuration problem(s).\n{detail}\n"
            "Fix these and restart. No secret values are shown above by design."
        )


def get_environment(raw: str | None = None) -> Environment:
    """Resolve the environment. Unknown values are an error, not a default.

    Falling back to ``development`` on a typo would be the worst possible
    behaviour: ``IRA_ENVIRONMENT=produciton`` would silently disable every rule
    in this module on the one deployment that needs them.
    """
    value = (raw if raw is not None else os.getenv(ENV_VAR, "")).strip().lower()
    if not value:
        return Environment.DEVELOPMENT
    try:
        return Environment(value)
    except ValueError:
        valid = ", ".join(e.value for e in Environment)
        raise ConfigurationError(
            Environment.PRODUCTION,
            [f"{ENV_VAR}={value!r} is not a known environment (expected one of: {valid})"],
        ) from None


# ── Placeholder detection ────────────────────────────────────────────────────
#
# These are the shapes a value takes when someone copied `.env.example` and did
# not fill it in. `CHANGE_ME_*` is this repository's own convention.

_PLACEHOLDER_PATTERNS = (
    re.compile(r"^change[_\-]?me", re.I),
    re.compile(r"^your[_\-]", re.I),
    re.compile(r"^(placeholder|example|sample|dummy|xxx+|todo|tbd|fixme)$", re.I),
    re.compile(r"^<.*>$"),
)

#: Values that are never acceptable for a credential in a production-like
#: environment, whatever the length rules say.
_FORBIDDEN_SECRETS = frozenset({
    "admin", "password", "secret", "changeme", "root", "test", "letmein",
    "ci-test-secret-key", "ci-test-password", "ci-test",
})

#: Minimum length for a signing key. 32 characters is the floor for a key that
#: is meant to be `openssl rand -hex 32` output (64 chars) — anything shorter is
#: a passphrase somebody typed.
MIN_SECRET_KEY_LENGTH = 32

#: Minimum length for the admin password, matching the rotation procedure in
#: docs/security/CREDENTIAL_ROTATION.md ("16+ char password").
MIN_ADMIN_PASSWORD_LENGTH = 16


def is_placeholder(value: str | None) -> bool:
    """True when a value looks unfilled rather than chosen."""
    if value is None:
        return True
    v = value.strip()
    if not v:
        return True
    if v.lower() in _FORBIDDEN_SECRETS:
        return True
    return any(p.match(v) for p in _PLACEHOLDER_PATTERNS)


# ── Production validation ────────────────────────────────────────────────────

def validate_for_environment(cfg, environment: Environment) -> list[str]:
    """Return the list of configuration problems. Empty means acceptable.

    Pure and side-effect free: it reads settings and returns strings. The caller
    decides whether to raise (see :func:`enforce`), which keeps it usable from a
    test and from a readiness probe without either one starting an application.

    Only ``getattr`` is used against ``cfg`` so this cannot break when a setting
    is renamed — a missing setting is reported as a problem rather than raising
    ``AttributeError`` from inside a startup path.
    """
    if not environment.is_production_like:
        return []

    problems: list[str] = []
    env = environment.value

    # 1. DEV_MODE bypasses authentication globally.
    if getattr(cfg, "dev_mode", False):
        problems.append(
            f"DEV_MODE is enabled in {env}. It bypasses authentication and the "
            "biometric gate for every request. Set DEV_MODE=false."
        )

    # 2. Signing key — the highest-severity item on this list. A known
    #    IRA_SECRET_KEY lets anyone forge an admin token.
    secret = getattr(cfg, "ira_secret_key", None)
    if is_placeholder(secret):
        problems.append("IRA_SECRET_KEY is unset or still a placeholder.")
    elif len(secret) < MIN_SECRET_KEY_LENGTH:
        problems.append(
            f"IRA_SECRET_KEY is shorter than {MIN_SECRET_KEY_LENGTH} characters "
            f"(it is {len(secret)}). Generate one with: openssl rand -hex 32"
        )

    # 3. Admin password.
    admin_pw = getattr(cfg, "ira_admin_password", None)
    if is_placeholder(admin_pw):
        problems.append("IRA_ADMIN_PASSWORD is unset or still a placeholder.")
    elif len(admin_pw) < MIN_ADMIN_PASSWORD_LENGTH:
        problems.append(
            f"IRA_ADMIN_PASSWORD is shorter than {MIN_ADMIN_PASSWORD_LENGTH} "
            f"characters (it is {len(admin_pw)})."
        )

    # 4. Infrastructure credentials.
    for name in ("postgres_password", "redis_password"):
        if is_placeholder(getattr(cfg, name, None)):
            problems.append(f"{name.upper()} is unset or still a placeholder.")

    # 5. CORS. `app/middleware.py` builds the allowlist from ira_domain and adds
    #    localhost only when dev_mode is on. A production domain that is still
    #    local means the allowlist is wrong.
    domain = (getattr(cfg, "ira_domain", "") or "").strip()
    if not domain or is_placeholder(domain):
        problems.append("IRA_DOMAIN is unset or still a placeholder; the CORS allowlist depends on it.")
    elif getattr(cfg, "is_local_domain", False):
        problems.append(
            f"IRA_DOMAIN={domain!r} is a local address in {env}. The CORS "
            "allowlist would admit a localhost origin."
        )

    # 6. The key-encryption key provider. CLOUD-ADR-0012: "Development uses a
    #    clearly labelled development key provider; production refuses to start
    #    with it."
    #
    #    The danger is not that development uses an environment variable — it is
    #    that production uses one without anybody noticing, because it works.
    #    `core/crypto/key_provider.resolve` refuses too, at the point of use;
    #    this is the startup half, so a misconfigured deployment fails before it
    #    serves a request rather than at the first credential read.
    try:
        from core.crypto import key_provider

        name = key_provider.configured()
        if name not in key_provider.IMPLEMENTED:
            problems.append(
                f"{key_provider.PROVIDER_ENV}={name!r} is not a key provider "
                f"this build implements ({sorted(key_provider.IMPLEMENTED)})."
            )
        elif (key_provider.is_development_provider(name)
              and not key_provider.was_chosen()):
            problems.append(
                f"no {key_provider.PROVIDER_ENV} is set, so the key-encryption "
                f"key defaults to the development provider (the process "
                f"environment) in {env}. Anyone who reads the environment then "
                f"reads every data-encryption key it wraps, including those "
                f"around second factors and voice templates. This build "
                f"implements no managed provider yet — set "
                f"{key_provider.PROVIDER_ENV}={key_provider.ENVIRONMENT!r} "
                f"explicitly to accept that deliberately."
            )
    except Exception as exc:  # noqa: BLE001
        # Reported, never swallowed. A validator that cannot evaluate a rule has
        # not satisfied it, and this list is what a deployment is judged on.
        problems.append(
            f"the key provider could not be determined ({type(exc).__name__}), "
            f"so this deployment cannot show it is not using the development key."
        )

    # 7. Model runtime must not be reachable from outside. Phase 0 verified no
    #    Compose file publishes Ollama; this catches the configuration form of
    #    the same mistake — pointing the gateway at a public endpoint.
    for name in ("ollama_base_url", "vllm_fast_url", "vllm_deep_url"):
        url = getattr(cfg, name, "") or ""
        if url and _is_public_url(url):
            problems.append(
                f"{name.upper()}={_host_of(url)!r} is not a private address. The "
                "model runtime must not be reachable from outside the private network."
            )

    return problems


_PRIVATE_HOST = re.compile(
    r"^(localhost|127\.\d+\.\d+\.\d+|::1|0\.0\.0\.0|"
    r"10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|192\.168\.\d+\.\d+)$",
    re.I,
)

#: Hostnames that resolve inside a container network rather than on the
#: internet. Compose service names land here.
_INTERNAL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$", re.I)


def _host_of(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _is_public_url(url: str) -> bool:
    """True when a URL's host is neither loopback, RFC1918 nor a container name."""
    host = _host_of(url)
    if not host:
        return False
    if _PRIVATE_HOST.match(host):
        return False
    if host.endswith(".local") or host.endswith(".internal"):
        return False
    # A bare single-label name (`ollama`, `vllm-fast`) is a Docker/Kubernetes
    # service name, not a public host.
    if "." not in host and _INTERNAL_NAME.match(host):
        return False
    return True


def enforce(cfg, environment: Environment | None = None) -> Environment:
    """Validate and raise on any problem. Returns the resolved environment.

    Called from the application lifespan. In development and test it is a no-op
    that returns immediately, so nothing changes for an existing install.
    """
    env = environment if environment is not None else get_environment()
    problems = validate_for_environment(cfg, env)
    if problems:
        raise ConfigurationError(env, problems)
    return env
