"""Cloud tenant and workspace provisioning (Phase 2 batch 4).

Separate from `core/identity/provisioning.py`, which is the PRIVATE edition's
owner bootstrap — it gives the one configured `.env` owner the identity rows
`023`/`035` seed for a single hard-coded UUID, and it explicitly *"does not run
in the cloud edition"* and *"does not create a tenant, an organisation or a
workspace"*. This module does exactly those things, for a customer, and the two
share nothing but a word.

The step that turns a verified account into one that can actually resolve.
Batch 2 created the user and proved the address; batch 3 made the password
check a real path. Both stopped at the same wall, and both said so: after
verification the user *still resolves to nothing*, because
`_resolve_cloud_principal` requires a membership that grants access and nothing
had ever created one. This creates one.

    tenant → organisation → default workspace
           → tenant membership (primary_owner)
           → workspace membership (workspace_admin)

── One transaction, or none of it ──────────────────────────────────────────

All five rows are written in a single transaction, the `tenants` row included.
A tenant with no organisation, or an organisation with no workspace, or a
workspace with no owner, are each a state somebody would eventually have to
diagnose — and the last one is unrecoverable through any normal path, because
`provisioning_connection` refuses a tenant that already has an active
membership and every other route needs a Principal. So a partial tenant is not
a state this can commit.

── Every child row derives its tenant from the bound transaction ───────────

`organisations.tenant_id` and `workspaces.tenant_id` are DEFAULTed to
`current_setting('app.tenant_id')`, so those INSERTs name no tenant at all. The
two membership tables have no such default, so they select the same GUC
explicitly rather than taking a Python parameter. The difference matters: a
parameter is a second copy of the answer, and a second copy can disagree with
the policy that checks it. Reading it back from the transaction means the value
the row carries and the value the WITH CHECK tests are the same expression.

── The display name is NOT `tenants.name` ──────────────────────────────────

`tenants.name` carries a globally UNIQUE index, which is the wrong shape for a
customer-supplied name in a multi-tenant system — 033 recorded exactly this
reasoning when it made workspace slugs unique per tenant rather than globally:
*"a global unique constraint on a tenant-created name means the first tenant to
use `main` takes it from everyone, and the second tenant sees an error that
looks like their own input was wrong."*

Worse than confusing, it is an oracle: a provisioning call that fails because
another customer already used that name has disclosed that customer's name.

The index cannot simply be dropped — `data/__init__.py` upserts with
`ON CONFLICT (name)`, which requires it. So the customer's name is stored where
033 says it belongs: on `organisations.name`, which that migration explicitly
left non-unique — *"Two unrelated customers may both be called \'Acme\'"*.
`tenants.name` and `tenants.slug` get values unique by construction, derived
from the tenant id, and are internal labels.

The same reasoning governs `organisations.slug`, in the opposite direction.
033 made THAT index globally unique deliberately and said so, because it is the
public identifier that may appear in a URL or an invite link. This batch does
not overturn that decision; it simply does not derive the slug from anything a
customer supplied, so there is nothing for two customers to collide on. A
customer-chosen public slug is a real feature and a separate one, because the
uniqueness check it needs is an oracle unless it is designed not to be.
"""
from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass

logger = logging.getLogger("ira.identity.tenant_provisioning")

#: The role the founder holds in their own tenant. `cloud_scopes_for` maps it,
#: and `Principal.is_owner` stays hard-false regardless — a tenant's primary
#: owner is a customer, not the person who owns the deployment (3C-04, 3C-08,
#: P2-07).
FOUNDER_TENANT_ROLE = "primary_owner"

#: The founder's role in the default workspace. `workspace_role_capabilities`
#: is the authority for what it grants; this only names it.
FOUNDER_WORKSPACE_ROLE = "workspace_admin"

DEFAULT_WORKSPACE_NAME = "General"
DEFAULT_WORKSPACE_SLUG = "general"

MAX_NAME_LENGTH = 120


class ProvisioningError(RuntimeError):
    """Provisioning could not proceed. Safe to show to the caller."""


class ProvisioningRefused(ProvisioningError):
    """This account may not be provisioned a tenant.

    Unknown, unverified, deleted, suspended — or already a member of one.
    Deliberately one type: the caller of a provisioning API is usually an
    operator, but the reasons overlap with the ones `cloud_login` refuses to
    distinguish, and there is no benefit in being more forthcoming here.
    """


class ProvisioningUnavailable(ProvisioningError):
    """The database could not be reached. Fail closed, never partially."""


@dataclass(frozen=True, slots=True)
class ProvisionedTenant:
    """What was created. Every id is one this process generated."""

    tenant_id: uuid.UUID
    organisation_id: uuid.UUID
    workspace_id: uuid.UUID
    founder_user_id: uuid.UUID
    #: The stored normalised email — the value that becomes a token's `sub`
    #: and that every later request re-resolves.
    founder_subject: str


def slugify(value: str) -> str:
    """A URL-safe slug, or `''` when nothing survives.

    Returning empty rather than a placeholder is deliberate: a caller that gets
    `''` has to decide what to do, whereas a silent `"workspace-1"` would let a
    name made entirely of characters this cannot represent become a slug that
    looks chosen.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    lowered = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower())
    return lowered.strip("-")[:60]


def _validate_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ProvisioningError("an organisation name is required")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise ProvisioningError(
            f"an organisation name must be at most {MAX_NAME_LENGTH} characters"
        )
    return cleaned


async def _eligible_founder(email: str):
    """The verified, active account this tenant will belong to, or a refusal.

    The predicate is `_resolve_cloud_principal`'s, repeated for the same reason
    `cloud_login` repeats it: `users_verified_email_key` is UNIQUE only among
    rows that are verified and not deleted, so without both clauses the lookup
    is non-deterministic across the several unverified rows that 034 allows to
    share one address — and provisioning a tenant to the wrong one of them
    would hand an impostor a tenant under somebody else's address.
    """
    from utils.db import acquire

    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, status, normalised_email
              FROM users
             WHERE normalised_email = normalise_email($1)
               AND email_verified_at IS NOT NULL
               AND deleted_at IS NULL
            """,
            email.strip(),
        )

    if row is None:
        raise ProvisioningRefused("no verified account for that address")
    if row["status"] != "active":
        raise ProvisioningRefused("that account is not active")
    return row


async def provision_tenant(
    founder_email: str,
    organisation_name: str,
    *,
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
) -> ProvisionedTenant:
    """Create a tenant, an organisation, a default workspace and the founder's
    memberships — atomically.

    Raises `ProvisioningRefused` when the account may not be provisioned and
    `ProvisioningUnavailable` when the database cannot be reached. Never
    returns partially.

    ── Why a second tenant is refused ──────────────────────────────────────

    A user who already holds an access-granting membership is refused, and this
    is a deliberate coupling to a limitation batch 3 recorded rather than an
    arbitrary rule. `_resolve_cloud_principal` raises `AmbiguousTenant` when a
    user belongs to several tenants and names none, and the cloud token carries
    no tenant claim to name one with — so provisioning a second tenant to an
    existing member would *silently remove their ability to log in at all*.

    Breaking a working account as a side effect of an unrelated successful
    operation is the worst available outcome, so the refusal stays until tenant
    selection makes multi-tenancy expressible end to end.
    """
    from core.identity.db_context import provisioning_connection

    name = _validate_name(organisation_name)
    ws_name = _validate_name(workspace_name)

    try:
        founder = await _eligible_founder(founder_email)
    except ProvisioningRefused:
        raise
    except Exception as exc:  # noqa: BLE001 — an unreachable register is a refusal
        # The exception TYPE only: a connection error's body can carry the DSN.
        logger.warning("provisioning could not read the identity register: %s",
                       type(exc).__name__)
        raise ProvisioningUnavailable(
            "the identity register could not be reached"
        ) from exc

    founder_id = founder["id"]
    subject = founder["normalised_email"]

    # Ids are generated here so that every INSERT below names a value this
    # process chose, and so the tenant GUC can be bound before the tenant row
    # exists — which is what allows all five writes to share one transaction.
    tenant_id = uuid.uuid4()
    organisation_id = uuid.uuid4()
    workspace_id = uuid.uuid4()

    ws_slug = slugify(ws_name) or DEFAULT_WORKSPACE_SLUG

    try:
        async with provisioning_connection(tenant_id, founder_id) as conn:
            # Checked INSIDE the provisioning transaction rather than before
            # it. Between a check and a write there is a window, and the write
            # here is the one that would take a working account's login away.
            # `membership_grants_access` is the generated column, so "does this
            # membership grant access" is the database's answer, not a
            # re-implementation of it.
            existing = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM tenant_memberships "
                " WHERE user_id = $1 AND membership_grants_access)",
                founder_id,
            )
            if existing:
                raise ProvisioningRefused(
                    "that account already belongs to a tenant"
                )

            # `tenants` carries no RLS — it is the registry that tenant-scoped
            # policies point AT, so it cannot itself be tenant-scoped. That is
            # what lets this row be written inside a transaction already bound
            # to the tenant it creates.
            #
            # `name` and `slug` are internal and unique by construction; the
            # customer's name goes on the organisation. See the module header.
            #
            # `display_name` (055) carries the same customer text on a
            # deliberately NON-unique column, because tenant selection has to
            # show it before any tenant is bound — and `organisations` is
            # tenant-scoped, so an unbound read of it returns nothing.
            await conn.execute(
                """
                INSERT INTO tenants (id, name, slug, display_name,
                                     edition, status)
                VALUES ($1, $2, $3, $4, 'cloud', 'active')
                """,
                tenant_id, f"cloud-{tenant_id}", f"cloud-{tenant_id}", name,
            )

            # No tenant_id column in either INSERT. It DEFAULTs to
            # `current_setting('app.tenant_id')`, so the row's tenant and the
            # policy's tenant are the same expression rather than two copies.
            # The customer's text goes in `name`; `slug` is derived from the id.
            #
            # `organisations_slug_key` is GLOBALLY unique, and 033 made it so on
            # purpose: *"an organisation slug is a customer-facing identifier
            # that may end up in a URL or an invite link, so two customers
            # cannot hold the same one... the exception is called out here so it
            # is not read as an oversight."* That is a deliberate decision and
            # this batch does not overturn it.
            #
            # But a globally unique slug derived from a customer-supplied name
            # is an oracle: the second customer called "Acme Industries" would
            # fail on `acme-industries` and learn that the first one exists. So
            # the slug is not derived from the name at all. 033 states the other
            # half directly — *"Organisation NAME is not globally unique. Two
            # unrelated customers may both be called 'Acme'"* — which is exactly
            # where the customer's text belongs.
            #
            # Letting a customer CHOOSE a public slug is a real feature and a
            # separate one, because the uniqueness check it needs is an oracle
            # unless it is designed not to be.
            await conn.execute(
                "INSERT INTO organisations (id, name, slug) VALUES ($1, $2, $3)",
                organisation_id, name, f"org-{tenant_id}",
            )
            await conn.execute(
                """
                INSERT INTO workspaces
                       (id, organisation_id, name, slug, is_default, status)
                VALUES ($1, $2, $3, $4, TRUE, 'active')
                """,
                workspace_id, organisation_id, ws_name, ws_slug,
            )

            # These two tables have no tenant DEFAULT, so the GUC is read back
            # explicitly. Passing `tenant_id` as a parameter would work today
            # and would be a second copy of the answer — the thing the standing
            # rule about deriving tenancy from transaction-local context exists
            # to prevent.
            await conn.execute(
                """
                INSERT INTO tenant_memberships
                       (tenant_id, user_id, role, status, joined_at)
                VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                        $1, $2, 'active', NOW())
                """,
                founder_id, FOUNDER_TENANT_ROLE,
            )
            await conn.execute(
                """
                INSERT INTO workspace_memberships
                       (tenant_id, workspace_id, user_id, role, status)
                VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                        $1, $2, $3, 'active')
                """,
                workspace_id, founder_id, FOUNDER_WORKSPACE_ROLE,
            )
    except ProvisioningError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("provisioning failed: %s", type(exc).__name__)
        raise ProvisioningUnavailable(
            "the tenant could not be provisioned"
        ) from exc

    logger.info("provisioned a cloud tenant with its default workspace")
    return ProvisionedTenant(
        tenant_id=tenant_id,
        organisation_id=organisation_id,
        workspace_id=workspace_id,
        founder_user_id=founder_id,
        founder_subject=subject,
    )


__all__ = [
    "DEFAULT_WORKSPACE_NAME",
    "FOUNDER_TENANT_ROLE",
    "FOUNDER_WORKSPACE_ROLE",
    "ProvisionedTenant",
    "ProvisioningError",
    "ProvisioningRefused",
    "ProvisioningUnavailable",
    "provision_tenant",
    "slugify",
]
