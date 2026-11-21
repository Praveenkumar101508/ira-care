#!/usr/bin/env python3
"""Count the rows that have no workspace, before any policy starts refusing them.

── Why this runs before the policies, not after ─────────────────────────────

`045` added `workspace_id` to the seventeen workspace-scoped tables as a
NULLABLE column and backfilled what it could. Nullable was the only safe choice
at the time: a NOT NULL on a table with existing rows and no writer supplying a
value is a migration that cannot apply.

The consequence is that a row with `workspace_id IS NULL` is currently legal and
currently readable. The moment a policy reads `app.workspace_id`, that stops
being true in a specific and quiet way:

    workspace_id = current_setting('app.workspace_id')::uuid

is NULL for a NULL row — not false, NULL — and a policy predicate that is not
TRUE denies. So every NULL row **disappears from every read**, silently, with no
error anywhere. Not deleted; invisible. The owner sees an empty vault and
nothing in any log says why.

That is why the count comes first, and why "do not begin policy enforcement
while unresolved NULL rows remain" is a rule rather than a preference.

── Three kinds of NULL row, and they need three different answers ───────────

A NULL row is not automatically a problem, and treating them all as one number
hides the fact that they need different work. The tenant's active workspace
count decides which kind it is:

    exactly one   RESOLVABLE   Its home is determined. There is only one place
                               it could belong, so assigning it is arithmetic
                               rather than a judgement.

    more than one AMBIGUOUS    Something has to decide which one, and that
                               decision is about somebody's content. This
                               script refuses to make it: a backfill that
                               guesses is indistinguishable from one that is
                               right, until somebody looks.

    zero          HOMELESS     There is nowhere to put the row at all. Either
                               the tenant was never provisioned — in which case
                               `provision_tenant_substrate` comes first — or
                               the row is residue from a tenant that has been
                               deleted, in which case it is rubbish and should
                               be removed rather than assigned.

The distinction is not cosmetic. "1584 ambiguous rows" reads as 1584 content
decisions somebody has to make; "1584 rows in tenants that have no workspace"
reads as a provisioning gap or a cleanup, which is what it usually is.

── What this script does NOT do ─────────────────────────────────────────────

It writes nothing. It is a `SELECT`-only audit, safe to run against any
database including one carrying real content, and it never prints a row's
contents — only identifiers and counts. Names, message bodies and memory text
are customer data and have no business in an evidence file.

Usage:

    IRA_TEST_ADMIN_DSN=postgresql://... python -m scripts.phase3b.workspace_null_audit
    python -m scripts.phase3b.workspace_null_audit --dsn postgresql://... --markdown
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import Optional

#: The seventeen tables `045` gave a `workspace_id`. Imported rather than
#: retyped, so this cannot audit a stale list.
try:  # pragma: no cover - import shape depends on how the script is invoked
    from core.privacy.erasure import WORKSPACE_SCOPED_TABLES
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from core.privacy.erasure import WORKSPACE_SCOPED_TABLES

TABLES = tuple(sorted(WORKSPACE_SCOPED_TABLES))


@dataclass
class TableTenantRow:
    """One (tenant, table) cell of the audit."""

    tenant_id: str
    table: str
    total_rows: int
    null_rows: int
    #: workspace_id → row count, for the rows that HAVE one. Identifiers only.
    workspace_distribution: dict[str, int] = field(default_factory=dict)
    #: Workspaces this tenant has, in a selectable state.
    tenant_workspace_count: int = 0

    @property
    def resolvable_rows(self) -> int:
        """NULL rows whose tenant has exactly one workspace — home determined."""
        return self.null_rows if self.tenant_workspace_count == 1 else 0

    @property
    def ambiguous_rows(self) -> int:
        """NULL rows whose tenant has several workspaces — a decision is owed."""
        return self.null_rows if self.tenant_workspace_count > 1 else 0

    @property
    def homeless_rows(self) -> int:
        """NULL rows whose tenant has no workspace — provision or remove."""
        return self.null_rows if self.tenant_workspace_count == 0 else 0


@dataclass
class Audit:
    rows: list[TableTenantRow]
    #: Rows with a NULL tenant_id as well — these cannot even be attributed to
    #: an owner, so they are reported separately and loudly.
    orphan_rows: dict[str, int]
    tables_missing_column: list[str]

    @property
    def total_null(self) -> int:
        return sum(r.null_rows for r in self.rows)

    @property
    def total_resolvable(self) -> int:
        return sum(r.resolvable_rows for r in self.rows)

    @property
    def total_ambiguous(self) -> int:
        return sum(r.ambiguous_rows for r in self.rows)

    @property
    def total_homeless(self) -> int:
        return sum(r.homeless_rows for r in self.rows)

    def by_table(self) -> dict[str, dict[str, int]]:
        """The same figures aggregated per table.

        The per-tenant table runs to hundreds of lines on any database with
        history, which is unreadable as evidence. This is the shape a person
        actually reviews; the per-tenant rows stay available in the JSON.
        """
        out: dict[str, dict[str, int]] = {}
        for r in self.rows:
            cell = out.setdefault(
                r.table,
                {"tenants": 0, "total_rows": 0, "null_rows": 0,
                 "resolvable": 0, "ambiguous": 0, "homeless": 0},
            )
            cell["tenants"] += 1
            cell["total_rows"] += r.total_rows
            cell["null_rows"] += r.null_rows
            cell["resolvable"] += r.resolvable_rows
            cell["ambiguous"] += r.ambiguous_rows
            cell["homeless"] += r.homeless_rows
        return out

    @property
    def total_orphan(self) -> int:
        return sum(self.orphan_rows.values())

    @property
    def clear_to_enforce(self) -> bool:
        """True only when nothing is left that a policy would make invisible.

        Deliberately strict: `resolvable` rows count against it too. A row whose
        home is arithmetically determined still has to actually be assigned
        before a policy can read it, and "we know where it goes" is not the same
        statement as "it is there".
        """
        return self.total_null == 0 and self.total_orphan == 0


async def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        await conn.fetchval(
            """
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = $1 AND column_name = $2
            """,
            table,
            column,
        )
    )


async def audit(dsn: str = "", *, conn=None) -> Audit:
    """Read the whole picture in one pass.

    Runs on a connection that can see every tenant — this is an operator audit,
    not a request. It is `SELECT`-only, so the widest possible read here cannot
    turn into the widest possible write.

    `conn` lets a caller supply its own connection instead of a DSN. That exists
    for the tests: since 049 and 050 a NULL workspace can only be created inside
    a transaction that lifts both protections, and a function that opened its own
    connection could not see uncommitted rows. Passing the connection is how the
    audit gets tested against the state it exists to report.
    """
    import asyncpg

    if conn is None and not dsn:
        raise ValueError("audit() needs a dsn or a connection")
    owned = conn is None
    if owned:
        conn = await asyncpg.connect(dsn)
    try:
        rows: list[TableTenantRow] = []
        orphans: dict[str, int] = {}
        missing: list[str] = []

        # Workspaces per tenant, once. `is_default` is not consulted: the
        # question is how many places a row COULD belong to, and a default
        # workspace is still a choice among several when several exist.
        workspace_counts = {
            str(r["tenant_id"]): r["n"]
            for r in await conn.fetch(
                """
                SELECT tenant_id, count(*) AS n
                  FROM workspaces
                 WHERE status = 'active'
                 GROUP BY tenant_id
                """
            )
        }

        for table in TABLES:
            if not await _column_exists(conn, table, "workspace_id"):
                missing.append(table)
                continue

            orphans[table] = await conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE tenant_id IS NULL"  # noqa: S608
            )

            per_tenant = await conn.fetch(
                # Table name is interpolated from TABLES, which comes from a
                # frozenset in the source — not from input. Values stay
                # parameterised.
                f"""
                SELECT tenant_id::text AS tenant_id,
                       count(*) AS total_rows,
                       count(*) FILTER (WHERE workspace_id IS NULL) AS null_rows
                  FROM {table}
                 WHERE tenant_id IS NOT NULL
                 GROUP BY tenant_id
                """  # noqa: S608
            )

            for r in per_tenant:
                tenant = r["tenant_id"]
                distribution = {
                    str(d["workspace_id"]): d["n"]
                    for d in await conn.fetch(
                        f"""
                        SELECT workspace_id, count(*) AS n
                          FROM {table}
                         WHERE tenant_id = $1 AND workspace_id IS NOT NULL
                         GROUP BY workspace_id
                        """,  # noqa: S608
                        r["tenant_id"],
                    )
                }
                rows.append(
                    TableTenantRow(
                        tenant_id=tenant,
                        table=table,
                        total_rows=r["total_rows"],
                        null_rows=r["null_rows"],
                        workspace_distribution=distribution,
                        tenant_workspace_count=workspace_counts.get(tenant, 0),
                    )
                )

        return Audit(rows=rows, orphan_rows=orphans, tables_missing_column=missing)
    finally:
        if owned:
            await conn.close()


def to_markdown(result: Audit) -> str:
    out: list[str] = []
    out.append("# Workspace NULL audit")
    out.append("")
    out.append(
        "Row counts only. No content, no names — a NULL row's *contents* are "
        "customer data and are not what this audit is about."
    )
    out.append("")

    if result.tables_missing_column:
        out.append("## Tables without a `workspace_id` column")
        out.append("")
        out.append(
            "These were expected to carry one. A table listed here has not had "
            "`045` applied, or has been dropped from the schema without being "
            "dropped from `WORKSPACE_SCOPED_TABLES`."
        )
        out.append("")
        for t in result.tables_missing_column:
            out.append(f"* `{t}`")
        out.append("")

    out.append("## Summary")
    out.append("")
    out.append("| Measure | Count | What it needs |")
    out.append("|---|---:|---|")
    out.append(f"| Rows with `workspace_id IS NULL` | {result.total_null} | — |")
    out.append(
        f"| ├─ resolvable (tenant has exactly 1 workspace) | {result.total_resolvable} "
        f"| a backfill; the home is determined |"
    )
    out.append(
        f"| ├─ ambiguous (tenant has several) | {result.total_ambiguous} "
        f"| a decision about somebody's content |"
    )
    out.append(
        f"| └─ homeless (tenant has none) | {result.total_homeless} "
        f"| provision the tenant, or remove the rows |"
    )
    out.append(
        f"| Rows with `tenant_id IS NULL` (orphans) | {result.total_orphan} "
        f"| cannot be attributed to an owner at all |"
    )
    out.append(
        f"| **Clear to enforce workspace policies** | "
        f"**{'yes' if result.clear_to_enforce else 'NO'}** | |"
    )
    out.append("")

    if not result.clear_to_enforce:
        out.append(
            "> Workspace policy enforcement must not begin. A policy reading "
            "`app.workspace_id` evaluates to NULL — not false — for every row "
            "above, and a predicate that is not TRUE denies. Those rows would "
            "vanish from every read with no error in any log."
        )
        out.append("")

    if not result.rows:
        out.append("## By table")
        out.append("")
        out.append("No rows in any workspace-scoped table.")
        return "\n".join(out) + "\n"

    out.append("## By table")
    out.append("")
    out.append("| Table | Tenants | Total rows | NULL | Resolvable | Ambiguous | Homeless |")
    out.append("|---|---:|---:|---:|---:|---:|---:|")
    for table, cell in sorted(result.by_table().items()):
        out.append(
            f"| `{table}` | {cell['tenants']} | {cell['total_rows']} | {cell['null_rows']} "
            f"| {cell['resolvable']} | {cell['ambiguous']} | {cell['homeless']} |"
        )
    out.append("")

    # The per-tenant detail, but only where there is something to act on. On a
    # database with history the full grid runs to hundreds of rows and nobody
    # reads it; the rows that need work are the ones worth printing.
    needs_work = [r for r in result.rows if r.null_rows and r.tenant_workspace_count >= 1]
    out.append("## By tenant — rows that can be placed")
    out.append("")
    out.append(
        "Tenants that HAVE at least one workspace and still hold NULL rows. "
        "Homeless rows are omitted: their tenants have no workspace, so the "
        "next step there is provisioning or cleanup, not a per-row decision."
    )
    out.append("")
    if not needs_work:
        out.append("None.")
        out.append("")
    else:
        out.append("| Tenant | Table | Total | NULL | Workspaces | Distribution |")
        out.append("|---|---|---:|---:|---:|---|")
        for r in sorted(needs_work, key=lambda r: (r.tenant_id, r.table)):
            dist = (
                ", ".join(
                    f"`{w[:8]}`={n}" for w, n in sorted(r.workspace_distribution.items())
                )
                or "—"
            )
            out.append(
                f"| `{r.tenant_id[:8]}` | `{r.table}` | {r.total_rows} | {r.null_rows} "
                f"| {r.tenant_workspace_count} | {dist} |"
            )
        out.append("")

    return "\n".join(out) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.getenv("IRA_TEST_ADMIN_DSN") or os.getenv("DATABASE_URL", ""),
        help="PostgreSQL DSN. Read-only; the script issues no writes.",
    )
    parser.add_argument("--markdown", action="store_true", help="Markdown instead of JSON")
    args = parser.parse_args(argv)

    if not args.dsn:
        print("No DSN. Pass --dsn or set IRA_TEST_ADMIN_DSN.", file=sys.stderr)
        return 2

    result = asyncio.run(audit(args.dsn))

    if args.markdown:
        print(to_markdown(result), end="")
    else:
        print(
            json.dumps(
                {
                    "rows": [asdict(r) for r in result.rows],
                    "orphan_rows": result.orphan_rows,
                    "tables_missing_column": result.tables_missing_column,
                    "by_table": result.by_table(),
                    "total_null": result.total_null,
                    "total_resolvable": result.total_resolvable,
                    "total_ambiguous": result.total_ambiguous,
                    "total_homeless": result.total_homeless,
                    "total_orphan": result.total_orphan,
                    "clear_to_enforce": result.clear_to_enforce,
                },
                indent=2,
                sort_keys=True,
            )
        )

    # Exit code carries the finding, so a CI step can gate on it without
    # parsing output — the same reason the rollback guards raise rather than
    # printing a warning.
    return 0 if result.clear_to_enforce else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
