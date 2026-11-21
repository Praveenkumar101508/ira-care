"""Edition awareness — which product this process is serving.

IRA ships as two editions over one governed core (ADR-0001):

  * ``private``  — today's product. Local-first, single owner, local models,
                   direct filesystem executors, owner-grade tooling.
  * ``cloud``    — the multi-tenant SaaS. Not implemented yet; Phase 2 onward.

The edition is EXPLICIT CONFIGURATION resolved once at startup. It is never
inferred from a request, a header, or a hostname — a request-derived edition
would let a caller pick which security posture applies to them.

Phase 1 introduces the concept and the defaults it selects. It does not change
private behaviour: with ``IRA_EDITION`` unset the process is ``private`` and
every default resolves to exactly what it was before.
"""
from __future__ import annotations

import os
from typing import Final, Literal

EditionName = Literal["private", "cloud"]

EDITION_PRIVATE: Final[EditionName] = "private"
EDITION_CLOUD: Final[EditionName] = "cloud"

EDITIONS: Final[tuple[EditionName, ...]] = (EDITION_PRIVATE, EDITION_CLOUD)

#: The edition assumed when nothing is configured. Private, so an existing
#: install that never sets IRA_EDITION keeps its current behaviour.
DEFAULT_EDITION: Final[EditionName] = EDITION_PRIVATE


class UnknownEditionError(ValueError):
    """Raised when IRA_EDITION names an edition that does not exist.

    Deliberately fatal rather than falling back to a default: an unrecognised
    edition means the operator believes they configured something this build
    does not implement, and guessing which posture they meant is exactly the
    kind of silent assumption this module exists to remove.
    """


def normalise_edition(value: str | None) -> EditionName:
    """Parse an edition name. Blank/None → the default edition."""
    if value is None:
        return DEFAULT_EDITION
    cleaned = value.strip().lower()
    if not cleaned:
        return DEFAULT_EDITION
    if cleaned not in EDITIONS:
        raise UnknownEditionError(
            f"IRA_EDITION={value!r} is not a known edition. "
            f"Valid values: {', '.join(EDITIONS)}."
        )
    return cleaned  # type: ignore[return-value]


def current_edition() -> EditionName:
    """The edition this process is serving.

    Reads the environment directly rather than Settings so that modules which
    must import without a full configuration (see security/owner_gate.py's
    graceful-degradation note) can still ask.
    """
    return normalise_edition(os.getenv("IRA_EDITION"))


def is_private() -> bool:
    return current_edition() == EDITION_PRIVATE


def is_cloud() -> bool:
    return current_edition() == EDITION_CLOUD


# ── Edition-scoped defaults ──────────────────────────────────────────────────
#
# Where the two editions want different answers, the difference lives HERE — one
# table — rather than as scattered `if is_cloud()` branches. Each entry records
# the private value (which must equal today's behaviour) and the cloud value.
#
# Phase 1 resolves the three defaults Phase 0 deferred:
#
#  * browser_agent_enabled — read-only Playwright browsing is outbound egress
#    that is ON by default. For PRIVATE that stays true: the owner installed it
#    on their own machine and the local-first promise is about their data
#    leaving, not about the box reaching a public web page they asked for.
#    For CLOUD it is false, and stays false until browsing runs through the
#    Permission Broker (Phase 4) and produces an Action Receipt (Phase 7).
#
#  * privacy_mode — private keeps local_first (today's code default). Cloud is
#    local_only.
#
#    PHASE 8 UPDATE: the precondition stated below is now MET. The model gateway
#    exists (`core/models/`), it discloses which provider handled each request
#    (`GET /api/v1/models/usage` reports locality per call), and every provider's
#    retention posture is recorded — including "not stated", which the routing
#    policy treats as "may retain".
#
#    The cloud default is nonetheless UNCHANGED, deliberately. Loosening a
#    shipped privacy default is a product decision, not a consequence of the
#    blocker being cleared, and doing it silently in the phase that cleared the
#    blocker is exactly how a posture drifts. An operator can now choose
#    `local_first` knowingly, with a disclosure surface to back it; that choice
#    is theirs to make. See docs/cloud-transformation/MODEL_GATEWAY.md §7.
#
#    (Historic note, kept because it records what the condition WAS:)
#    Cloud was local_only until the Phase 8 model gateway could actually disclose which
#    provider handled a request; offering an external model before the
#    disclosure exists would be a privacy claim the system cannot honour.
#
#  * cortex_available — the Cortex bridge shells out to a `cortex -z` binary on
#    the host. Private MAY use it (opt-in; the code default is still false).
#    Cloud cannot: there is no local Cortex install to shell out to. Note this
#    is AVAILABILITY, not a default — private does not turn Cortex on by itself.
#
#  * auth_fail_closed_on_redis — PHASE 9. Token revocation and the per-user
#    token version both live in Redis. When that lookup raises, the code default
#    is to fail OPEN: treat the token as not revoked and the version as current.
#
#    For PRIVATE that is the right trade. One owner, one machine, Redis on
#    localhost; a blip that locked the owner out of their own assistant would be
#    a worse outcome than briefly honouring a token they had revoked, and there
#    is no second party to protect them from.
#
#    For CLOUD it is the wrong trade and it is the posture this project's own
#    rule forbids: an authorisation service that cannot answer must deny. Failing
#    open means that during a Redis outage, every token a user revoked — logout,
#    lost phone, password change, "sign out everywhere" — starts working again,
#    for as long as the outage lasts and with nothing in the response to show it.
#    The user did the one thing available to them and it silently stopped taking
#    effect.
#
#    So cloud defaults to true. The mechanism already existed
#    (`AUTH_FAIL_CLOSED_ON_REDIS`, `api/middleware/auth.py`); only the default
#    was wrong for a multi-user service. An operator who explicitly sets the env
#    var still wins, as with every entry here.
#
#    What this does NOT fix: `revoke_token` WRITES to Redis and is fail-soft, so
#    during an outage a logout may fail to record the revocation at all. Failing
#    closed on reads does not help if the write never landed. A durable
#    revocation store is recorded as an open item in RELEASE_READINESS.md (R08)
#    rather than implied to be solved here.

_EDITION_DEFAULTS: Final[dict[str, dict[EditionName, object]]] = {
    "browser_agent_enabled": {EDITION_PRIVATE: True, EDITION_CLOUD: False},
    "privacy_mode": {EDITION_PRIVATE: "local_first", EDITION_CLOUD: "local_only"},
    "cortex_available": {EDITION_PRIVATE: True, EDITION_CLOUD: False},
    "auth_fail_closed_on_redis": {EDITION_PRIVATE: False, EDITION_CLOUD: True},
    # Owner-grade tooling. Structurally excluded from cloud by ADR-0001; the
    # flags are belt-and-braces so a misconfiguration cannot enable them there.
    # Billing is a CLOUD surface and only a cloud surface. Plans,
    # subscriptions, entitlements and invoices describe a customer paying an
    # operator for a hosted service; a private install has an owner, their own
    # machine, and nobody to bill. The routes existed in both editions because
    # nothing ever asked the question, not because private needed them.
    #
    # Gated as a MOUNT rather than as a 403, on the reasoning `21f8226` set out
    # for Forge: "a router that mounts, publishes itself in /docs and then
    # answers 403 tells an operator the capability is present and merely
    # unauthorised, which invites someone to go looking for the
    # authorisation." An absent route answers the question honestly — this
    # product does not do that — where a 403 answers a different one.
    #
    # The direction is deliberate: false for private, true for cloud, so the
    # edition that gains a surface is the one that has a reason for it. An
    # operator cannot turn billing ON in private by setting an env var, because
    # there is no code path behind it that would mean anything.
    "billing_available": {EDITION_PRIVATE: False, EDITION_CLOUD: True},
    # Workspace MANAGEMENT — create, rename, leave — is a cloud surface.
    #
    # Read the name carefully, because the distinction is the whole point.
    # Listing workspaces and selecting one stay mounted in BOTH editions and
    # must: `WorkspaceGate` calls the listing on every sign-in, and it is the
    # listing's `selection_required` that tells a client whether the cloud rule
    # applies at all. Gating the picker would break the private product to
    # remove a surface the private product uses.
    #
    # The three P12-02 verbs are different. Each is a TENANT-shaped act —
    # create derives its authority from `INVITE_CAPABLE_ROLES`, rename from
    # `workspace.manage_settings`, leave from a guard against stranding the
    # last administrator — and every one of those words describes an
    # organisation with more than one person in it. A private install has one
    # owner and a deterministic tenant, no client anywhere in this repository
    # calls the three verbs, and `legacy_owner_workspace()` serves the owner
    # without any of them.
    #
    # What made it worth removing rather than leaving alone: in private the
    # unheadered fallback serves the LEGACY workspace specifically, so a second
    # workspace created through the API is one the owner's own client would
    # never open. The surface did not merely go unused; it could manufacture
    # state the product has no way to reach.
    #
    # A mount gate, for the reason `21f8226` gave — an absent route says "this
    # product does not do that", where a 403 says "it does, and you are not
    # allowed", which invites someone to go looking for the authorisation.
    "workspace_management_available": {EDITION_PRIVATE: False, EDITION_CLOUD: True},
    "armory_available": {EDITION_PRIVATE: True, EDITION_CLOUD: False},
    "forge_available": {EDITION_PRIVATE: True, EDITION_CLOUD: False},
    "local_filesystem_executors": {EDITION_PRIVATE: True, EDITION_CLOUD: False},
}


def edition_default(key: str, edition: EditionName | None = None) -> object:
    """The default value of `key` for `edition` (defaults to the current one)."""
    if key not in _EDITION_DEFAULTS:
        raise KeyError(f"No edition-scoped default named {key!r}")
    return _EDITION_DEFAULTS[key][edition or current_edition()]


def edition_default_keys() -> tuple[str, ...]:
    return tuple(_EDITION_DEFAULTS)


__all__ = [
    "EditionName",
    "EDITION_PRIVATE",
    "EDITION_CLOUD",
    "EDITIONS",
    "DEFAULT_EDITION",
    "UnknownEditionError",
    "normalise_edition",
    "current_edition",
    "is_private",
    "is_cloud",
    "edition_default",
    "edition_default_keys",
]
