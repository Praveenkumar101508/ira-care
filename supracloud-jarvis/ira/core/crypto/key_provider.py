"""Where the key-encryption key comes from (Phase 4 batch 04).

CLOUD-ADR-0012 lists two requirements this module exists to satisfy:

    Development uses a clearly labelled development key provider; **production
    refuses to start with it**.
    Key material never lives in PostgreSQL.

The second has been true since Blocker B3. The first has not: `secrets.py`
reads `IRA_CONNECTOR_ENCRYPTION_KEY` from the process environment and says so
honestly in its own docstring — *"Still not a KMS or an HSM. The KEK comes from
the process environment. An attacker with both the database and the environment
gets every live DEK."* There was nothing to select and therefore nothing to
refuse.

── What this adds, and what it deliberately does not ───────────────────────

It adds a NAME for where the key came from, a place to add a real provider, and
a production refusal. It does **not** add a KMS. Binding a live provider needs
a provisioned account and credentials, which belongs to Phase 15; the plan says
so, and pretending otherwise here would be the "correct mechanism with no
callers" shape this repository keeps finding.

So there is exactly one implemented provider today — `environment` — and the
value of this module is that using it in production now fails loudly instead of
succeeding quietly.

── Why the refusal lives here AND in environment.py ────────────────────────

`core/runtime/environment.py` is the startup gate: it collects problems and
raises before the application serves anything (CLOUD-ADR-0007). That is where a
misconfiguration must be caught, and `validate_for_environment` gains a check
for this one.

But a check that only runs at startup protects only processes that ran it —
not a worker, not a migration script, not a test harness that constructs
settings directly. So `resolve()` refuses on its own too, in the provider
lookup, where nothing can reach a key without passing through it.

Two refusals for one rule is the shape `061` used for its append-only log and
`060` used for its grants: one of them is the one somebody removes by accident.
"""
from __future__ import annotations

import logging
from typing import Final, Optional

logger = logging.getLogger("ira.crypto.key_provider")

#: Which provider to use. Named for the KEK specifically, not for encryption in
#: general, because the DEKs are minted per record and never come from here.
PROVIDER_ENV: Final[str] = "IRA_KEK_PROVIDER"

#: The development provider: the KEK is an environment variable, read by
#: `core.connectors.secrets._load_key`. Clearly labelled, which is ADR-0012's
#: actual requirement — the danger is not that development uses it, it is that
#: production uses it without anybody noticing.
ENVIRONMENT: Final[str] = "environment"

#: AWS KMS holds the master key; the KEK arrives as ciphertext only KMS can
#: open. P4-10. See `core/crypto/aws_kms.py`.
AWS_KMS: Final[str] = "aws-kms"

#: Named but NOT implemented. Listed so the refusal below can tell "you asked
#: for a provider that does not exist yet" apart from "you typed it wrong",
#: which are different operator mistakes with different fixes.
#:
#: One provider was implemented, not three. A second managed provider adds a
#: second set of failure modes to test and no security the first does not
#: already give; the boundary exists so the second can be added when a
#: deployment needs it.
PLANNED: Final[frozenset[str]] = frozenset({"gcp-kms", "azure-keyvault", "vault"})

IMPLEMENTED: Final[frozenset[str]] = frozenset({ENVIRONMENT, AWS_KMS})

#: The default, and it is the development one on purpose. A default of "aws-kms"
#: would make every fresh checkout fail with a credentials error instead of
#: working, and the production refusal is what makes a permissive default safe.
DEFAULT: Final[str] = ENVIRONMENT


class KeyProviderError(RuntimeError):
    """The KEK provider is unusable. Never a fallback — always a refusal."""


def configured(env: Optional[dict[str, str]] = None) -> str:
    """The provider name as configured, without judging it."""
    import os

    source = os.environ if env is None else env
    return (source.get(PROVIDER_ENV) or DEFAULT).strip().lower()


def was_chosen(env: Optional[dict[str, str]] = None) -> bool:
    """True when an operator SET the provider rather than inheriting the default.

    The distinction carries the whole production rule. ADR-0012 says production
    "refuses to start with" the development provider, and the danger it names is
    specific: *"production uses it without anybody noticing."*

    Refusing the default in production is that rule. Refusing an EXPLICIT
    `IRA_KEK_PROVIDER=environment` would be a different one — it would make
    production unstartable outright, because the environment provider is the
    only one this build implements and a real KMS is Phase 15. A rule that makes
    every deployment impossible does not get obeyed; it gets deleted, along with
    the reasoning that produced it.

    So the line is drawn at deliberateness. An operator who writes it down has
    accepted a stated exposure and it is recorded in their configuration. An
    operator who wrote nothing has not decided anything, and that is exactly the
    case that must not boot.
    """
    import os

    source = os.environ if env is None else env
    return bool((source.get(PROVIDER_ENV) or "").strip())


def is_development_provider(name: str) -> bool:
    """True for a provider whose key material is not in a managed store.

    One entry today. A function rather than `name == ENVIRONMENT` because the
    question the callers ask is "is this safe for production", and that question
    should keep its meaning when a second development provider appears — a file
    on disk, say, which would be equally unacceptable and equally easy to add
    without revisiting every comparison.
    """
    return name == ENVIRONMENT


def resolve(environment=None, env: Optional[dict[str, str]] = None) -> str:
    """The provider to use, or raise.

    `environment` is a `core.runtime.environment.Environment`; when omitted it
    is resolved from the process. Passing it in keeps this callable from a test
    without setting process-wide state.

    Refuses in three cases, and the messages are different because the fixes
    are:

      * an unknown name — a typo;
      * a planned-but-unimplemented name — a real provider that does not exist
        yet, so the operator is asking for something this build cannot do;
      * the development provider in a production-like environment — the case
        ADR-0012 names.
    """
    name = configured(env)

    if name not in IMPLEMENTED:
        if name in PLANNED:
            raise KeyProviderError(
                f"{PROVIDER_ENV}={name!r} names a key provider that is planned "
                f"but not implemented in this build. Phase 4 delivers the "
                f"provider boundary and the production refusal; binding a live "
                f"KMS is Phase 15 and needs provisioned credentials. There is "
                f"no fallback: continuing would silently use the development "
                f"key while the configuration says otherwise."
            )
        raise KeyProviderError(
            f"{PROVIDER_ENV}={name!r} is not a key provider this build knows. "
            f"Implemented: {sorted(IMPLEMENTED)}. Planned: {sorted(PLANNED)}."
        )

    if environment is None:
        from core.runtime.environment import get_environment

        environment = get_environment()

    if (is_development_provider(name)
            and environment.is_production_like
            and not was_chosen(env)):
        raise KeyProviderError(
            f"no {PROVIDER_ENV} is set and this is {environment.value}, so the "
            f"key-encryption key would default to the DEVELOPMENT provider — "
            f"the process environment. Anyone who reads the environment then "
            f"reads every data-encryption key it wraps, including the ones "
            f"around second factors and voice templates. CLOUD-ADR-0012 "
            f"requires production to refuse this rather than run with it "
            f"unnoticed.\n\n"
            f"This build implements no managed provider yet — a real KMS is "
            f"Phase 15 — so the only way forward today is to accept the "
            f"exposure deliberately by setting {PROVIDER_ENV}={ENVIRONMENT!r} "
            f"explicitly. Writing it down is the point: it is then a decision "
            f"in your configuration rather than a default nobody saw."
        )

    return name


__all__ = [
    "DEFAULT",
    "ENVIRONMENT",
    "IMPLEMENTED",
    "PLANNED",
    "PROVIDER_ENV",
    "KeyProviderError",
    "configured",
    "is_development_provider",
    "resolve",
]
