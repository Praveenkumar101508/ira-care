#!/usr/bin/env python3
"""Verify every path and symbol cited by the Phase 2 auth inventory.

An inventory is only worth having if a reader can trust it without redoing it.
The failure mode is not a wrong claim about behaviour — that is visible on
review — it is a claim about a **file or function that does not exist**, which
reads as authoritative and wastes the time of whoever tries to follow it.

This parses `docs/cloud/PHASE_2_AUTH_INVENTORY.md`, pulls out every backticked
token that looks like a path or a symbol, and checks each one against the tree.
Paths are checked with `pathlib`; symbols are checked by **parsing** the module
with `ast` rather than grepping, so a name that only appears in a docstring or
a comment does not count as defined.

Exit code 0 when everything resolves, 1 otherwise. Run it after every edit.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[4]
IRA = REPO / "supracloud-jarvis" / "ira"
DOC = REPO / "docs" / "cloud" / "PHASE_2_AUTH_INVENTORY.md"

#: A backticked token that looks like a file path.
PATH_RE = re.compile(r"`([\w./-]+\.(?:py|sql|md|json))`")

#: `module.py::symbol` — the strongest form, and the only one that lets the
#: checker verify the symbol against a SPECIFIC file rather than anywhere.
QUALIFIED_RE = re.compile(r"`([\w./-]+\.py)::(\w+)`")

#: Bare backticked identifiers. Checked against the union of the modules the
#: document cites, because the inventory names a symbol in a table row whose
#: "Path" cell is a separate cell.
BARE_RE = re.compile(r"`(_?[a-zA-Z][\w]*)`")

#: Tokens that are prose, config keys, SQL, or otherwise not Python symbols in
#: this tree. Listed explicitly: silently ignoring unresolved names would make
#: the checker report success while verifying less than it claims.
NOT_SYMBOLS = {
    # editions, statuses and prose
    "EDITION_CLOUD", "EDITION_PRIVATE", "Implemented", "Not", "Partially",
    "Unknown", "High", "Medium", "Low", "Critical", "done", "true", "false",
    "sub", "exp", "jti", "tok", "ver", "access", "refresh", "id", "body",
    "title", "read", "active", "role", "username", "password", "secret",
    # settings keys (config.Settings fields, not module-level symbols)
    "dev_mode", "ira_secret_key", "ira_admin_username", "is_local_domain",
    "access_token_expire_minutes", "refresh_token_expire_days",
    "telegram_chat_id", "ira_voice_service_username",
    # environment variable names, not Python symbols
    "IRA_TRUSTED_PROXY_HOPS", "DEV_MODE", "AUTH_FAIL_CLOSED_ON_REDIS",
    "IRA_CLOUD_REGISTRATION_ENABLED",
    # database objects
    "totp_secrets", "user_sessions", "notifications", "delegated_users",
    "users", "tenant_memberships", "normalised_email",
    "membership_grants_access", "users_verified_email_key",
    "idx_tenant_memberships_user_active",
    "user_credentials", "email_verification_tokens", "normalise_email",
    "organisations", "workspaces", "workspace_memberships",
    # tenant/workspace ROLE VALUES — the authority for what these may be is the
    # CHECK constraint on tenant_memberships.role and workspace_memberships.role,
    # not a Python constant, so there is nothing here for this checker to
    # resolve them against. `cloud_roles.CLOUD_TENANT_ROLES` is checked against
    # the database's own vocabulary by tests/tenancy/test_p2_cloud_identity.py.
    "primary_owner", "workspace_admin",
    # database ROLES, not Python symbols
    "ira_worker", "ira_app",
    "tenant_id", "workspace_id", "user_id", "session_id", "device_id",
    "roles", "scopes", "is_owner", "edition", "correlation", "memory_embeddings",
    # third-party / stdlib
    "bcrypt", "passlib", "slowapi", "ast", "pathlib", "HS256", "ValueError",
    "None", "True", "False", "X-Workspace-ID", "X-Forwarded-For", "lru_cache",
    "maxsize", "deprecated", "schemes", "key_func",
    # third-party symbols imported but not defined here
    "get_remote_address", "Limiter", "CryptContext",
    # method calls quoted in prose, not module-level names
    "accept",
}

#: Modules the inventory cites; bare symbols are resolved against these.
def cited_paths(text: str) -> set[str]:
    return {m for m in PATH_RE.findall(text) if m.endswith(".py")}


def defined_names(path: pathlib.Path) -> set[str]:
    """Every name DEFINED at module level (and one level into classes).

    Parsed, not grepped: a symbol mentioned in a docstring is not a definition,
    and the whole point of this checker is to catch names that read as real and
    are not.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()

    names: set[str] = set()
    # `ast.walk`, not `tree.body`: FastAPI route handlers in this codebase are
    # nested inside a registration function (`auth/routes.py::register_auth_routes`
    # defines `login`, `logout`, `refresh_token_endpoint`). Those are real,
    # findable symbols; a module-level-only scan reported them missing.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        names.add(sub.name)
                        names.add(f"{node.name}.{sub.name}")
                    elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                        names.add(f"{node.name}.{sub.target.id}")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def resolve(rel: str) -> pathlib.Path | None:
    """A cited path may be repo-, ira- or document-relative; try each."""
    for base in (REPO, IRA, DOC.parent):
        candidate = base / rel
        if candidate.exists():
            return candidate
    return None


def main() -> int:
    if not DOC.is_file():
        print(f"FAIL: inventory not found at {DOC}")
        return 1

    text = DOC.read_text(encoding="utf-8")
    path_failures: list[str] = []
    symbol_failures: list[str] = []

    # ── paths ────────────────────────────────────────────────────────────────
    paths = sorted(set(PATH_RE.findall(text)))
    resolved: dict[str, pathlib.Path] = {}
    for rel in paths:
        found = resolve(rel)
        if found is None:
            path_failures.append(rel)
        else:
            resolved[rel] = found

    # ── qualified symbols: module.py::symbol ─────────────────────────────────
    qualified = sorted(set(QUALIFIED_RE.findall(text)))
    for rel, symbol in qualified:
        found = resolve(rel)
        if found is None:
            symbol_failures.append(f"{rel}::{symbol} (module not found)")
        elif symbol not in defined_names(found):
            symbol_failures.append(f"{rel}::{symbol} (not defined in module)")

    # ── bare symbols, resolved against every cited python module ─────────────
    universe: set[str] = set()
    for rel, found in resolved.items():
        if found.suffix == ".py":
            universe |= defined_names(found)
    # A `module.py::symbol` citation does not match PATH_RE (the backtick does
    # not close after the extension), so its module was absent from the universe
    # and every bare symbol from it reported missing. Add them.
    for rel, _symbol in qualified:
        found = resolve(rel)
        if found is not None and found.suffix == ".py":
            universe |= defined_names(found)

    bare = sorted(set(BARE_RE.findall(text)))
    checked_bare = 0
    for name in bare:
        if name in NOT_SYMBOLS or name in {p.split("/")[-1] for p in paths}:
            continue
        checked_bare += 1
        if name not in universe:
            symbol_failures.append(f"{name} (not defined in any cited module)")

    total_symbols = len(qualified) + checked_bare
    print(f"paths checked:   {len(paths):<5} failed: {len(path_failures)}")
    print(f"symbols checked: {total_symbols:<5} failed: {len(symbol_failures)}")

    for bad in path_failures:
        print(f"  PATH   MISSING  {bad}")
    for bad in symbol_failures:
        print(f"  SYMBOL MISSING  {bad}")

    return 1 if (path_failures or symbol_failures) else 0


if __name__ == "__main__":
    sys.exit(main())
