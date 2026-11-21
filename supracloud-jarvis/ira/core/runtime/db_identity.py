"""What the runtime asserts about its own database role, at startup.

The application role is now supposed to own nothing and hold no DDL. That is
enforced by `042_role_ownership_separation.sql` — but a migration enforces it
only on databases the migration reached. A cluster restored from an old backup,
a hand-provisioned staging box, or a rollback of `042` all produce a runtime
that silently has more authority than it should.

Silently is the problem. This module turns it into a readiness failure.

**It refuses to be a fix.** Nothing here re-grants, re-owns or repairs anything:
a process that repaired its own privileges would need the privileges to do it,
which is the situation being detected. It reports, and the deployment is fixed
by an operator.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("ira.runtime.db_identity")

#: Roles the runtime may legitimately authenticate as.
RUNTIME_ROLES = frozenset({"ira_app", "ira_worker"})

#: Tables whose isolation the product's claims rest on. Checked explicitly
#: rather than "every table", so the failure names something an operator can act
#: on rather than producing a wall of output.
PROTECTED_TABLES = (
    "tenant_memberships", "workspace_memberships", "organisations", "workspaces",
    "totp_secrets", "voice_profiles", "biometric_audit", "delegated_users",
    "messages", "conversations", "memory_embeddings",
)


@dataclass
class Finding:
    code: str
    detail: str
    #: False for observations that are worth logging but must not take an
    #: otherwise healthy deployment offline.
    blocks_readiness: bool = True


@dataclass
class DatabaseIdentityReport:
    role: str = ""
    database: str = ""
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.blocks_readiness for f in self.findings)

    def as_dict(self) -> dict[str, Any]:
        """Deliberately coarse. See `summary()` for why."""
        return {
            "role": self.role,
            "ok": self.ok,
            "findings": [f.code for f in self.findings],
        }

    def summary(self) -> str:
        """One line, safe for a log. Codes only — never the grant details.

        An anonymous health endpoint that enumerated exactly which privileges
        the application role holds would be a reconnaissance surface: it tells
        an attacker which of these checks to expect and which already fail.
        `/readyz` publishes the check name and its pass/fail; the detail stays
        in the process log.
        """
        if self.ok:
            return f"database identity OK (role={self.role})"
        return f"database identity FAILED (role={self.role}): " + ", ".join(
            f.code for f in self.findings if f.blocks_readiness
        )


async def inspect(conn: Any) -> DatabaseIdentityReport:
    """Check the connected role against what the runtime is supposed to be."""
    report = DatabaseIdentityReport()

    row = await conn.fetchrow(
        "SELECT current_user AS role, current_database() AS db, "
        "       r.rolsuper, r.rolbypassrls, r.rolcreatedb, r.rolcreaterole "
        "  FROM pg_roles r WHERE r.rolname = current_user"
    )
    report.role, report.database = row["role"], row["db"]

    if row["rolsuper"]:
        report.findings.append(Finding(
            "role_is_superuser",
            "the runtime is connected as a superuser, which bypasses every "
            "row-level security policy. No tenant isolation claim holds."))
    if row["rolbypassrls"]:
        report.findings.append(Finding(
            "role_has_bypassrls",
            "the runtime role holds BYPASSRLS, so every policy is advisory."))
    if row["rolcreatedb"] or row["rolcreaterole"]:
        report.findings.append(Finding(
            "role_has_cluster_privileges",
            "the runtime role holds CREATEDB or CREATEROLE."))

    if report.role not in RUNTIME_ROLES:
        # A warning, not a blocker. Local development and the test harness
        # legitimately connect as other roles, and refusing to start there would
        # teach people to disable this check — which is worse than not having it.
        report.findings.append(Finding(
            "unexpected_role",
            f"connected as {report.role!r}, expected one of {sorted(RUNTIME_ROLES)}",
            blocks_readiness=False))

    owned = await conn.fetchval(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        " WHERE n.nspname = 'public' AND c.relkind IN ('r','v','m','S','p') "
        "   AND pg_get_userbyid(c.relowner) = current_user"
    )
    if owned:
        report.findings.append(Finding(
            "runtime_owns_objects",
            f"the runtime role owns {owned} object(s) in `public`. An owner can "
            f"disable forced row-level security and drop policies, so the "
            f"isolation controls are advisory (D-18, R-50)."))

    can_create = await conn.fetchval(
        "SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
    if can_create:
        report.findings.append(Finding(
            "runtime_can_create_objects",
            "the runtime role can CREATE in `public`, so it can introduce a "
            "table or function outside the migration chain."))

    # RLS state on the tables the claims rest on.
    unprotected = await conn.fetch(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
        "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        " WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname = ANY($1::text[]) "
        "   AND NOT (c.relrowsecurity AND c.relforcerowsecurity)",
        list(PROTECTED_TABLES),
    )
    if unprotected:
        report.findings.append(Finding(
            "protected_table_without_forced_rls",
            f"{[r['relname'] for r in unprotected]} do not have row-level "
            f"security enabled AND forced. A backfill window that never closed "
            f"looks exactly like this."))

    # A table with RLS on and no policy denies everything, which is correct for
    # `legacy_identity_map` and a silent outage anywhere else.
    policyless = await conn.fetch(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        " WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity "
        "   AND c.relname = ANY($1::text[]) "
        "   AND NOT EXISTS (SELECT 1 FROM pg_policies p "
        "                    WHERE p.schemaname = 'public' AND p.tablename = c.relname)",
        list(PROTECTED_TABLES),
    )
    if policyless:
        report.findings.append(Finding(
            "protected_table_without_policy",
            f"{[r['relname'] for r in policyless]} have RLS enabled and no "
            f"policy, so they return nothing to anybody."))

    return report


async def enforce(conn: Any) -> DatabaseIdentityReport:
    """Inspect, and raise in staging and production if anything blocks.

    Development and test only log. The reason is the same one
    `core/runtime/environment.py` uses: a check that stops local work gets
    switched off, and a check that is switched off protects nothing.
    """
    from core.runtime.environment import get_environment

    report = await inspect(conn)
    env = get_environment().value

    for finding in report.findings:
        logger.warning("database identity finding", extra={
            "code": finding.code, "detail": finding.detail,
            "blocks_readiness": finding.blocks_readiness,
        })

    if not report.ok and env in ("production", "staging"):
        blocking = [f for f in report.findings if f.blocks_readiness]
        raise RuntimeError(
            "refusing to serve with an over-privileged database role in "
            f"{env}: " + "; ".join(f"{f.code}: {f.detail}" for f in blocking)
        )
    return report
