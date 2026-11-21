"""P12-05 — a user's own data, read back through the boundary that scopes it.

The counterpart to `erasure.py`, and deliberately its mirror image: the same
table inventory (`SUBJECT_TABLES`), the same two-pass workspace/tenant split,
the same refusal to report success on a pass that did not happen.

── Three properties, each the point of a different piece ────────────────────

**Scoped by RLS, not by a WHERE clause.** Every read goes through
`tenant_connection` or `workspace_connection` — the same helpers every product
surface uses. The boundary that decides what an export contains is therefore the
same boundary that decides what a page shows, rather than a second definition of
"what you may see" maintained alongside the first. The first copy to drift is
always the one nobody looks at.

**Empty is stated, never implied.** Twenty tables carry a workspace predicate;
an export that bound only the tenant would read zero rows from all of them and
produce a plausible file. So every table in scope appears in the result with its
row count and the binding it was read under, including the zeros. A user cannot
otherwise tell an empty account from a broken export, and neither can an
operator.

**Secret material is excluded by name.** This is the risk the feature
introduces. An export is an authenticated request for a file containing
everything the caller may see, and the caller may see rows in tables that hold
password hashes, TOTP secrets and a wrapped biometric key. A subject is entitled
to know that a credential EXISTS — never to be handed its material, and never to
have it handed to whoever compromises their session. `NEVER_EXPORTED` and
`REDACTED_COLUMNS` are enforced below and asserted in
`tests/tenancy/test_p1205_data_export.py`.

── What this deliberately does not do ───────────────────────────────────────

It does not write an artefact. `erase_object_storage` already reports
`ok=False, "no object-storage backend is configured in this build"` rather than
a green tick for a store that does not exist; inventing one here to fill
`export_requests.artifact_key` would be the same lie in the other direction. The
export is streamed in the response and the request row records why the key is
NULL.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.privacy.erasure import (PERSONAL, SUBJECT_TABLES,
                                  WORKSPACE_OPTIONAL_TABLES, read_own_rows,
                                  tenant_workspaces, workspace_scoped)

logger = logging.getLogger("ira.privacy.export")

#: Tables a subject may never receive the contents of, whatever their scope.
#:
#: Every entry is a table the caller CAN read — that is exactly why the list is
#: needed. RLS grants a user their own credential row; it has no opinion about
#: whether the bytes in it should be written into a file the user downloads,
#: emails to themselves, and stores wherever downloads go.
#:
#: The rule is: a subject is entitled to the FACT that a credential exists, its
#: lifecycle dates, and where it was used from. Never the material. Handing over
#: a password hash converts an account compromise into an offline cracking
#: attack; handing over a wrapped voiceprint DEK or a TOTP seed converts it into
#: a permanent one, because a person cannot rotate their voice.
NEVER_EXPORTED: frozenset[str] = frozenset({
    "user_credentials",       # password hashes — offline attack material
    "totp_secrets",           # second-factor seeds
    "mfa_recovery_codes",     # single-use bypasses for the above
    "voice_profiles",         # biometric template, wrapped DEK, KEK id
    "connector_oauth_states", # in-flight CSRF/PKCE material
})

#: Columns stripped from tables that ARE exported.
#:
#: `connector_accounts` is the case this exists for. A user is entitled to know
#: which third-party accounts they connected and when — that is some of the most
#: useful information in an export — but the row also holds a stored refresh
#: token, which is a live credential at another company. Dropping the table
#: entirely would withhold the useful half to protect the dangerous one.
#: Every name below was checked against the live schema, and that check is a
#: test rather than a habit. The first version of this dict was written from
#: memory — `access_token`, `refresh_token`, `wrapped_dek` — and `connector_accounts`
#: has none of those columns. The redaction named eight things that do not
#: exist and stripped nothing, while reading in review as though it protected
#: the table. `test_a_redacted_column_is_stripped_but_its_table_is_kept`
#: asserts the intersection with `information_schema` is non-empty, so a
#: redaction aimed at a schema that has moved fails instead of quietly lapsing.
REDACTED_COLUMNS: dict[str, frozenset[str]] = {
    "connector_accounts": frozenset({
        "credential_ciphertext", "credential_nonce", "encryption_key_id",
    }),
    "user_sessions": frozenset({"token_hash", "ip_hash"}),
}

#: Ceiling on rows per table in one export.
#:
#: Present so a single request cannot pin a connection reading a million-row
#: table, and TRUTHFUL when it bites: `TableExport.truncated` is set and the
#: caller is told, rather than being handed a silently short file that looks
#: complete. A cap nobody is told about is indistinguishable from data loss.
ROW_CAP = 10_000


@dataclass(frozen=True, slots=True)
class TableExport:
    """One table's contribution, including the empty ones."""

    table: str
    #: `"workspace"` or `"tenant"` — which binding this was read under. Recorded
    #: because reading a workspace-scoped table on a tenant-only connection is
    #: the failure this module is written against, and the count alone
    #: (zero) looks identical whether the binding was right or wrong.
    boundary: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    workspace_id: str | None = None
    truncated: bool = False
    redacted_columns: tuple[str, ...] = ()
    error: str | None = None

    @property
    def count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True, slots=True)
class ExportResult:
    """What an export actually reached, said plainly enough to act on."""

    ok: bool
    scope: str
    generated_at: datetime
    tables: list[TableExport] = field(default_factory=list)
    workspaces_read: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(t.count for t in self.tables)

    def as_dict(self) -> dict[str, Any]:
        """The wire form. Every table appears, including the empty ones."""
        return {
            "ok": self.ok,
            "scope": self.scope,
            "generated_at": self.generated_at.isoformat(),
            "workspaces_read": self.workspaces_read,
            "total_rows": self.total_rows,
            "error": self.error,
            "notes": self.notes,
            "tables": [
                {
                    "table": t.table,
                    "boundary": t.boundary,
                    "workspace_id": str(t.workspace_id) if t.workspace_id else None,
                    "count": t.count,
                    "truncated": t.truncated,
                    "redacted_columns": list(t.redacted_columns),
                    "error": t.error,
                    "rows": t.rows,
                }
                for t in self.tables
            ],
        }


def exportable_tables(scope: str) -> tuple[str, ...]:
    """`SUBJECT_TABLES[scope]` minus the tables holding secret material.

    Derived rather than listed, so a table added to the erasure inventory is
    exported by default and has to be excluded DELIBERATELY. The opposite
    default — an allow-list — fails silently in the direction that matters:
    a new table quietly stops appearing in exports and nobody finds out, because
    an export with a missing section looks exactly like an account with no data
    of that kind.
    """
    return tuple(t for t in SUBJECT_TABLES.get(scope, ())
                 if t not in NEVER_EXPORTED)


async def _read_table(conn, table: str, *, boundary: str,
                      workspace_id=None, principal=None) -> TableExport:
    """One table, through whatever boundary `conn` is already bound to.

    A table listed in `core.privacy.erasure.PERSONAL` is narrowed further,
    to the person's own rows (ADR-0010). Those tables' policies have no user
    clause, so through the boundary alone an export handed over every
    member's rows. A caller that reads one without a principal is refused
    rather than given the whole workspace.
    """
    redact = REDACTED_COLUMNS.get(table, frozenset())
    try:
        # `LIMIT ROW_CAP + 1` so truncation is DETECTED rather than inferred:
        # a query returning exactly the cap is ambiguous between "there were
        # exactly that many" and "there were more".
        if table in PERSONAL:
            if principal is None:
                raise ValueError(f"{table} is exported per person, "
                                 "and no principal was given")
            records = await read_own_rows(conn, table, principal, ROW_CAP + 1)
        else:
            records = await conn.fetch(
                f'SELECT * FROM "{table}" LIMIT {ROW_CAP + 1}')  # noqa: S608
    except Exception as exc:  # noqa: BLE001 — reported, never swallowed
        logger.warning("export: %s failed under %s boundary: %s",
                       table, boundary, type(exc).__name__)
        return TableExport(
            table=table, boundary=boundary, workspace_id=workspace_id,
            error=f"{type(exc).__name__}: {exc}",
        )

    truncated = len(records) > ROW_CAP
    rows = [
        {k: _wire(v) for k, v in dict(r).items() if k not in redact}
        for r in records[:ROW_CAP]
    ]
    return TableExport(
        table=table, boundary=boundary, workspace_id=workspace_id, rows=rows,
        truncated=truncated,
        redacted_columns=tuple(sorted(redact & set(dict(records[0]))) if records
                               else sorted(redact)),
    )


def _wire(value):
    """JSON-representable, without pretending a type it is not."""
    import decimal
    import uuid as _uuid

    if isinstance(value, (_uuid.UUID, decimal.Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        # Never the bytes. A bytea column in an exportable table is a blob whose
        # meaning this module cannot know, and guessing wrong means either
        # corrupting it or emitting key material that `REDACTED_COLUMNS` missed.
        # Its SIZE is the honest, useful answer.
        return {"bytes": len(bytes(value))}
    return value


async def export_subject_data(principal, *, scope: str = "everything") -> ExportResult:
    """Everything in `scope` that `principal` may see, read through RLS.

    Mirrors `erase_postgres`: workspace passes first, then the tenant pass, with
    `WORKSPACE_OPTIONAL_TABLES` deliberately in both — those tables carry rows
    with a workspace and rows without, and each half is only reachable under the
    matching binding.
    """
    from core.identity.db_context import tenant_connection, workspace_connection
    from core.privacy.erasure import _in_workspace

    generated_at = datetime.now(timezone.utc)
    tables = exportable_tables(scope)
    if not tables:
        return ExportResult(
            ok=True, scope=scope, generated_at=generated_at,
            notes=[f"nothing in PostgreSQL is in scope for {scope!r}"],
        )

    scoped = workspace_scoped()
    workspace_tables = [t for t in tables if t in scoped]
    tenant_tables = [t for t in tables if t not in scoped]
    workspace_tables += [t for t in tenant_tables if t in WORKSPACE_OPTIONAL_TABLES]

    results: list[TableExport] = []
    notes: list[str] = []
    workspaces: list = []

    if workspace_tables:
        workspaces = await tenant_workspaces(principal)
        if not workspaces:
            # The same refusal `erase_postgres` makes, for the same reason. "No
            # workspaces" and "we could not enumerate the workspaces" produce an
            # identical empty export, and only one of them is an answer.
            return ExportResult(
                ok=False, scope=scope, generated_at=generated_at,
                error=(
                    "could not enumerate this tenant's workspaces, so the "
                    "workspace-scoped tables were not read. Refusing to hand "
                    "over a partial export as if it were complete."
                ),
            )
        for workspace_id in workspaces:
            bound = _in_workspace(principal, workspace_id)
            async with workspace_connection(bound) as conn:
                for table in workspace_tables:
                    results.append(await _read_table(
                        conn, table, boundary="workspace",
                        workspace_id=workspace_id, principal=principal))

    async with tenant_connection(principal) as conn:
        for table in tenant_tables:
            results.append(await _read_table(conn, table, boundary="tenant",
                                             principal=principal))

    failed = [t for t in results if t.error]
    if failed:
        # Partial success is a failure, exactly as it is for erasure. An export
        # missing a table the user has data in is worse than no export: they
        # will read it as the complete record of what is held about them.
        return ExportResult(
            ok=False, scope=scope, generated_at=generated_at, tables=results,
            workspaces_read=len(workspaces),
            error=(
                f"{len(failed)} table(s) could not be read "
                f"({', '.join(sorted({t.table for t in failed}))}). This export "
                f"is NOT complete."
            ),
        )

    excluded = sorted(set(SUBJECT_TABLES.get(scope, ())) & NEVER_EXPORTED)
    if excluded:
        # Named, not silently dropped. A user is entitled to know that something
        # about them was withheld and why — an export with an unmentioned gap is
        # the same category of untruth as an erasure that overstates its reach.
        notes.append(
            "Excluded, because a copy of the material would be more dangerous "
            "to you than useful: " + ", ".join(excluded) + ". The existence of "
            "these records is reported by /privacy/overview; their contents are "
            "never exported."
        )
    if not any(t.count for t in results):
        notes.append(
            "Every table in scope was read and all were empty. This is a "
            "complete export of an account holding no data — not a failure."
        )

    return ExportResult(
        ok=True, scope=scope, generated_at=generated_at, tables=results,
        workspaces_read=len(workspaces), notes=notes,
    )


async def record_export_request(principal, *, scope: str,
                                result: ExportResult) -> None:
    """Write the row `026` has had a table for since Phase 5 and no writer.

    Non-fatal: an audit outage must not deny somebody their own data. It is
    logged at warning rather than debug, because unlike the biometric audit
    write this one records a request a person made about their own data, and
    losing it silently is how "we have no record of you asking" happens.

    `artifact_key` stays NULL and `note` says why. This build has no
    object-storage backend — `erase_object_storage` reports that honestly rather
    than claiming a store it does not have — so there is no key, and inventing
    one would point at nothing.
    """
    from core.identity.db_context import tenant_connection

    try:
        async with tenant_connection(principal) as conn:
            await conn.execute(
                """INSERT INTO export_requests
                   (tenant_id, user_id, status, completed_at, byte_size, note)
                   VALUES ($1, $2, $3, NOW(), $4, $5)""",
                principal.tenant_id, principal.user_id,
                "completed" if result.ok else "failed",
                None,
                (f"scope={scope}; streamed in the response — this build has no "
                 f"object-storage backend, so there is no artifact_key; "
                 f"tables={len(result.tables)}; rows={result.total_rows}"
                 + (f"; error={result.error}" if result.error else "")),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("export: could not record the request (non-fatal): %s", exc)
