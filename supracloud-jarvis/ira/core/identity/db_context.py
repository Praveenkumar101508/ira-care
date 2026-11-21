"""Tenant-scoped database access.

Row-Level Security in `postgres/023_tenancy.sql` decides visibility from one
setting: the `app.tenant_id` GUC. This module is the only place that sets it, and
it sets it from the authenticated :class:`Principal` — never from a request body,
query parameter or header.

Why the GUC rather than a `WHERE tenant_id = $1` convention:

    A convention is enforced by whoever remembers it. RLS is enforced by the
    database on every statement, including the one written at 2am six months
    from now. If the GUC is unset, `current_setting('app.tenant_id', true)`
    returns NULL, `tenant_id = NULL` is NULL rather than true, and the query
    matches no rows. Missing context therefore denies by construction.

Why `SET LOCAL` inside a transaction:

    Connections come from a pool and are reused. A plain `SET` would persist on
    the connection after release, so the next borrower — possibly a different
    tenant — would inherit it. That is a cross-tenant read with no bug in any
    query. `SET LOCAL` is scoped to the transaction and is discarded on COMMIT
    or ROLLBACK, so a returned connection carries no tenant.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from core.identity.principal import Principal

logger = logging.getLogger("ira.identity.db")


class MissingTenantContext(RuntimeError):
    """Raised when tenant-scoped work is attempted without a resolved tenant.

    Fail closed: this raises rather than falling back to a default tenant. A
    default tenant in a multi-tenant process is a cross-tenant read waiting for
    the right request.
    """


class MissingWorkspaceContext(MissingTenantContext):
    """Raised when workspace-scoped work is attempted without a verified workspace.

    A subclass of :class:`MissingTenantContext` so existing handlers that already
    fail closed on a missing tenant also fail closed on a missing workspace,
    rather than letting it through a narrower `except` they were never updated
    for. Missing workspace context is the same category of problem: not enough
    information to know whose data this is.
    """


@asynccontextmanager
async def tenant_connection(principal: Principal) -> AsyncGenerator[object, None]:
    """Acquire a connection with RLS bound to `principal`'s tenant and identity.

    Everything inside runs in one transaction with `app.tenant_id` and
    `app.user_id` set. On exit the transaction ends and both are discarded
    with it.

    **Why `app.user_id` as well.** Phase 3B Batch 1 introduced USER_PRIVATE
    tables — `totp_secrets`, `voice_profiles` — whose policies predicate on
    `app.user_id` rather than `app.tenant_id`, so that a tenant administrator
    has no route to a member's second factor or biometric template. Under forced
    RLS an unset GUC yields NULL, and `user_id = NULL` is NULL rather than true,
    so a connection that bound only the tenant would match **no rows at all** in
    those tables — silently, without erroring. That is the fail-closed direction
    and therefore the dangerous one: the symptom is a second factor that appears
    not to exist, not an error anybody would notice.
    """
    if principal is None:
        raise MissingTenantContext(
            "tenant_connection() requires a Principal — refusing to open an "
            "unscoped connection to customer data."
        )
    if getattr(principal, "tenant_id", None) is None:
        raise MissingTenantContext(
            f"Principal {getattr(principal, 'user_id', '?')} has no tenant_id — "
            "refusing to open an unscoped connection."
        )

    from utils.db import acquire

    async with acquire() as conn:
        async with conn.transaction():
            # Parameterised: neither id is ever interpolated into SQL text.
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(principal.tenant_id),
            )
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)",
                str(principal.user_id),
            )
            yield conn


@asynccontextmanager
async def workspace_connection(principal: Principal) -> AsyncGenerator[object, None]:
    """A connection bound to the principal's tenant, user AND verified workspace.

    Sets three transaction-local GUCs — `app.tenant_id`, `app.user_id` and
    `app.workspace_id` — so that when Batch 5C forces workspace RLS on the 17
    content tables, the policies have something to read. Until then this is
    behaviourally a `tenant_connection` that also carries the workspace, which
    is the point: the context can be threaded, tested and proved correct before
    anything depends on it, rather than at the same time.

    **The workspace must already be verified.** This function does not check
    membership — `core.identity.workspace.select_workspace()` does, and it is
    the only thing that may set `Principal.workspace_id`. What this does check
    is that the principal is internally consistent, because the failure it
    guards against is not a forged request but an ordinary bug: a workspace
    context left over from an earlier principal, or one whose tenant no longer
    matches the tenant the connection is about to bind.

    Why no fallback. There is deliberately no branch here that selects the
    default workspace when `workspace_id` is None. Such a branch would make
    every unscoped call site silently correct-looking and silently wrong — the
    write lands in the default workspace, RLS accepts it because the GUC was
    set, and nothing anywhere reports that the caller never knew which workspace
    it meant. Raising is the whole design.
    """
    if principal is None:
        raise MissingWorkspaceContext(
            "workspace_connection() requires a Principal — refusing to open an "
            "unscoped connection to customer data."
        )
    if getattr(principal, "tenant_id", None) is None:
        raise MissingWorkspaceContext(
            f"Principal {getattr(principal, 'user_id', '?')} has no tenant_id."
        )
    # Raises MissingWorkspaceContext with a message naming the fix.
    workspace_id = principal.require_workspace()

    from utils.db import acquire

    async with acquire() as conn:
        async with conn.transaction():
            # All three parameterised — no identifier is ever interpolated into
            # SQL text, and all three are transaction-local, so a pooled
            # connection returns carrying none of them.
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(principal.tenant_id)
            )
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)", str(principal.user_id)
            )
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, true)", str(workspace_id)
            )
            # The tenant/workspace pair is re-checked against the database
            # INSIDE the bound transaction rather than trusted from the
            # principal. `workspaces` is tenant-scoped with forced RLS, so this
            # row is only visible if the workspace really is in the tenant just
            # bound — which makes the check use the same boundary the policies
            # use, instead of a second opinion that could disagree with it.
            #
            # It costs one indexed primary-key lookup per workspace transaction.
            # That is worth paying: a mismatched pair means the GUCs describe a
            # combination that does not exist, and every policy downstream would
            # be evaluating against a fiction.
            ok = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM workspaces WHERE id = $1 AND tenant_id = $2)",
                workspace_id,
                principal.tenant_id,
            )
            if not ok:
                raise MissingWorkspaceContext(
                    f"Workspace {workspace_id} is not in tenant "
                    f"{principal.tenant_id}, or is not visible to this "
                    f"connection. Refusing to run workspace-scoped work against "
                    f"a tenant/workspace pair that does not exist. The "
                    f"transaction is rolled back, so no context persists."
                )
            yield conn


@asynccontextmanager
async def identity_bootstrap_connection(user_id) -> AsyncGenerator[object, None]:
    """A connection bound to ONE user and deliberately to no tenant.

    The bootstrap read, and the only one there is. Resolving a cloud principal
    has to answer "which tenants is this user a member of?" *before* a tenant is
    known — so `tenant_connection` cannot be used, because it requires the
    answer as an argument.

    ── Why this is not a hole ──────────────────────────────────────────────

    It binds `app.user_id` and nothing else, so it is strictly WEAKER than any
    other connection in this module, not stronger:

      * `tenant_memberships` has a SELECT policy of
        `tenant_id = app.tenant_id OR user_id = app.user_id`. With only the
        user bound, the first disjunct is NULL and the second matches exactly
        this user's own membership rows. The caller cannot enumerate another
        user's memberships even if the query asked it to.
      * Every tenant-scoped table keys on `app.tenant_id`, which is unset here.
        Under FORCE RLS an unset GUC is NULL, `tenant_id = NULL` is NULL rather
        than true, and the table matches **zero rows**. So this connection
        cannot read customer data at all — not by policy review, but by
        construction.

    That second property is the same fail-closed behaviour that made the P2-05
    defect invisible, used deliberately here instead of by accident: a
    bootstrap connection that is silently blind to tenant data is exactly what
    a bootstrap connection should be.

    It is NOT a general-purpose escape hatch, and it must not become one.
    `tests/architecture/` pins its call sites; adding one is a decision to be
    argued, not a convenience.
    """
    if user_id is None:
        raise MissingTenantContext(
            "identity_bootstrap_connection() requires a user_id. Binding "
            "neither a tenant nor a user would leave a connection that matches "
            "no rows anywhere, which is a silent no-op rather than an error."
        )

    from utils.db import acquire

    async with acquire() as conn:
        async with conn.transaction():
            # Parameterised; the id is never interpolated into SQL text.
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)", str(user_id)
            )
            yield conn


@asynccontextmanager
async def provisioning_connection(tenant_id, user_id) -> AsyncGenerator[object, None]:
    """A connection for a tenant that does not have an owner yet.

    Provisioning has a genuine chicken-and-egg: `organisations`, `workspaces`,
    `tenant_memberships` and `workspace_memberships` are all FORCE RLS keyed on
    `app.tenant_id`, so the very first rows of a tenant cannot be written
    without that GUC bound — and it cannot come from a Principal, because a
    Principal requires the membership this connection exists to create.

    ── Why this is not a bypass ────────────────────────────────────────────

    It binds a tenant, so it is genuinely stronger than
    `identity_bootstrap_connection` and the argument for it has to be
    correspondingly stronger. "The caller only passes an id it just generated"
    is a usage convention, and a usage convention is not a boundary.

    So the constraint is enforced here, in the database, before the caller gets
    the connection: **if the named tenant already has any membership that
    grants access, this refuses.** An established tenant has an owner by
    definition, so this helper cannot be pointed at one — not by review, but
    because the check runs inside the same transaction the caller would use.

    The read is legitimate under RLS rather than around it: with
    `app.tenant_id` bound, `tenant_memberships`'s SELECT policy
    (`tenant_id = app.tenant_id OR user_id = app.user_id`) matches this
    tenant's rows, so the guard sees exactly what it needs and nothing else.

    The transaction is the caller's. Everything provisioning writes — the
    tenant row included — happens inside it, so a half-provisioned tenant
    (a `tenants` row with no organisation, no workspace and no owner) is not a
    state that can be committed.

    `tests/architecture/` pins its call sites, as it does for the bootstrap.
    """
    if tenant_id is None or user_id is None:
        raise MissingTenantContext(
            "provisioning_connection() requires both a tenant_id and a "
            "user_id. Binding one without the other would leave the founding "
            "membership INSERT unable to satisfy its own policy, which fails "
            "as a silent no-op rather than as an error."
        )

    from utils.db import acquire

    async with acquire() as conn:
        async with conn.transaction():
            # Parameterised; neither id is ever interpolated into SQL text.
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
            )
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)", str(user_id)
            )

            established = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM tenant_memberships
                     WHERE tenant_id = $1 AND membership_grants_access
                )
                """,
                tenant_id,
            )
            if established:
                # Raised INSIDE the transaction, so it rolls back and no
                # context survives. The message names no member and no role:
                # this is reachable by a caller who has proved nothing.
                raise MissingTenantContext(
                    f"tenant {tenant_id} already has an active membership, so "
                    f"it is not being provisioned — it is established. "
                    f"provisioning_connection() binds a tenant without a "
                    f"Principal and must never be a route into one that "
                    f"already has an owner."
                )

            yield conn


@asynccontextmanager
async def scheduler_connection(tenant_id) -> AsyncGenerator[object, None]:
    """A connection bound to ONE tenant, for background enumeration (P4-11).

    The retention scheduler has to answer "which users in this tenant have a
    retention window", and `retention_settings` is FORCE RLS keyed on
    `app.tenant_id`. There is no principal to bind: the scheduler acts for
    nobody, and inventing one would mean choosing a user whose authority it then
    borrows.

    ── Why this is not a bypass ────────────────────────────────────────────

    It binds a tenant and NOTHING else. `app.user_id` is cleared, so every
    user-scoped policy — `totp_secrets`, `user_mfa_keys`, `auth_events`,
    `memory_embeddings`' owner clauses — matches nothing through this
    connection. It can see a tenant's shape, not its people's secrets.

    It is strictly narrower than `owner_connection`, which binds a tenant AND a
    workspace and is forbidden for cloud-scoped data anyway. It is narrower than
    `provisioning_connection`, which binds a user as well because the founding
    membership INSERT has to satisfy a policy naming one.

    ── Why not just one unbound query across every tenant ──────────────────

    Because that is the P2-05 shape: `retention_settings` forces RLS, an unbound
    connection matches **zero rows silently**, and the scheduler would report a
    successful pass having enumerated nothing. The first draft of the scheduler
    used `owner_connection` and appeared to work only because the test harness
    ran as a superuser — RLS was not enforced at all.

    The caller loops over `tenants`, which carries no RLS because it is the
    registry rather than tenant-scoped data. One tenant is bound at a time, so
    no connection ever holds two tenants' rows in scope.
    """
    if tenant_id is None:
        raise MissingTenantContext(
            "scheduler_connection() requires a tenant_id. An unbound connection "
            "matches zero rows under FORCE RLS and would report an empty tenant "
            "as a tenant with nothing to do.")

    from utils.db import get_pool

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            # Explicitly cleared, not merely unset: a pooled connection that
            # served a request a moment ago could otherwise carry that user's
            # id into a background pass.
            await conn.execute("SELECT set_config('app.user_id', '', true)")
            await conn.execute("SELECT set_config('app.workspace_id', '', true)")
            yield conn


@asynccontextmanager
async def invitation_connection(token_hash: str, user_id) -> AsyncGenerator[object, None]:
    """A connection for accepting an invitation, scoped by the token presented.

    The third ordering problem in this module, and the same shape as the other
    two: the invitee is authenticated but is NOT yet a member of the tenant they
    are joining, so there is no Principal and no tenant to bind — and the tenant
    they will join must be READ, never supplied, or the invitation becomes a way
    to name a tenant.

    ── The tenant is derived, in three steps that cannot be reordered ──────

    1. Bind `app.invitation_token_hash`. `tenant_invitations` is FORCE RLS with
       a bearer policy on exactly that setting, so the connection can now see
       one row: the invitation whose 256-bit token the caller already holds. An
       unbound connection sees none.
    2. Read the tenant FROM THAT ROW. This is the whole point. The caller never
       states a tenant; possession of the token selects one.
    3. Bind `app.tenant_id` to what step 2 found, and `app.user_id` to the
       authenticated invitee. Every write after this — the membership INSERT,
       the workspace membership, the consuming UPDATE — is governed by the
       ordinary tenant policies.

    The bearer policy is SELECT-only (055 asserts it), so possession of a link
    never authorises a write. Writes are authorised by the tenant binding, and
    the tenant binding is derived from the row.

    ── What this is NOT ────────────────────────────────────────────────────

    It is not a way to bind an arbitrary tenant: an unknown, revoked-and-purged
    or malformed token yields no row, and this refuses rather than binding
    nothing — because a connection bound to no tenant under FORCE RLS matches
    zero rows *silently*, which is the P2-05 failure mode and the last thing an
    acceptance path should do.

    It deliberately does NOT check expiry, prior consumption or identity match.
    Those are the acceptance DECISION and belong in one conditional UPDATE in
    `core/identity/invitations.py`, where they cannot be separated from the
    write they gate. Splitting them across a check here and a write there is
    exactly the window `verify_email` was written to avoid.

    `tests/architecture/` pins its call sites.
    """
    if not token_hash or user_id is None:
        raise MissingTenantContext(
            "invitation_connection() requires a token hash and the "
            "authenticated invitee's user id. Without both, the acceptance "
            "would run unbound and match no rows silently."
        )

    from utils.db import acquire

    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.invitation_token_hash', $1, true)",
                token_hash,
            )

            tenant_id = await conn.fetchval(
                "SELECT tenant_id FROM tenant_invitations WHERE token_hash = $1",
                token_hash,
            )
            if tenant_id is None:
                # No such invitation — or one the bearer policy will not show,
                # which is the same thing from here. Raised inside the
                # transaction so it rolls back and no context survives.
                raise MissingTenantContext(
                    "no invitation matches that token"
                )

            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
            )
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)", str(user_id)
            )
            yield conn


@asynccontextmanager
async def owner_connection(reason: str = "") -> AsyncGenerator[object, None]:
    """A connection bound to the single private tenant. **Private edition only.**

    Blocker B1. Six tables — `files`, `commitments`, `lifeos_events`,
    `cognitive_events`, `privacy_envelopes`, `envelope_members` — predate Phase 2
    and were read through a bare `acquire()` with no tenant bound.
    `031_pretenancy_isolation.sql` gives them `tenant_id` and forced RLS, and
    under forced RLS an unbound connection matches **no rows at all** — silently,
    without erroring. So every one of those call sites needs a bound tenant, and
    this is where it comes from.

    Why a separate function rather than `tenant_connection(principal)`: these
    subsystems are single-owner by construction. `lifeos/events.py` takes a
    `user_id: str`, `cognition/store.py` takes an owner name, and
    `worker/heartbeat.py` runs on a schedule with no request and therefore no
    Principal at all. Threading a Principal through all of them would be a
    larger and riskier change than the isolation it is meant to enable, and it
    would invent a Principal in the worker — which is worse, because an invented
    Principal looks exactly like a real one at the call site.

    Naming it for what it is makes every use greppable, and makes the one thing
    that must never happen impossible:

    **It refuses in the cloud edition.** None of these subsystems is offered in
    cloud (`docs/privacy/DATA_PROCESSING_INVENTORY.md` records no cloud write
    path), so a cloud process reaching one is a bug. Binding the private tenant
    there would hand a cloud request the owner's LifeOS, files and cognition —
    the precise failure this whole retrofit exists to prevent. Refusing turns
    that from a silent cross-edition read into a loud error.

    `reason` is recorded in the refusal so the message names the caller rather
    than leaving an operator to find it.
    """
    from core.editions import EDITION_CLOUD, current_edition
    from core.identity.principal import PRIVATE_TENANT_ID

    if current_edition() == EDITION_CLOUD:
        raise MissingTenantContext(
            f"owner_connection() was called in the cloud edition"
            f"{f' ({reason})' if reason else ''}. It binds the single private "
            f"tenant, and the subsystems that use it — LifeOS, cognition, "
            f"privacy envelopes, files — are Private-edition only. Refusing "
            f"rather than serving one tenant's data to a multi-tenant process. "
            f"If this subsystem is genuinely wanted in cloud it needs a real "
            f"Principal and `tenant_connection`, not this."
        )

    from utils.db import acquire

    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(PRIVATE_TENANT_ID),
            )
            # `app.user_id` is deliberately left UNSET here, not set to the
            # private owner. This connection exists for subsystems that have no
            # Principal — the heartbeat worker has no request at all — so there
            # is no authenticated person to name. Binding the owner's id would
            # hand a scheduled job read access to the owner's TOTP secret and
            # voiceprint, which is precisely what the USER_PRIVATE policies are
            # for. Unset means those tables return nothing, which is correct:
            # none of the subsystems this function serves reads them.
            await conn.execute("SELECT set_config('app.user_id', '', true)")

            # ── The workspace, since 049 ────────────────────────────────────
            #
            # Six of the tables this connection exists to serve — `files`,
            # `commitments`, `lifeos_events` among them — are workspace-scoped,
            # and 049's policies require BOTH settings. Binding only the tenant
            # now means those tables read zero rows and refuse every insert.
            #
            # So this binds the private tenant's canonical default workspace,
            # which is the connection-level counterpart of what
            # `legacy_owner_workspace()` does for an HTTP request: the private
            # edition shipped with one owner and no workspace concept, and these
            # subsystems have no request to carry a selection on.
            #
            # It is read from the database and it REFUSES on zero or on more
            # than one. Never "the first workspace" — that is the inference this
            # whole module exists to make impossible, and a private tenant that
            # somehow has two default workspaces is a state nobody should be
            # writing content into on a guess.
            rows = await conn.fetch(
                """
                SELECT id FROM workspaces
                 WHERE tenant_id = $1 AND is_default AND status = 'active'
                """,
                str(PRIVATE_TENANT_ID),
            )
            if len(rows) != 1:
                raise MissingTenantContext(
                    f"owner_connection() found {len(rows)} active default "
                    f"workspaces for the private tenant"
                    f"{f' ({reason})' if reason else ''}, and needs exactly one. "
                    f"Since 049 the workspace-scoped tables require a bound "
                    f"workspace, so this connection cannot serve them without "
                    f"one — and it will not choose among several. Run "
                    f"`provision_tenant_substrate` if there are none; if there "
                    f"are two, the `033` trigger that permits only one active "
                    f"default has been dropped and that is the real fault."
                )
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, true)", str(rows[0]["id"])
            )
            yield conn


@asynccontextmanager
async def voiceprint_connection(reason: str = "") -> AsyncGenerator[object, None]:
    """The ONLY connection permitted to reach `voice_profiles`. Private edition only.

    P12-04 finding J, and it is the reason `owner_connection` above cannot be
    used here. That function deliberately leaves `app.user_id` UNSET, and says
    why in its own comment: *"Binding the owner's id would hand a scheduled job
    read access to the owner's TOTP secret and voiceprint, which is precisely
    what the USER_PRIVATE policies are for."* That reasoning is right and stands.
    The consequence nobody followed up is that `voice/biometrics.py` was left
    with nothing it could use, and it went on calling a bare `acquire()`.

    **What that cost, measured rather than reasoned about.** `037` put
    `voice_profiles` under FORCE ROW LEVEL SECURITY with
    `USING (user_id = app.user_id)`. On an unbound connection that predicate is
    `NULL`, so:

        save_owner_profile(...)   -> new row violates row-level security policy
                                     ...caught by the enclosing `except`, logged,
                                     returns False. Enrolment has been impossible.
        _load_owner_profile()     -> zero rows -> None -> is_owner_authenticated
                                     returns False. Voice has authenticated
                                     nobody since `037`.

    Both directions fail CLOSED, which is why it survived: voice degrading to
    public access looks exactly like "the owner has not enrolled yet".
    `scripts/phase4/encrypt_voiceprints.py` already refuses to run against this
    table for precisely this reason and says so at length — the hazard was
    understood for the offline worker and never carried to the runtime.

    **What this binds, and why that is narrow rather than a bypass.**
    `app.user_id` is set to `private_user_id(ira_admin_username)` — a
    deterministic UUIDv5 over the deployment's *login* name, not the
    `owner_name` display string that `037` deliberately stopped treating as an
    identity. It grants exactly one person's own row and nothing else; RLS still
    applies in full, and every other USER_PRIVATE table (`totp_secrets`) stays
    invisible to it because they key on the same GUC and this connection is only
    ever used by the voice module.

    **It refuses in the cloud edition**, on the same ground `is_owner()` fails
    closed there: `ira_admin_username` is one deployment-wide string, a username
    is per-tenant, and deriving deployment-wide owner authority from it in a
    multi-tenant process would hand one tenant's `admin` the biometric template
    of another's. Voice biometrics are a Private-edition subsystem; a cloud
    process reaching this is a bug, and a loud one is better than a template
    that opens for the wrong body.
    """
    from core.editions import EDITION_CLOUD, current_edition
    from core.identity.principal import PRIVATE_TENANT_ID, private_user_id

    if current_edition() == EDITION_CLOUD:
        raise MissingTenantContext(
            f"voiceprint_connection() was called in the cloud edition"
            f"{f' ({reason})' if reason else ''}. It derives the owner's "
            f"identity from `ira_admin_username`, which is one deployment-wide "
            f"string and therefore not an identity in a multi-tenant process. "
            f"Voice biometrics are Private-edition only. Refusing rather than "
            f"binding one deployment's 'admin' to whichever tenant asked."
        )

    from config import get_settings
    from utils.db import acquire

    owner_id = private_user_id(get_settings().ira_admin_username)

    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(PRIVATE_TENANT_ID),
            )
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)", str(owner_id)
            )
            # No workspace. `voice_profiles` and `biometric_audit` carry no
            # workspace predicate — P12-01 classified the module NOT_APPLICABLE
            # for exactly that reason — and binding one here would suggest a
            # scoping this table does not have.
            yield conn


def voiceprint_owner_id():
    """The `user_id` a voiceprint row must carry to be visible to its owner.

    Exported so `save_owner_profile` writes the same value the policy checks
    against, from one definition rather than two that can drift. A row written
    with any other `user_id` is refused by `WITH CHECK` at INSERT time, which is
    the loud failure that replaces the silent one.
    """
    from config import get_settings
    from core.identity.principal import private_user_id

    return private_user_id(get_settings().ira_admin_username)


async def current_tenant(conn) -> str | None:
    """The tenant id this connection is currently scoped to, or None.

    Intended for assertions and diagnostics — a cheap way for a test or a health
    check to prove the context is what it should be.
    """
    value = await conn.fetchval("SELECT current_setting('app.tenant_id', true)")
    return value or None


async def current_workspace(conn) -> str | None:
    """The workspace id this connection is currently scoped to, or None.

    The counterpart to :func:`current_tenant`, and the assertion the batch 5A
    tests use to prove `SET LOCAL` really is local: read it after the
    transaction ends and it is gone.
    """
    value = await conn.fetchval("SELECT current_setting('app.workspace_id', true)")
    return value or None


def assert_workspace_scoped(principal: Principal | None) -> Principal:
    """Guard for call sites that must not run without a verified workspace.

    The workspace equivalent of :func:`assert_tenant_scoped`, for the Batch 5C
    migration: a function whose signature cannot yet take a workspace can call
    this at the top and fail loudly rather than writing somewhere arbitrary.
    """
    principal = assert_tenant_scoped(principal)
    principal.require_workspace()
    return principal


def assert_tenant_scoped(principal: Principal | None) -> Principal:
    """Guard for call sites that must not run without a tenant.

    Use at the top of any function touching customer data whose signature cannot
    be changed to require a Principal yet, so the failure is a loud error rather
    than a silent unscoped query.
    """
    if principal is None or getattr(principal, "tenant_id", None) is None:
        raise MissingTenantContext(
            "This operation touches customer data and requires an authenticated "
            "tenant context."
        )
    return principal


__all__ = [
    "tenant_connection",
    "identity_bootstrap_connection",
    "owner_connection",
    "workspace_connection",
    "current_tenant",
    "current_workspace",
    "assert_tenant_scoped",
    "assert_workspace_scoped",
    "MissingTenantContext",
    "MissingWorkspaceContext",
]
