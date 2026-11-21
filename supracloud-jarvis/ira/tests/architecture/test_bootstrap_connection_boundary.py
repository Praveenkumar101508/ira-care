"""`identity_bootstrap_connection` is a bootstrap, and must not become a hatch.

It is the one connection helper that binds a user and deliberately no tenant.
That is necessary — resolving a cloud principal has to ask "which tenants is
this user in?" before a tenant is known, so `tenant_connection` cannot be used,
because it takes the answer as an argument.

It is also, read quickly, the most attractive thing in `db_context` to reach
for: it needs no Principal. Every other helper in that module demands one, and
demanding one is most of what they are for.

Its docstring says *"tests/architecture/ pins its call sites"*. This is that
file. A docstring promising a guard that does not exist is the exact shape of
defect this codebase keeps finding — `tenant_connection`'s own docstring
described the P2-05 failure mode precisely while nothing called it — so the
claim is made true here rather than left as prose.

── What is actually enforced ───────────────────────────────────────────────

The allow-list is a set of modules, not a count of calls, because the number of
calls is not the interesting property — *which module* may open an unscoped-by-
tenant connection is. Adding a module here is a decision to argue, and the
pinning test below makes it one.

Note what this does NOT need to enforce: the helper cannot read tenant data.
It binds no `app.tenant_id`, so under FORCE RLS every tenant-scoped table
matches zero rows. That is proved against a real database in
`tests/tenancy/test_p2_cloud_identity.py::test_the_bootstrap_connection_cannot_
read_tenant_data`. This file guards the blast radius; that one guards the
capability.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# tests/architecture/ -> tests/ -> ira/
IRA_ROOT = Path(__file__).resolve().parents[2]

HELPER = "identity_bootstrap_connection"

#: Modules permitted to call it, and why. Both are the identity bootstrap
#: itself: one defines the helper, the other is the resolution that needs it.
ALLOWED: dict[str, str] = {
    "core/identity/db_context.py":
        "defines the helper",
    "core/identity/resolver.py":
        "cloud principal resolution — the one place that must read a user's "
        "memberships before any tenant is known",
    "core/identity/cloud_login.py":
        "reads the account's credential row to verify a password. Same table, "
        "same FORCE RLS on `app.user_id`, and the same reason there is no "
        "Principal to bind: the caller is unauthenticated until this read "
        "succeeds. It binds the id of the user the email lookup just found, so "
        "it can reach exactly one credential row — and no tenant is bound, so "
        "the pre-authentication connection can read no tenant data at all",
    "core/identity/registration.py":
        "writes the new account's credential row. `user_credentials` is FORCE "
        "RLS keyed on `app.user_id`, and at registration the user exists but "
        "has no tenant and no membership — so there is no Principal to bind "
        "and tenant_connection cannot be used. The connection is bound to the "
        "id this module just generated, so it can reach exactly one row: the "
        "one it is creating",
    "core/identity/password_reset.py":
        "enumerates the account's memberships in order to end every session, "
        "in every tenant, when a password is reset. This is the case the "
        "assertion message below names as the legitimate one: a user's "
        "memberships must be read BEFORE any tenant is known, because the "
        "whole point is to reach all of them rather than a selected one. The "
        "first version used a bare `acquire()` and found nothing — "
        "`tenant_memberships` is tenant-scoped under FORCE RLS, so an unbound "
        "connection matches zero rows silently, and the reset reported success "
        "having left every session alive. That is the P2-05 shape in the one "
        "place where the consequence is that the attacker the user was "
        "resetting against stays signed in",
    "core/identity/external_identity.py":
        "reads and writes an account's provider links. Two reasons, and both "
        "are the absence of a tenant rather than a convenience: this table "
        "carries NO tenant column at all, and a federated-only account holds no "
        "membership to bind one from. `Principal` refuses to exist without a "
        "tenant — correctly — so an account-level operation like listing or "
        "unlinking a provider cannot take one, and tenant_connection is not "
        "available to it. The connection binds the id of the account the caller "
        "is already authenticated as",
}


#: `provisioning_connection` binds a tenant with no Principal, so it is
#: strictly STRONGER than the bootstrap and its allow-list is correspondingly
#: shorter. Unlike the bootstrap, it also carries a structural guard of its own
#: — it refuses a tenant that already has an access-granting membership — so
#: this file pins who may call it, and
#: `tests/tenancy/test_p2_provisioning.py::test_provisioning_connection_refuses_
#: an_established_tenant` proves the guard those callers are trusted behind.
PROVISIONING_HELPER = "provisioning_connection"

#: `invitation_connection` binds a tenant too, but derives it from a row the
#: caller proved possession of rather than from an id they generated. Its
#: allow-list is separate and equally short.
INVITATION_HELPER = "invitation_connection"

INVITATION_ALLOWED: dict[str, str] = {
    "core/identity/db_context.py": "defines the helper",
    "core/identity/invitations.py":
        "invitation acceptance — the only place that turns possession of an "
        "invitation token into a membership, and the only one that needs a "
        "tenant bound for a user who is not yet a member of it",
}

PROVISIONING_ALLOWED: dict[str, str] = {
    "core/identity/db_context.py":
        "defines the helper",
    "core/identity/tenant_provisioning.py":
        "the founding transaction — the one place that writes a tenant's first "
        "organisation, workspace and memberships, none of which can be written "
        "without app.tenant_id bound and none of which have a Principal to "
        "bind it from",
}


def _skip(path: Path) -> bool:
    parts = path.parts
    return (
        "tests" in parts
        or "__pycache__" in parts
        or ".venv" in parts
        or "site-packages" in parts
        or "future-scale" in parts
    )


def _modules() -> list[Path]:
    return sorted(p for p in IRA_ROOT.rglob("*.py") if not _skip(p))


def _calls_named(path: Path, name: str) -> bool:
    """Does this module CALL `name` — not merely mention it?"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == name:
                return True
            if isinstance(fn, ast.Attribute) and fn.attr == name:
                return True
    return False


def _calls_helper(path: Path) -> bool:
    """Does this module CALL the helper — not merely mention it?

    An AST walk rather than a substring search, and the distinction has already
    mattered once in this repository: a guard written as `name in source`
    passed with the guarded call deleted, because an `import` line still
    satisfied the substring. A comment naming the helper is not a call either,
    and a file explaining why it does not use it should not be flagged for
    saying so.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == HELPER:
                return True
            if isinstance(fn, ast.Attribute) and fn.attr == HELPER:
                return True
    return False


def test_the_scan_finds_modules_at_all():
    """A scanner that walks nothing passes everything."""
    modules = _modules()
    assert len(modules) > 100, f"only {len(modules)} modules scanned"
    assert any("core/identity" in p.as_posix() for p in modules)


def test_only_the_identity_bootstrap_opens_a_tenantless_connection():
    callers = {
        p.relative_to(IRA_ROOT).as_posix()
        for p in _modules() if _calls_helper(p)
    }
    unexpected = sorted(callers - set(ALLOWED))
    assert not unexpected, (
        f"{unexpected} call {HELPER}(). It binds a user and NO tenant, so it "
        f"is the one helper in db_context that does not require a Principal — "
        f"which makes it the easy thing to reach for and the wrong thing to "
        f"reach for. Tenant-scoped work must use tenant_connection(principal). "
        f"If a module genuinely needs to read a user's memberships before any "
        f"tenant is known, add it to ALLOWED with the reason."
    )


def test_the_allow_list_is_exactly_what_is_expected():
    """Growing the list has to be a decision, not a convenience.

    Without this, ALLOWED is simply the place to put whatever the scanner
    caught, and an allow-list nobody has to argue with is not a boundary.
    """
    assert set(ALLOWED) == {
        "core/identity/cloud_login.py",
        "core/identity/db_context.py",
        "core/identity/external_identity.py",
        "core/identity/password_reset.py",
        "core/identity/registration.py",
        "core/identity/resolver.py",
    }, (
        f"the bootstrap-connection allow-list changed to {sorted(ALLOWED)}. "
        f"Each entry grants a module the ability to open a connection with no "
        f"tenant bound. Justify it above and update this assertion "
        f"deliberately."
    )


@pytest.mark.parametrize("module", sorted(ALLOWED))
def test_every_allowed_module_exists(module: str):
    """A stale entry fails OPEN: it silently covers a path that moved."""
    assert (IRA_ROOT / module).is_file(), (
        f"{module} is on the bootstrap-connection allow-list but does not "
        f"exist. Either it moved — in which case the exemption now covers "
        f"nothing — or the entry is stale."
    )


def test_the_scanner_detects_a_planted_violation(tmp_path):
    """Proves the scanner works, so a broken scanner cannot pass this file."""
    offender = tmp_path / "leaky.py"
    offender.write_text(
        "from core.identity.db_context import identity_bootstrap_connection\n"
        "async def go(uid):\n"
        "    async with identity_bootstrap_connection(uid) as c:\n"
        "        return await c.fetch('SELECT 1')\n",
        encoding="utf-8",
    )
    assert _calls_helper(offender)

    mentions_only = tmp_path / "polite.py"
    mentions_only.write_text(
        "# identity_bootstrap_connection is deliberately NOT used here.\n"
        "from core.identity.db_context import identity_bootstrap_connection\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )
    assert not _calls_helper(mentions_only), (
        "the scanner flagged a module that only imports and mentions the "
        "helper. A guard that matches an import matches the absence of the "
        "thing it guards."
    )


# ─────────────────────────────────────────────────────────────────────────────
# `provisioning_connection` — the stronger helper, the shorter list
# ─────────────────────────────────────────────────────────────────────────────


def test_only_the_founding_transaction_binds_a_tenant_without_a_principal():
    callers = {
        p.relative_to(IRA_ROOT).as_posix()
        for p in _modules() if _calls_named(p, PROVISIONING_HELPER)
    }
    unexpected = sorted(callers - set(PROVISIONING_ALLOWED))
    assert not unexpected, (
        f"{unexpected} call {PROVISIONING_HELPER}(). It binds app.tenant_id "
        f"with no Principal behind it, which is strictly more than the "
        f"bootstrap connection does. It exists so a tenant's FIRST rows can be "
        f"written; anything after that has a membership and must go through "
        f"tenant_connection(principal)."
    )


def test_the_provisioning_allow_list_is_exactly_what_is_expected():
    assert set(PROVISIONING_ALLOWED) == {
        "core/identity/db_context.py",
        "core/identity/tenant_provisioning.py",
    }, (
        f"the provisioning-connection allow-list changed to "
        f"{sorted(PROVISIONING_ALLOWED)}. Each entry grants a module the "
        f"ability to bind an arbitrary tenant. Justify it above and update "
        f"this assertion deliberately."
    )


@pytest.mark.parametrize("module", sorted(PROVISIONING_ALLOWED))
def test_every_provisioning_allowed_module_exists(module: str):
    """A stale entry fails OPEN: it silently covers a path that moved."""
    assert (IRA_ROOT / module).is_file(), (
        f"{module} is on the provisioning-connection allow-list but does not "
        f"exist. Either it moved — in which case the exemption now covers "
        f"nothing — or the entry is stale."
    )


def test_the_two_allow_lists_are_not_the_same_list():
    """They guard different capabilities and must not drift into one.

    The bootstrap binds a user; provisioning binds a tenant. If the second list
    ever grew to match the first, every module allowed to read its own
    memberships would also be allowed to write another tenant's founding rows.
    """
    assert set(PROVISIONING_ALLOWED) < set(ALLOWED) | set(PROVISIONING_ALLOWED)
    assert "core/identity/resolver.py" not in PROVISIONING_ALLOWED
    assert "core/identity/cloud_login.py" not in PROVISIONING_ALLOWED


def test_the_provisioning_scanner_detects_a_planted_violation(tmp_path):
    """Proves the scanner works, so a broken scanner cannot pass this file."""
    offender = tmp_path / "greedy.py"
    offender.write_text(
        "from core.identity.db_context import provisioning_connection\n"
        "async def go(t, u):\n"
        "    async with provisioning_connection(t, u) as c:\n"
        "        return await c.fetch('SELECT 1')\n",
        encoding="utf-8",
    )
    assert _calls_named(offender, PROVISIONING_HELPER)

    mentions_only = tmp_path / "polite.py"
    mentions_only.write_text(
        "# provisioning_connection is deliberately NOT used here.\n"
        "from core.identity.db_context import provisioning_connection\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )
    assert not _calls_named(mentions_only, PROVISIONING_HELPER)


# ─────────────────────────────────────────────────────────────────────────────
# Two modules named "provisioning", and they must both keep existing
# ─────────────────────────────────────────────────────────────────────────────


def test_both_provisioning_modules_exist_and_are_distinct():
    """`provisioning.py` and `tenant_provisioning.py` do unrelated jobs.

    The first is the PRIVATE edition's owner bootstrap: it gives the one
    configured `.env` owner the identity rows `023`/`035` seed for a hard-coded
    UUID, and its own docstring says it *"does not run in the cloud edition"*
    and *"does not create a tenant, an organisation or a workspace"*. The
    second creates exactly those, for a customer.

    They are asserted together because the names are one word apart, and during
    batch 4 the cloud module was written straight over the private one — which
    deleted `ensure_private_owner_identity` outright. Nothing noticed until an
    unrelated Phase 3B suite failed on the import three steps later, which is
    the expensive way to find out. This is the cheap way.
    """
    import importlib

    private = importlib.import_module("core.identity.provisioning")
    cloud = importlib.import_module("core.identity.tenant_provisioning")

    assert hasattr(private, "ensure_private_owner_identity"), (
        "core/identity/provisioning.py no longer exports "
        "ensure_private_owner_identity. That is the private edition's owner "
        "bootstrap — without it, every workspace-scoped endpoint answers 403 "
        "on a deployment whose IRA_ADMIN_USERNAME is not the seeded default.")
    assert hasattr(cloud, "provision_tenant"), (
        "core/identity/tenant_provisioning.py no longer exports "
        "provision_tenant — cloud accounts cannot be given a tenant.")

    assert not hasattr(private, "provision_tenant"), (
        "the private owner bootstrap has grown a cloud entry point. Keeping "
        "them apart is what stops 'provisioning' meaning two things in one "
        "namespace.")
    assert not hasattr(cloud, "ensure_private_owner_identity")


def test_only_invitation_acceptance_binds_a_tenant_from_a_token():
    callers = {
        p.relative_to(IRA_ROOT).as_posix()
        for p in _modules() if _calls_named(p, INVITATION_HELPER)
    }
    unexpected = sorted(callers - set(INVITATION_ALLOWED))
    assert not unexpected, (
        f"{unexpected} call {INVITATION_HELPER}(). It binds app.tenant_id to "
        f"whatever tenant an invitation row names, so anything that can reach "
        f"it with a token can act inside that tenant. Only the acceptance path "
        f"needs that, and only for the length of one transaction."
    )


def test_the_invitation_allow_list_is_exactly_what_is_expected():
    assert set(INVITATION_ALLOWED) == {
        "core/identity/db_context.py",
        "core/identity/invitations.py",
    }, (
        f"the invitation-connection allow-list changed to "
        f"{sorted(INVITATION_ALLOWED)}. Justify it above and update this "
        f"assertion deliberately."
    )


def test_the_three_tenantless_helpers_have_three_separate_lists():
    """One list per capability, because they are not the same capability.

    The bootstrap binds a user. Provisioning binds a tenant the caller just
    created. Invitation binds a tenant a token names. Merging any two would
    grant every module on one list the other's reach.
    """
    assert set(PROVISIONING_ALLOWED) != set(INVITATION_ALLOWED)
    assert "core/identity/invitations.py" not in PROVISIONING_ALLOWED
    assert "core/identity/tenant_provisioning.py" not in INVITATION_ALLOWED
