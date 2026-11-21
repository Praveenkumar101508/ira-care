"""The model registry — which models may serve, and which artefacts may be
fetched (Phase 5, MOD-02 and MOD-03).

`BASELINE_MODEL_INVENTORY.md` §9:

    MOD-02 | No artefact hashes, no revision pinning, no pull allowlist | High
    MOD-03 | No model registry, no versioning, no rollback target      | Medium

Migration `067` holds the tables. This is the only thing that reads them.

── Two questions, deliberately not one ─────────────────────────────────────

    serving(...)      may this model answer a request?
    allowlisted(...)  may this artefact be downloaded at all?

Permission to fetch is not permission to serve. A model can be pulled, hashed
and inspected long before anybody has established what its commercial terms are
— and under `067` it cannot serve until somebody has.

── The environment is derived, never supplied ──────────────────────────────

`067` scopes every registry row to `development`, `staging` or `production`,
which is MOD-02's "no separation between development, staging and production
model registries". That separation is worth nothing if the caller picks which
registry to read: a request that could name its environment could name the one
whose rules it likes. So the environment comes from
`core.runtime.environment`, and there is no parameter for it.

The same rule as tenancy, for the same reason — authority is derived from the
server's own context, never accepted from request data.

── Nothing here writes ─────────────────────────────────────────────────────

`067` grants `ira_app` SELECT and revokes the rest. This module has no insert
or update path, and that is not an oversight to be filled in later: if the
process that CHECKS the allowlist can also ADD to it, then a request-handling
path talked into writing one entry has defeated the control. Registration is an
operator act performed by the migration role.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # annotation only — imported here, never at runtime
    # `resolve_for_request` is annotated `-> "ApprovedModel"` and the name was
    # imported nowhere, so anything resolving this module's type hints raised
    # NameError. Harmless while nothing calls `get_type_hints()`, which is
    # exactly why it survived — the same shape as FINDING AH one file over.
    from core.interfaces.model_provider import ApprovedModel

logger = logging.getLogger("ira.models.registry")

__all__ = [
    "RegisteredModel", "AllowlistEntry", "LicencePosition",
    "ModelNotRegistered", "ArtefactNotAllowlisted", "ModelLicenceNotApproved",
    "current_environment", "serving", "for_tenant", "rollout_bucket",
    "resolve_for_request",
    "allowlisted", "require_allowlisted",
    "licence_for", "require_approved_licence",
]


@dataclass(frozen=True, slots=True)
class RegisteredModel:
    """One serving row. Mirrors the columns `067` records."""

    id: object
    model_key: str
    version: int
    base_model: str
    revision: str
    artifact_hash: str
    adapter_version: Optional[str]
    commercial_licence: Optional[str]
    environment: str
    status: str
    rollout_percentage: int
    rollback_target: Optional[object]
    #: `068`. The licence register row this version serves under, or None.
    #:
    #: None is not "unlicensed but probably fine" — it is the state `067` left
    #: every row in, and `require_approved_licence` refuses it. The free-text
    #: `commercial_licence` above is what `067` had, and it accepts any string;
    #: this is what decides.
    licence_id: Optional[object] = None


@dataclass(frozen=True, slots=True)
class AllowlistEntry:
    source: str
    identifier: str
    revision: str
    #: None until the artefact has been fetched and hashed once. A real state,
    #: and the loader's problem — see `require_allowlisted`.
    artifact_hash: Optional[str]


class ModelNotRegistered(RuntimeError):
    """Raised rather than returning None.

    A `None` gets `or`-ed into a default by the third caller. Refusing to serve
    a model nobody registered is the whole point of MOD-03, so it raises.
    """


class ArtefactNotAllowlisted(RuntimeError):
    """MOD-02. Raised for both "not on the list" and "on the list at a
    different revision", because the second is the one that looks fine."""


class ModelLicenceNotApproved(RuntimeError):
    """MOD-01. The model is registered, serving and allowlisted — and nobody has
    established that its licence permits IRA to run it for a paying tenant.

    Raised, not returned, and carrying the classification it found, because the
    four states mean different things to whoever reads the log:

        unknown           the publisher declares nothing. Somebody must obtain
                          terms — there is nothing to read. (L-03.)
        review_required   evidence is on file and nobody has read it. (L-01, L-02.)
        known_restricted  somebody read it and it does NOT permit this use.
        (absent)          the row predates 068, or the registry was edited by
                          hand. Treated as unapproved, never as fine.
    """


@dataclass(frozen=True, slots=True)
class LicencePosition:
    """One row of `model_licence_register`, as the request path sees it.

    Deliberately carries the EVIDENCE and the CLASSIFICATION and nothing that
    interprets them. `licence_identifier` is what the publisher published,
    verbatim — `Apache License`, `Gemma Terms of Use`, or None where nothing was
    declared. Whether that permits IRA's use is `classification`, and only a
    named human writes it.
    """

    id: object
    source: str
    identifier: str
    reference: str
    licence_identifier: Optional[str]
    classification: str

    @property
    def approved(self) -> bool:
        return self.classification == "known_approved"


def current_environment() -> str:
    """`development`, `staging` or `production` — from the server, not the
    request.

    Anything the environment module reports that `067` does not model (`test`,
    for instance) reads the development registry. That is the conservative
    direction: a test environment must not accidentally consult production's
    rollout decisions.
    """
    try:
        from core.runtime.environment import get_environment

        name = str(getattr(get_environment(), "value", get_environment())).lower()
    except Exception:  # noqa: BLE001
        return "development"
    return name if name in ("development", "staging", "production") else "development"


_SELECT = (
    "SELECT id, model_key, version, base_model, revision, artifact_hash, "
    "       adapter_version, commercial_licence, environment, status, "
    "       rollout_percentage, rollback_target, licence_id "
    "  FROM model_registry "
)


def _row_to_model(row) -> RegisteredModel:
    return RegisteredModel(
        id=row["id"], model_key=row["model_key"], version=int(row["version"]),
        base_model=row["base_model"], revision=row["revision"],
        artifact_hash=row["artifact_hash"],
        adapter_version=row["adapter_version"],
        commercial_licence=row["commercial_licence"],
        environment=row["environment"], status=row["status"],
        rollout_percentage=int(row["rollout_percentage"]),
        rollback_target=row["rollback_target"],
        licence_id=row["licence_id"],
    )


async def serving(model_key: str) -> Optional[RegisteredModel]:
    """The version of `model_key` serving in THIS environment, or None.

    A plain connection: `model_registry` carries no tenant column and no RLS —
    it is platform configuration, classified `REFERENCE_DATA` alongside
    `model_providers`. Binding a tenant here would suggest a per-tenant registry
    that does not exist and cannot be enforced.
    """
    from utils.db import acquire

    async with acquire() as conn:
        row = await conn.fetchrow(
            _SELECT + " WHERE model_key = $1 AND environment = $2 "
                      "   AND status = 'serving'",
            model_key, current_environment())
    return _row_to_model(row) if row else None


def rollout_bucket(model_key: str, tenant_id) -> int:
    """Which 0–99 bucket this tenant falls in for this model. Stable, forever.

    **Not `hash()`.** Python randomises string hashing per process unless
    `PYTHONHASHSEED` is fixed, so `hash(...) % 100` would put a tenant in a
    different bucket after every restart — and a tenant that flips between two
    model versions on each deploy is the opposite of a controlled rollout. It
    would also be invisible: each individual request looks correct.

    Keyed on the model as well as the tenant so that two models at 10% do not
    select the same tenants. Otherwise one unlucky tenant is the canary for
    everything at once.
    """
    import hashlib

    digest = hashlib.sha256(f"{model_key}:{tenant_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100


async def for_tenant(model_key: str, tenant_id) -> Optional[RegisteredModel]:
    """The version this tenant should get, honouring the rollout percentage.

    ── What `rollout_percentage` means here ────────────────────────────────

    The serving row is rolled out to that percentage of tenants. The remainder
    stay on `rollback_target` — the version the serving one replaced. So a
    partial rollout is expressible without a second status: one row is serving
    at 20%, and 80% of tenants keep getting the row it points back to.

    100 means everybody, which is the ordinary case and what a fully rolled-out
    model looks like.

    A serving row at less than 100 with NO rollback target has nowhere to send
    the remainder. Rather than invent a fallback — silently serving the new
    model to everybody, which makes the percentage decorative — those tenants
    get nothing and the caller refuses. That is the fail-closed direction: an
    incomplete rollout configuration stops requests rather than quietly
    ignoring the operator's instruction.
    """
    current = await serving(model_key)
    if current is None:
        return None
    if current.rollout_percentage >= 100:
        return current
    if rollout_bucket(model_key, tenant_id) < current.rollout_percentage:
        return current

    if current.rollback_target is None:
        logger.warning(
            "model %s is serving at %d%% with no rollback target; %s gets "
            "nothing", model_key, current.rollout_percentage, tenant_id)
        return None

    from utils.db import acquire

    async with acquire() as conn:
        row = await conn.fetchrow(_SELECT + " WHERE id = $1",
                                  current.rollback_target)
    if row is None:
        # The FK is ON DELETE RESTRICT, so this means the row was retired or
        # the registry was edited by hand. Either way the configuration no
        # longer describes a reachable version.
        logger.warning("rollback target of %s does not resolve", model_key)
        return None
    return _row_to_model(row)




async def resolve_for_request(model_key: str, tenant_id) -> "ApprovedModel":
    """The exact model a request is permitted to run, or a refusal.

    **This is the whole of P5-07's flow, in one place.** The gateway calls it
    BEFORE it calls a provider, so an unapproved model produces zero provider
    calls rather than a discarded answer.

        registry lookup
        → is a version serving in THIS environment?
        → which version does the rollout give THIS tenant?
        → is its artefact pinned and on the pull allowlist?
        → hand back the exact identity the provider must run

    Every step refuses rather than falling back. There is no "closest match":
    the two things that could stand in for an approved model — a different
    version, or the same version at a different revision — are precisely the
    substitutions MOD-02 and MOD-03 exist to prevent.
    """
    from core.interfaces.model_provider import ApprovedModel

    chosen = await for_tenant(model_key, tenant_id)
    if chosen is None:
        raise ModelNotRegistered(
            f"No model version is approved to serve {model_key!r} for this "
            f"tenant in the {current_environment()} environment. Either nothing "
            f"is registered as serving, or a partial rollout excludes this "
            f"tenant and names no fallback.")

    # The registry says which weights; the allowlist says they may be fetched at
    # that revision. Both, because a registered model whose artefact is not
    # allowlisted is a supply-chain hole with a licence attached to it.
    await require_allowlisted("huggingface", chosen.base_model, chosen.revision)

    # MOD-01, and the third thing that must be true. Phase 6.
    position = await require_approved_licence(chosen)

    return ApprovedModel(
        model_key=chosen.model_key,
        version=chosen.version,
        base_model=chosen.base_model,
        revision=chosen.revision,
        artifact_hash=chosen.artifact_hash,
        licence_identifier=position.licence_identifier or "",
    )


async def licence_for(licence_id) -> Optional[LicencePosition]:
    """The licence register row, or None if there is not one.

    None means "no such row", never "no restriction". Every caller has to
    distinguish those, so none of them gets a default here.
    """
    if licence_id is None:
        return None

    from utils.db import acquire

    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, source, identifier, reference, licence_identifier, "
            "       classification "
            "  FROM model_licence_register WHERE id = $1", licence_id)
    if row is None:
        return None
    return LicencePosition(
        id=row["id"], source=row["source"], identifier=row["identifier"],
        reference=row["reference"],
        licence_identifier=row["licence_identifier"],
        classification=row["classification"])


async def require_approved_licence(model: RegisteredModel) -> LicencePosition:
    """**MOD-01, enforced where the request is, not only where the write was.**

    `068`'s `model_registry_licence_gate` trigger already refuses to let a
    version reach `status = 'serving'` without an approved licence row. So why
    check again on the read path?

    Because the two failures are different, and only one of them the trigger
    sees:

      * A trigger enforces the moment of writing. It cannot speak for a row
        written before it existed, for a row written while it was disabled, or
        for a database whose schema drifted — and `pg_trigger.tgenabled` is a
        one-character column that `ALTER TABLE ... DISABLE TRIGGER` flips
        without touching any DDL a schema diff would show.

      * An approval is a legal position, and legal positions get WITHDRAWN. The
        withdrawal trigger retires the versions that relied on one, which is the
        right behaviour and depends on that trigger also still being there.

    Two independent mechanisms, both fail-closed, is the shape the encryption
    and tenancy work already uses. Neither is redundant: this one refuses to
    serve, and the trigger refuses to record.

    An unreadable register is a REFUSAL, not a pass. P5-02 recorded the same
    decision for the registry itself — a control that treats "I could not check"
    as "nothing to worry about" is a control that switches itself off under
    exactly the conditions it exists for.
    """
    try:
        position = await licence_for(model.licence_id)
    except Exception as exc:  # noqa: BLE001
        raise ModelLicenceNotApproved(
            f"the licence register could not be read for {model.model_key}@"
            f"{model.version} ({type(exc).__name__}). IRA does not serve a model "
            f"whose licence position it cannot establish."
        ) from exc

    if position is None:
        raise ModelLicenceNotApproved(
            f"{model.model_key}@{model.version} has no licence register row. "
            f"067's free-text commercial_licence field is not a licence "
            f"position — see docs/cloud/MODEL_LICENSE_REGISTER.md.")

    if not position.approved:
        raise ModelLicenceNotApproved(
            f"{model.model_key}@{model.version} runs {position.identifier}:"
            f"{position.reference}, whose licence is {position.classification!r}"
            f" (published identifier: {position.licence_identifier!r}). Only a "
            f"named human may record known_approved — see "
            f"docs/cloud/MODEL_LICENCE_DETERMINATIONS_REQUIRED.md.")

    return position



async def allowlisted(source: str, identifier: str,
                      revision: str) -> Optional[AllowlistEntry]:
    """The allowlist entry for exactly this (source, identifier, revision)."""
    from utils.db import acquire

    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT source, identifier, revision, artifact_hash "
            "  FROM model_pull_allowlist "
            " WHERE source = $1 AND identifier = $2 AND revision = $3",
            source, identifier, revision)
    if row is None:
        return None
    return AllowlistEntry(
        source=row["source"], identifier=row["identifier"],
        revision=row["revision"], artifact_hash=row["artifact_hash"])


async def require_allowlisted(source: str, identifier: str,
                              revision: str) -> AllowlistEntry:
    """Refuse to fetch anything that is not on the list at that exact revision.

    Three refusals, and the middle one is the interesting one:

      * no revision at all — an unpinned fetch is MOD-02's finding verbatim
      * the identifier is allowlisted, but not at this revision — the entry
        exists, a "is it allowlisted" check passes, and a different artefact
        arrives. This is the shape a supply-chain substitution takes.
      * nothing matches — the ordinary case
    """
    if not revision or revision.strip().lower() in ("latest", "main", "master", "head"):
        raise ArtefactNotAllowlisted(
            f"{identifier!r} was requested at revision {revision!r}, which does "
            f"not pin anything — it fetches whatever the source serves at that "
            f"moment. MOD-02 exists because IRA did this.")

    entry = await allowlisted(source, identifier, revision)
    if entry is not None:
        return entry

    from utils.db import acquire

    async with acquire() as conn:
        others = [r["revision"] for r in await conn.fetch(
            "SELECT revision FROM model_pull_allowlist "
            " WHERE source = $1 AND identifier = $2", source, identifier)]
    if others:
        raise ArtefactNotAllowlisted(
            f"{identifier!r} is allowlisted on {source}, but not at revision "
            f"{revision!r}. Allowed: {sorted(others)}. A different revision is "
            f"a different artefact.")
    raise ArtefactNotAllowlisted(
        f"{identifier!r} is not on the {source} pull allowlist. Adding it is an "
        f"operator action performed by the migration role; the runtime role "
        f"cannot widen the list it is being checked against.")
