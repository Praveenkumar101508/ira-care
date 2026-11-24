"""Deleting a user's data across every store that holds it — and saying
truthfully what was reached.

The brief lists the stores: PostgreSQL, pgvector, Redis, object storage, search
indexes, background queues, derived data, and backups according to the retention
schedule. It also says: *"Do not claim data deletion before all relevant stores
are handled"* and *"Do not claim immediate backup deletion unless implemented."*

Both of those are really the same instruction — **report what happened, not what
was intended** — and it drives the whole shape of this module.

── Why every store returns a result instead of raising ─────────────────────

The tempting implementation deletes from each store in turn and lets the first
failure propagate. The user then sees an error, and has no idea whether their
conversations were deleted and their memories were not, or nothing happened at
all. Worse, a `try/except` around the whole thing produces the opposite failure:
a green tick when only the first store was cleared.

So each store reports a :class:`StoreResult`, the orchestrator collects all of
them, and the overall status is `partial` unless every store succeeded. A
partial deletion is a real outcome that the user needs to be told about, not an
edge case to be smoothed over.

── Backups ─────────────────────────────────────────────────────────────────

Nothing here touches backups, and the module says so rather than staying quiet.
`backup_residue_until` records when copies are expected to age out under the
retention schedule. That is a date, not a deletion, and the wording in
`disclosure.deletion_disclosure` reflects that.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Final, Optional, Sequence

logger = logging.getLogger("ira.privacy.erasure")

#: The seventeen tables `045_workspace_columns.sql` gave a `workspace_id`.
#:
#: Erasure has to know which side of the boundary each table sits on, because
#: the two need different connections. Kept here rather than imported from the
#: test that also lists them: this is production behaviour, and a test module
#: is not a dependency production code should have.
#: `tests/tenancy/test_migration_049_workspace_policies.py` and its two
#: neighbours import this set and assert it against the live schema.
WORKSPACE_SCOPED_TABLES = frozenset({
    "agents", "calendar_events", "commitments", "companion_versions",
    "companions", "conversations", "decisions", "edges", "entities", "files",
    "lifeos_events", "memory_embeddings", "messages", "people", "projects",
    "reminders", "tasks",
})

#: Workspace-scoped tables added AFTER `045`, carrying the same strict
#: predicate but not part of the seventeen.
#:
#: Kept separate rather than folded into the set above, because that set means
#: "the tables `045` created" and three migration tests import it to assert
#: `048`'s backfill, `049`'s policies and `050`'s NOT NULL against exactly that
#: era. Widening it would make those tests assert something they were not
#: written to check.
#:
#: `briefings` joined in `081` (P12-03 finding G). What erasure needs is the
#: union — `workspace_scoped()` below — because the DISTINCTION that matters to
#: it is the policy shape, not which migration created the column. A strict
#: predicate deleted on a tenant-only connection matches nothing, which is
#: finding E's failure exactly.
WORKSPACE_SCOPED_SINCE = frozenset({"briefings", "heartbeat_surfaced"})


def workspace_scoped() -> frozenset[str]:
    """Every table whose policy requires `app.workspace_id` to match."""
    return WORKSPACE_SCOPED_TABLES | WORKSPACE_SCOPED_SINCE


#: Tables that carry `workspace_id` but whose policy treats a NULL workspace as
#: tenant-wide, rather than requiring the workspace to match.
#:
#: `051` gave `notifications` that shape so a tenant-wide notification is
#: readable from any of the tenant's workspaces. The consequence for erasure is
#: that neither pass alone is sufficient: a tenant-bound DELETE reaches only the
#: NULL rows, and a workspace-bound DELETE reaches only that workspace's. Both
#: passes run over these, which is complete and needs no special authority.
#:
#: A table belongs here if its policy says `workspace_id IS NULL OR
#: workspace_id = …`. `tests/tenancy/test_p1201_workspace_hazards.py` derives
#: that set from `pg_policies` and asserts it against this one, so a future
#: table with the same shape cannot join the schema and quietly miss a pass.
WORKSPACE_OPTIONAL_TABLES = frozenset({"notifications"})

#: How long backup copies are expected to persist. Configuration for the
#: *statement made to the user*, not something that causes a deletion.
BACKUP_RETENTION_DAYS: Final[int] = 35

#: Every store that can hold customer data. Named here so a store that is
#: forgotten shows up as missing from a result rather than silently not being
#: cleared — `MISSING_STORE` below turns that into a visible failure.
KNOWN_STORES: Final[tuple[str, ...]] = (
    "postgres",        # conversations, messages, memories, people, tasks
    "pgvector",        # embeddings (same table, separate concern)
    "redis",           # sessions, caches, rate limits
    "object_storage",  # files, exports, voice artefacts
    "audit",           # trust_ledger + permission_decisions + action_receipts, by REDACTION
    "keys",            # tenant_data_keys, by DESTRUCTION — see erase_keys
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class StoreResult:
    """What happened in one store."""

    store: str
    ok: bool
    deleted: int = 0
    redacted: int = 0
    #: Records that could not be erased, and why. Non-zero here means the
    #: deletion is genuinely incomplete and must be reported as such.
    unerasable: int = 0
    error: str = ""
    note: str = ""

    def public_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"store": self.store, "ok": self.ok}
        if self.deleted:
            out["deleted"] = self.deleted
        if self.redacted:
            out["redacted"] = self.redacted
        if self.unerasable:
            out["unerasable"] = self.unerasable
        if self.error:
            out["error"] = self.error
        if self.note:
            out["note"] = self.note
        return out


@dataclass(frozen=True, slots=True)
class ErasureOutcome:
    """The whole operation, reported honestly."""

    scope: str
    results: tuple[StoreResult, ...]
    started_at: datetime
    finished_at: datetime
    backup_residue_until: datetime

    @property
    def stores_reached(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def stores_total(self) -> int:
        return len(self.results)

    @property
    def unerasable_records(self) -> int:
        return sum(max(0, r.unerasable) for r in self.results)

    @property
    def status(self) -> str:
        """'completed', 'partial' or 'failed'.

        `completed` requires EVERY store to have succeeded. Anything less is
        `partial` — a word chosen because it is the one a user needs to see.
        """
        if not self.results:
            return "failed"
        if all(r.ok for r in self.results):
            return "completed"
        if any(r.ok for r in self.results):
            return "partial"
        return "failed"

    @property
    def complete(self) -> bool:
        return self.status == "completed"

    def public_dict(self) -> dict[str, Any]:
        from core.privacy.disclosure import deletion_disclosure

        return {
            "scope": self.scope,
            "status": self.status,
            "stores": [r.public_dict() for r in self.results],
            "stores_reached": self.stores_reached,
            "stores_total": self.stores_total,
            "unerasable_records": self.unerasable_records,
            "backup_residue_until": self.backup_residue_until.isoformat(),
            "summary": deletion_disclosure(
                stores_reached=self.stores_reached,
                stores_total=self.stores_total,
                unredactable_records=self.unerasable_records,
            ),
        }


#: A store that was expected but produced no result at all. Represented rather
#: than omitted: an absent store in a report reads as "nothing to do there",
#: which is exactly the wrong inference.
def missing_store(name: str) -> StoreResult:
    return StoreResult(
        store=name,
        ok=False,
        error="no result was produced for this store",
        note=(
            "This store was expected to be cleared and reported nothing. Treat "
            "the deletion as incomplete."
        ),
    )


Eraser = Callable[[], Awaitable[StoreResult]]


async def run_erasure(
    *,
    scope: str,
    erasers: dict[str, Eraser],
    expected_stores: Sequence[str] = KNOWN_STORES,
    now: Optional[datetime] = None,
) -> ErasureOutcome:
    """Run every eraser, collect every result, and never stop at the first fault.

    An eraser that raises becomes a failed :class:`StoreResult` rather than
    aborting the run — the user's other stores should still be cleared, and the
    failure is reported alongside rather than instead of the successes.
    """
    started = now or _now()
    results: list[StoreResult] = []

    for name in expected_stores:
        eraser = erasers.get(name)
        if eraser is None:
            results.append(missing_store(name))
            continue
        try:
            result = await eraser()
            if not isinstance(result, StoreResult):  # pragma: no cover - defensive
                result = StoreResult(
                    store=name, ok=False,
                    error=f"eraser returned {type(result).__name__}, not StoreResult",
                )
            results.append(result)
        except Exception as exc:  # noqa: BLE001 - collected, not swallowed
            logger.exception("erasure failed for store %s", name)
            results.append(
                StoreResult(store=name, ok=False, error=f"{type(exc).__name__}: {exc}")
            )

    finished = _now()
    return ErasureOutcome(
        scope=scope,
        results=tuple(results),
        started_at=started,
        finished_at=finished,
        # A date, not a deletion. See the module docstring.
        backup_residue_until=finished + timedelta(days=BACKUP_RETENTION_DAYS),
    )


# ── The individual erasers ───────────────────────────────────────────────────
#
# Each is a small function returning a StoreResult. They are separate so a
# deployment without (say) object storage can supply a result saying so rather
# than the orchestrator guessing.


#: The tables that hold a subject's data, by erasure scope.
#:
#: Module level since P12-05, and that move is the point rather than tidiness.
#: This lived inside `erase_postgres`, so the only way to ask "which tables
#: belong to a person" was to read a function that deletes them —
#: `tests/readiness/test_erasure_coverage.py` had to AST-parse it out of the
#: source of a deletion routine. `core/privacy/export.py` needs the same answer
#: to a different question, and two copies would drift in the direction nobody
#: notices: a table added to erasure and forgotten by export is a table the
#: product destroys on request and cannot show you.
#:
#: One definition, and these readers. Recorded because moving it broke three
#: suites that were each hand-rolling their own AST walk over
#: `erase_postgres`'s body — the only way to reach the data while it was a
#: local, and three copies of a parser nobody knew existed:
#:
#:     core/privacy/export.py                      imports it
#:     tests/readiness/test_erasure_coverage.py    PARSES the source, on purpose
#:     tests/tenancy/test_p205_totp_erasure.py     imports it
#:     tests/tenancy/test_p2_mfa_recovery.py       imports it
#:     tests/tenancy/test_realtime_isolation_rls.py  imports it
#:
#: The coverage sweep still parses rather than imports, and that is deliberate:
#: it compares this scope against the LIVE SCHEMA, so a fixture that mutated the
#: dict would make it agree with itself and with nothing else. The three
#: membership assertions have no such failure mode and simply import.
SUBJECT_TABLES: dict[str, tuple[str, ...]] = {
    "memories": ("memory_embeddings",),
    "conversations": ("messages", "conversations"),
    "voice": ("realtime_events", "realtime_sessions"),
    "everything": (
        "memory_embeddings", "messages", "conversations", "tasks", "people",
        "realtime_events", "realtime_sessions",
        # Phase 7. In-flight OAuth states are deletable; connector_accounts
        # is NOT deleted here — see the note in `account` below.
        "connector_oauth_states",
        # Blocker B1. These ten became tenant-scoped in
        # 031_pretenancy_isolation.sql, and a tenant-scoped table holding
        # the user's own content belongs in "delete everything about me".
        # All ten are content, not accountability: documents the user
        # uploaded, commitments they made, events about their life, the
        # envelopes they created, and the cognitive loop's record of their
        # own activity. None of them is a record protecting a third party,
        # which is the only category ADR-0004 carved out for redaction.
        #
        # ORDER IS LOAD-BEARING. `commitments`, `lifeos_events` and
        # `envelope_members` all reference `privacy_envelopes(id)`, and
        # `cognitive_prediction_outcomes` references
        # `cognitive_predictions`. Children first, or the DELETE fails on a
        # foreign key halfway through an erasure.
        "cognitive_prediction_outcomes", "cognitive_predictions",
        "cognitive_cycles", "cognitive_events", "cognitive_goals",
        "files",
        "envelope_members", "commitments", "lifeos_events",
        "privacy_envelopes",
        # Batch 4 — see the note in `account`.
        "business_events", "model_performance",
        "strategy_predictions", "strategy_calibration",
        # Batch 5B — see the note in `account`.
        "edges", "entities", "reminders", "decisions",
        "calendar_events", "agents", "projects",
        # P12-03 finding G. `briefings` became workspace-scoped in `081`.
        # It was previously unreachable by erasure ENTIRELY — no tenant
        # column meant `_delete_scoped`'s `WHERE tenant_id = $1` could not
        # be written, so a subject access deletion left every briefing
        # behind and reported success. A briefing is the densest personal
        # summary IRA holds: that user's revenue, security posture, tasks
        # and calendar in one paragraph.
        "briefings",
        # P12-03b finding H. `082` scoped the heartbeat de-duplication
        # ledger. Like `briefings` it was previously unreachable by erasure
        # entirely — no tenant column meant `_delete_scoped` could not be
        # written for it — so a subject access deletion left every dedupe
        # key behind. The keys embed `decisions` and `memory_embeddings`
        # identifiers belonging to the person asking to be forgotten.
        "heartbeat_surfaced",
        # 3C-10. `notifications` became tenant-scoped in `051`. Its `body`
        # is the notification content itself — a briefing is the user's own
        # summary, a security alert is their security events — so it is
        # content, not accountability, and belongs in both scopes. It has
        # no children, so its position in the ordering is free.
        "notifications",
    ),
    "account": (
        "memory_embeddings", "messages", "conversations", "tasks", "people",
        "realtime_events", "realtime_sessions",
        "companion_versions", "companion_preferences", "companions",
        "constitution_rules", "permission_grants",
        # Blocker B1 — see the note in `everything`. Same tables, same
        # child-before-parent ordering.
        "cognitive_prediction_outcomes", "cognitive_predictions",
        "cognitive_cycles", "cognitive_events", "cognitive_goals",
        "files",
        "envelope_members", "commitments", "lifeos_events",
        "privacy_envelopes",
        # Blocker B2, ADR-0008. Four of the six tables Phase 9 found
        # unreached are the user's OWN records with no accountability value
        # to anybody, so they are deleted rather than redacted:
        #
        #   user_sessions        token hash, IP hash, user agent
        #   user_devices         "Praveen's iPhone" — a name and a possession
        #   retention_settings   the user's own privacy preferences
        #   tenant_memberships   the account relationship itself
        #
        # `user_sessions` before `user_devices`: sessions reference devices.
        # `tenant_memberships` last of the four — nothing references it, and
        # removing the membership is the step that makes the account gone.
        #
        # The record that a deletion HAPPENED survives in `privacy_events`
        # and `deletion_requests`, both exempt from erasure precisely so it
        # does. The other two of the six — `agent_runs` and `tool_calls` —
        # are REDACTED by `erase_audit` rather than deleted. ADR-0008 has
        # the reasoning table for all six.
        # Phase 3B Batch 0/1 added two more tables of the same kind, and
        # they belong here for the reason ADR-0008 already gave:
        #
        #   workspace_memberships  the account relationship again, one level
        #                          down. Leaving it would keep the person
        #                          listed as a member of a workspace after
        #                          their membership of the tenant is gone.
        #   delegated_users        username, password_hash, role and
        #                          `voice_enrolled` for a delegated account
        #                          — credentials, not audit.
        #
        # `workspace_memberships` before `tenant_memberships`: the workspace
        # relationship is the narrower one, and removing the tenant
        # membership is still the step that makes the account gone.
        # Phase 3B Batch 4. Four tables gained a tenant, and all four hold
        # the tenant's own content rather than anybody's accountability
        # record, so they are deleted rather than redacted:
        #
        #   business_events       webhook and monitoring events about the
        #                         customer's business
        #   model_performance     latency and token counts — operational
        #                         telemetry, NOT the billing record. That is
        #                         `model_usage`, which stays exempt because
        #                         it is retained for accounting.
        #   strategy_predictions  the prediction log; `question` is free text
        #                         the customer wrote
        #   strategy_calibration  derived from those predictions, so keeping
        #                         it after deleting them would leave an
        #                         aggregate of erased content behind
        #
        # `strategy_calibration` last: it is derived, and deleting a
        # derivation before its source is the ordering that leaves a
        # dangling aggregate if the run is interrupted.
        "business_events", "model_performance",
        "strategy_predictions", "strategy_calibration",
        # Batch 5B — the seven tables that had no tenant boundary at all
        # until `045` (R-64). They hold customer content and are now
        # tenant-scoped, so erasure has to reach them or a deletion request
        # leaves the customer's agents, calendar, decisions and knowledge
        # graph behind.
        #
        # Order matters and is not alphabetical. `edges` references
        # `entities` and `reminders` references `tasks`, both ON DELETE
        # CASCADE — but relying on a cascade to erase means the erasure
        # report cannot say how many rows it removed. Children first, so
        # each DELETE is counted.
        "edges", "entities", "reminders", "decisions",
        "calendar_events", "agents", "projects",
        # P12-03 finding G. `briefings` became workspace-scoped in `081`.
        # It was previously unreachable by erasure ENTIRELY — no tenant
        # column meant `_delete_scoped`'s `WHERE tenant_id = $1` could not
        # be written, so a subject access deletion left every briefing
        # behind and reported success. A briefing is the densest personal
        # summary IRA holds: that user's revenue, security posture, tasks
        # and calendar in one paragraph.
        "briefings",
        # P12-03b finding H. `082` scoped the heartbeat de-duplication
        # ledger. Like `briefings` it was previously unreachable by erasure
        # entirely — no tenant column meant `_delete_scoped` could not be
        # written for it — so a subject access deletion left every dedupe
        # key behind. The keys embed `decisions` and `memory_embeddings`
        # identifiers belonging to the person asking to be forgotten.
        "heartbeat_surfaced",
        # 3C-10. See the note in `everything`. Ordered before
        # `workspace_memberships` because `notifications.workspace_id` has
        # an ON DELETE RESTRICT foreign key into `workspaces` — leaving a
        # notification behind would block the workspace teardown that
        # follows account erasure.
        "notifications",
        # P2-05. `totp_secrets` gained a tenant in `052`, which made it
        # tenant-scoped and therefore something a deletion request has to
        # reach. It is deleted rather than redacted: a second-factor seed
        # belonging to an erased account has no accountability value to
        # anybody and no legitimate retention purpose — it is a live
        # credential for a person who has asked to stop existing here.
        #
        # It works under the erasure connection without special handling
        # because `erase_postgres` runs inside `tenant_connection`, which
        # binds `app.user_id` as well as `app.tenant_id` — and 052's
        # policies require both. A tenant-only connection would match zero
        # rows here and report a successful erasure that deleted nothing,
        # which is the failure mode this table specialises in.
        #
        # Ordered before `tenant_memberships` for the same reason
        # `workspace_memberships` is: remove the credential while the
        # membership that scopes it still exists.
        "totp_secrets",
        # Batch 7. A recovery code IS a second factor, so it is erased with
        # one — and BEFORE `tenant_memberships`, for the same reason
        # `totp_secrets` is: the row is scoped by the membership, so
        # removing the membership first would leave it unreachable rather
        # than deleted. That is the failure mode where erasure reports
        # success having deleted nothing.
        "mfa_recovery_codes",
        "workspace_memberships",
        "user_sessions", "user_devices", "retention_settings",
        "delegated_users", "tenant_memberships",
        # Phase 7. `connector_accounts` rows go LAST and only under
        # `account`, because deleting one destroys the stored refresh token
        # WITHOUT revoking it at the provider — leaving a live grant the
        # user believes is gone. `core/connectors/store.disconnect` is the
        # path that revokes first; `erase_connectors` below reports whether
        # that happened rather than assuming it.
        "connector_oauth_states", "connector_accounts",
    ),
}


async def erase_postgres(principal, *, scope: str) -> StoreResult:
    """Delete the user's rows from the relational stores.

    Runs inside `tenant_connection`, so Row-Level Security scopes every
    statement — a deletion cannot reach another tenant's rows even if a WHERE
    clause were wrong.
    """
    from core.identity.db_context import tenant_connection, workspace_connection

    # Phase 6 added `realtime_events` and `realtime_sessions`. They hold no
    # conversation content — the schema is structurally incapable of it — but
    # they ARE linked to a user and they record which external provider carried
    # that user's live microphone, and when. That is personal data, so erasure
    # has to reach it or the deletion promise is false for anyone who used a
    # live session. Events come first: they reference the session.
    #
    # This is also why `realtime_events` refuses UPDATE but permits DELETE.
    # An append-only trigger there would have put these rows out of reach and
    # made the omission invisible.
    tables = SUBJECT_TABLES.get(scope, ())
    if not tables:
        return StoreResult(
            store="postgres", ok=True, deleted=0,
            note=f"nothing in PostgreSQL is in scope for {scope!r}",
        )

    # ── Two passes, because the tables live behind two different boundaries ──
    #
    # Batch 5B gave seventeen tables a `workspace_id`, and Batch 5C's policy
    # stage will make every statement against them require a bound workspace.
    # A tenant-only connection would then match ZERO rows in those tables — an
    # erasure that reports success and deletes nothing, which is the worst
    # possible outcome for this particular function.
    #
    # The fix is NOT a policy exemption for "tenant-scoped operations": that
    # would be a hole any caller could reach. It is N scoped passes, one per
    # workspace in the tenant, the same shape the retention purge uses. The
    # remaining tables are tenant-scoped and only tenant-scoped, so they keep
    # the single tenant-bound pass.
    strict = workspace_scoped()
    workspace_tables = [t for t in tables if t in strict]
    tenant_tables = [t for t in tables if t not in strict]
    # ── The third category, found by P12-01 EXECUTION ────────────────────────
    #
    # `notifications` is in neither group cleanly, and putting it in the tenant
    # group left rows behind. Its policy is not the strict predicate the
    # seventeen carry; `051` gave it
    #
    #     tenant_id = app.tenant_id
    #     AND (workspace_id IS NULL OR workspace_id = app.workspace_id)
    #
    # so a tenant-bound DELETE with no workspace matches only the rows whose
    # `workspace_id` IS NULL. Any workspace-scoped notification survives the
    # erasure, `_delete_scoped` counts the rows it did remove, and the caller
    # records a successful deletion — the exact failure this function's own
    # comments are written against, arrived at from a direction nobody had
    # looked: a policy shape that is neither tenant-only nor workspace-only.
    #
    # Reached today? No. `worker/notifier.py` is the only production writer and
    # it never sets `workspace_id`, so every live row is NULL-workspace and is
    # erased correctly. This is a latent gap, and it is being closed while it
    # is still latent rather than after the first caller writes one — at which
    # point the leftover row also blocks workspace teardown, because
    # `notifications_workspace_id_fkey` is ON DELETE RESTRICT.
    #
    # The fix is the shape already established above: run it in BOTH passes.
    # Each workspace pass removes that workspace's rows through the same
    # policy every other caller uses, and the tenant pass removes the
    # NULL-workspace ones. No bypass, no exemption, nothing an ordinary caller
    # could reach — just both halves of a two-part predicate, each addressed
    # where it matches.
    workspace_tables += [t for t in tenant_tables if t in WORKSPACE_OPTIONAL_TABLES]

    deleted = 0
    failures: list[str] = []
    # Group B and C rows with no author or owner, kept by decision (ADR-0010 Q2,
    # Q4) and reported.
    kept: dict[str, int] = {}
    # This person's rows kept for other people's links (ADR-0011).
    released: dict[str, int] = {}

    # WORKSPACE PASSES FIRST. The ordering in `tables_by_scope` is child-first
    # and load-bearing, and splitting it across two connections broke that:
    # `commitments` and `lifeos_events` are workspace-scoped and reference
    # `privacy_envelopes`, which is tenant-scoped. Deleting the tenant tables
    # first therefore failed on
    # `commitments_envelope_id_fkey` — partway through an erasure, which is
    # exactly the failure the original ordering comment warns about.
    #
    # Every workspace-scoped table is a child of, or unrelated to, the
    # tenant-scoped ones, so doing all the workspace passes before the tenant
    # pass preserves child-before-parent across the split.
    if workspace_tables:
        workspaces = await tenant_workspaces(principal)
        if not workspaces:
            # No workspaces means nothing to erase in them — but it is only
            # safe to say that if we could actually ENUMERATE them. A lookup
            # that failed is reported as a failure, never as "nothing found".
            return StoreResult(
                store="postgres", ok=False, deleted=deleted,
                error=(
                    "could not enumerate this tenant's workspaces, so the "
                    "workspace-scoped tables were not reached. Refusing to "
                    "report a partial erasure as complete."
                ),
            )
        for workspace_id in workspaces:
            bound = _in_workspace(principal, workspace_id)
            try:
                async with workspace_connection(bound) as conn:
                    for table in workspace_tables:
                        if n := await release_shared(conn, table, principal):
                            released[table] = released.get(table, 0) + n
                        deleted += await _delete_table(conn, table, principal)
                        if n := await count_unattributed(conn, table):
                            kept[table] = kept.get(table, 0) + n
                    if scope == "conversations":
                        deleted += await _delete_own_transcript_memories(conn, principal)
                    if scope == "account":
                        # IRA CARE (107): the account's push tokens in this workspace. Keyed
                        # on user_id, so only this person's phones go; the circle member
                        # row (the person cared for's record) stays.
                        deleted += await _delete_own_care_push_tokens(conn, principal)
                        deleted += await _delete_own_care_user_preferences(conn, principal)
            except Exception as exc:  # noqa: BLE001 — recorded, never swallowed
                logger.error(
                    "erasure: workspace %s pass failed: %s", workspace_id, exc
                )
                failures.append(str(workspace_id))

    if tenant_tables and not failures:
        async with tenant_connection(principal) as conn:
            for table in tenant_tables:
                if n := await release_shared(conn, table, principal):
                    released[table] = released.get(table, 0) + n
                deleted += await _delete_table(conn, table, principal)
                if n := await count_unattributed(conn, table):
                    kept[table] = kept.get(table, 0) + n

    # ── LangGraph checkpoints ────────────────────────────────────────────────
    #
    # Everything above lives in tables the migration chain created, so it has a
    # tenant column and RLS. The checkpoint tables do not: their schema is
    # LangGraph's own (migration 097 creates it as LangGraph defines it), and they hold
    # `IRAState` — the message history, the user query, the response, and
    # `memory_context`, which is vault memories injected into the prompt.
    #
    # So an erasure that stopped at the line above reported ok=True while
    # leaving behind the conversation it was asked to remove. That is the worst
    # shape a privacy failure can take: the promise was made and the log says it
    # was kept.
    #
    # Only reachable because `agents/graph.checkpoint_thread_id` puts the tenant
    # and the workspace in front of the client's session id. Without that prefix
    # these rows have nothing to attribute them to and no way to find them.
    if scope in ("conversations", "everything", "account") and not failures:
        try:
            deleted += await _erase_checkpoints(principal)
        except Exception as exc:  # noqa: BLE001 — recorded, never swallowed
            logger.error("erasure: checkpoint pass failed: %s", exc)
            return StoreResult(
                store="postgres", ok=False, deleted=deleted,
                error=(
                    f"the LangGraph checkpoint pass failed ({exc}). Conversation "
                    f"state may remain; this erasure is NOT complete."
                ),
            )

    if failures:
        # Partial success is a failure. A caller told "erased" while rows
        # survive in one workspace has been told something untrue, and the
        # user has been told their data is gone.
        return StoreResult(
            store="postgres", ok=False, deleted=deleted,
            error=(
                f"{len(failures)} of {len(workspaces)} workspace pass(es) failed "
                f"({', '.join(failures)}). Rows may remain in those workspaces; "
                f"this erasure is NOT complete."
            ),
        )

    # ── The account's token version (migration 096) ──────────────────────────
    #
    # Erasing an account signs it out everywhere: the counter is raised, so no
    # token issued before the erasure is accepted after it. The counter's row
    # is KEPT, and `users.deleted_at` is set so that a scheduled purge deletes
    # it seven days from now (the longest life of a credential IRA signs).
    # Deleting it here instead would reset the version to 0 while a seven-day
    # refresh token could still be alive, which is the defect 096 fixed. The
    # row holds a UUID and an integer: no name, no address.
    #
    # An account with no `users` row has no counter to raise, and needs none:
    # `token_versions.current` refuses every token naming such an account
    # (UnknownAccount, 401), so none of its tokens is accepted now.
    if scope == "account":
        try:
            from core.identity import token_versions
            from utils.db import acquire

            try:
                await token_versions.bump(principal.user_id)
            except token_versions.UnknownAccount:
                pass
            async with acquire() as conn:   # `users` carries no RLS
                await conn.execute(
                    "UPDATE users SET deleted_at = COALESCE(deleted_at, NOW()), "
                    "updated_at = NOW() WHERE id = $1", principal.user_id)
        except Exception as exc:  # noqa: BLE001 — recorded, never swallowed
            # Worded so the log redaction filter keeps the placeholder: it
            # reads "tokens: <anything>" as a secret and replaces it.
            logger.error("erasure: the account's sign-out step failed (%s)", exc)
            return StoreResult(
                store="postgres", ok=False, deleted=deleted,
                error=(
                    f"the account's rows were deleted, but its tokens could not "
                    f"be invalidated ({type(exc).__name__}). Tokens issued before "
                    f"this erasure may still work; this erasure is NOT complete."
                ),
            )
    return StoreResult(store="postgres", ok=True, deleted=deleted,
                       note=kept_note(kept, released))



async def _erase_checkpoints(principal) -> int:
    """Delete this person's LangGraph checkpoint rows, in every workspace of
    their tenant.

    ── Why this is a prefix match rather than a join ────────────────────────

    The checkpoint tables have no `tenant_id` to join on. What they have is a
    `thread_id`, and since `agents/graph.checkpoint_thread_id` that value starts
    with the tenant, the workspace and (ADR-0009) the user. The prefixes come
    from `checkpoint_thread_prefix` in the same module, so the builder and the
    matcher cannot drift — a prefix that stopped matching what the builder
    produces would delete nothing and report success, which is precisely the
    failure being fixed here.

    ── Whose threads (ADR-0009) ─────────────────────────────────────────────

    This used to match the whole TENANT prefix, so one person's erasure took
    every colleague's agent state with it. It now matches, per workspace, the
    exact prefix `…:w:{workspace}:u:{user}:s:` — built from the enumerated
    workspaces rather than a `%` in the middle, because a `%` there would also
    match a thread whose client-chosen session id happened to contain this
    user's segment.

    Threads written before ADR-0009 (`…:w:{workspace}:s:`, no user) are removed
    too, as they always were: nothing builds their ids any more, so their state
    is unreachable, and leaving it would keep content nobody can attribute or
    erase.

    ── Why an owner-level connection, and why that is not a hole ────────────

    These tables have no RLS to bind, so a workspace-scoped connection would buy
    nothing and a tenant-scoped one is not available for them either. The
    statement is constrained by the prefixes instead, built from the verified
    principal's tenant and user and from the tenant's own workspaces — values
    the caller cannot supply. The `LIKE` patterns are parameterised and escaped.

    Absent tables are not an error. A database from before migration 097, where
    the checkpointer never ran, has none, and erasure must not fail because there
    was nothing to erase.
    """
    from agents.graph import checkpoint_thread_prefix, legacy_checkpoint_thread_prefix
    from utils.db import acquire

    def _like(prefix: str) -> str:
        # LIKE metacharacters cannot appear in a UUID, but the escape is
        # explicit rather than assumed — the day this takes a slug instead of a
        # uuid is the day the assumption stops holding.
        return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    patterns: list[str] = []
    for workspace_id in await tenant_workspaces(principal):
        patterns.append(_like(checkpoint_thread_prefix(
            principal.tenant_id, workspace_id, principal.user_id)))
        patterns.append(_like(legacy_checkpoint_thread_prefix(
            principal.tenant_id, workspace_id)))
    if not patterns:
        raise RuntimeError(
            "could not enumerate this tenant's workspaces, so no checkpoint "
            "prefix could be built; refusing to report the pass as done")

    removed = 0
    async with acquire() as conn:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            exists = await conn.fetchval("SELECT to_regclass($1)", f"public.{table}")
            if exists is None:
                continue
            result = await conn.execute(
                # `LIKE ANY (…) ESCAPE` is not valid SQL; one LIKE per pattern is.
                f"DELETE FROM {table} WHERE EXISTS ("  # noqa: S608
                f" SELECT 1 FROM unnest($1::text[]) AS p WHERE thread_id LIKE p ESCAPE '\\')",
                patterns,
            )
            try:
                removed += int(str(result).rsplit(" ", 1)[-1])
            except ValueError:  # pragma: no cover — driver-dependent
                pass
    logger.info(
        "erasure: removed %d checkpoint row(s) for one user in tenant %s",
        removed, principal.tenant_id)
    return removed


async def _delete_table(conn, table: str, principal) -> int:
    """Delete this person's rows from `table` (ADR-0010).

    A table listed in `PERSONAL` (at the end of this module) is narrowed to
    the person: the workspace and tenant policies have no user clause, so a
    DELETE by tenant alone removed every member's rows and reported success.
    A table not listed there is still deleted by tenant (`_delete_scoped`)
    until ADR-0010's later changes reach it.
    """
    if table in PERSONAL:
        return await delete_own_rows(conn, table, principal)
    return await _delete_scoped(conn, table, principal.tenant_id)


async def _delete_scoped(conn, table: str, tenant_id) -> int:
    """One DELETE, returning its row count.

    The `WHERE tenant_id = $1` is belt to RLS's braces: the policy already
    constrains the statement, and a wrong predicate here still cannot reach
    another tenant. Keeping both means neither is the single point of failure.
    """
    result = await conn.execute(
        f"DELETE FROM {table} WHERE tenant_id = $1", tenant_id
    )
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except ValueError:  # pragma: no cover — driver-dependent
        return 0


def _in_workspace(principal, workspace_id):
    """`principal`, bound to one of ITS OWN tenant's workspaces.

    The workspace ids come from `tenant_workspaces`, which reads them on a
    connection already bound to this tenant — so a workspace from another
    tenant cannot appear here, and `workspace_connection` re-checks the pair
    inside its own transaction regardless.
    """
    from dataclasses import replace

    return replace(principal, workspace_id=workspace_id)


async def _delete_own_care_user_preferences(conn, principal) -> int:
    """`care_user_preferences` rows belong to an ACCOUNT (how the app looks for it);
    they go with the account, inside each workspace pass, like the push tokens."""
    result = await conn.execute("DELETE FROM care_user_preferences WHERE user_id = $1", principal.user_id)
    return int(result.split()[-1]) if result else 0


async def _delete_own_care_push_tokens(conn, principal) -> int:
    """`care_push_tokens` rows belong to an ACCOUNT (the phone of a family member or
    carer), not to the person cared for, so account erasure deletes them by user_id
    inside the workspace pass. Nothing else in the care tables is the account's."""
    result = await conn.execute(
        "DELETE FROM care_push_tokens WHERE user_id = $1", principal.user_id)
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except ValueError:  # pragma: no cover
        return 0


async def _delete_own_transcript_memories(conn, principal) -> int:
    """Delete what chat remembered from this person's conversations.

    `memory.store.save_message` embeds every chat message, the person's and
    IRA's, into `memory_embeddings` with `source_type = 'message'`, and chat
    recall searches those rows. Deleting the conversations without them left
    IRA able to recall what was said in conversations the person had deleted
    (`docs/security/FORGET_LEAVES_TRANSCRIPT_COPIES_2026-10-19.md`).

    Only this account's own rows go. The workspace policy on
    `memory_embeddings` has no user clause, so the `user_id` test here is what
    keeps one person's erasure off a colleague's rows. The names come from
    `retention._own_memory_namespaces`, which never claims a reserved
    namespace, so the Memory Vault (`owner`) is untouched: forgetting a saved
    memory is the vault's own action.
    """
    from core.privacy.retention import _own_memory_namespaces

    own = await _own_memory_namespaces(conn, principal)
    if not own:
        return 0
    result = await conn.execute(
        "DELETE FROM memory_embeddings "
        " WHERE tenant_id = $1 AND source_type = 'message' "
        "   AND user_id = ANY($2::text[])",
        principal.tenant_id, own,
    )
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except ValueError:  # pragma: no cover — driver-dependent
        return 0


async def _count_own_transcript_memories(conn, principal) -> int:
    """How many of this person's chat memories are left (`erase_vectors`)."""
    from core.privacy.retention import _own_memory_namespaces

    own = await _own_memory_namespaces(conn, principal)
    left = await conn.fetchval(
        "SELECT COUNT(*) FROM memory_embeddings "
        " WHERE tenant_id = $1 AND source_type = 'message' "
        "   AND user_id = ANY($2::text[])",
        principal.tenant_id, own,
    )
    return int(left or 0)


async def _count_reserved_name_transcript_memories(conn, principal) -> int:
    """Chat memories filed under one of this account's names that is reserved.

    Only an install whose owner login was already a reserved name, such as
    `owner`, has any: chat filed their messages under that login, and deletion
    never touches a reserved namespace (`retention._reserved_account_names`).
    They are counted so the erasure result says they were kept instead of
    reporting success.
    """
    from core.privacy.retention import _reserved_account_names

    reserved = await _reserved_account_names(conn, principal)
    if not reserved:
        return 0
    kept = await conn.fetchval(
        "SELECT COUNT(*) FROM memory_embeddings "
        " WHERE tenant_id = $1 AND source_type = 'message' "
        "   AND user_id = ANY($2::text[])",
        principal.tenant_id, reserved,
    )
    return int(kept or 0)


async def tenant_workspaces(principal) -> list:
    """Every workspace in the principal's tenant, read through the tenant boundary.

    Not `SELECT … FROM workspaces` on an unbound connection: `workspaces` is
    tenant-scoped with forced RLS, so this list is exactly the tenant's own by
    construction rather than by a WHERE clause somebody has to get right.

    Archived and provisioning workspaces are INCLUDED. `select_workspace`
    refuses them for ordinary work, and rightly — but an erasure that skipped
    an archived workspace would leave the user's data in the one place they are
    least likely to look for it.
    """
    from core.identity.db_context import tenant_connection

    async with tenant_connection(principal) as conn:
        rows = await conn.fetch(
            "SELECT id FROM workspaces WHERE tenant_id = $1 ORDER BY created_at",
            principal.tenant_id,
        )
    return [r["id"] for r in rows]


async def _count_personal_memories(conn, principal) -> tuple[int, int]:
    """This person's memory embeddings still present, and the rows kept under one
    of the account's reserved names (`erase_vectors`)."""
    from core.privacy.retention import (
        _reserved_account_names,
        personal_memory_namespaces,
    )

    personal = await personal_memory_namespaces(conn, principal)
    left = int(await conn.fetchval(
        "SELECT COUNT(*) FROM memory_embeddings "
        " WHERE tenant_id = $1 AND user_id = ANY($2::text[]) "
        "   AND embedding IS NOT NULL",
        principal.tenant_id, personal) or 0)
    reserved = [n for n in await _reserved_account_names(conn, principal)
                if n not in personal]
    kept = 0
    if reserved:
        kept = int(await conn.fetchval(
            "SELECT COUNT(*) FROM memory_embeddings "
            " WHERE tenant_id = $1 AND user_id = ANY($2::text[])",
            principal.tenant_id, reserved) or 0)
    return left, kept


async def erase_vectors(principal, *, scope: str) -> StoreResult:
    """Embeddings.

    Reported as its own store even though the rows live in
    `memory_embeddings` alongside the text. They are a distinct artefact — an
    embedding is a lossy but real representation of the content, and a user
    asking "is my data gone" is asking about both. Folding it into the postgres
    line would let a future split of the two go unnoticed.
    """
    from core.identity.db_context import tenant_connection, workspace_connection

    if scope == "conversations":
        # The conversations' own embeddings: what chat remembered from them.
        # Counted per workspace, because `memory_embeddings` carries the strict
        # workspace predicate and a tenant-only connection would count nothing.
        #
        # Rows under one of the account's reserved names (an owner whose login
        # is `owner`) are never deleted, so they are counted as kept rather
        # than left out of the count. Leaving them out would report success.
        left = kept = 0
        for workspace_id in await tenant_workspaces(principal):
            async with workspace_connection(_in_workspace(principal, workspace_id)) as conn:
                left += await _count_own_transcript_memories(conn, principal)
                kept += await _count_reserved_name_transcript_memories(conn, principal)
        if left or kept:
            problems = []
            if left:
                problems.append(
                    f"{left} chat memory embedding(s) still present after the deletion")
            if kept:
                problems.append(
                    f"{kept} chat memory embedding(s) not deleted: they are filed under "
                    f"this account's login, which is a name IRA reserves for its own "
                    f"memory, and deletion never touches a reserved name "
                    f"(docs/security/RESERVED_OWNER_LOGIN_2026-10-19.md)")
            return StoreResult(
                store="pgvector", ok=False, unerasable=left + kept,
                error="; ".join(problems),
            )
        return StoreResult(store="pgvector", ok=True,
                           note="what chat remembered from these conversations was removed with them")

    if scope not in ("memories", "everything", "account"):
        return StoreResult(
            store="pgvector", ok=True,
            note=f"no embeddings are in scope for {scope!r}",
        )

    # Counted per workspace, under the same rule the deletion used. This used to
    # count on a tenant-only connection, where `memory_embeddings`' strict
    # workspace predicate matches nothing, so it reported success whatever was
    # left. Shared rows (another person's, the reference corpora, meetings) are
    # not this person's and are not counted; rows under one of the account's
    # reserved names are never deleted, so they are counted as kept.
    remaining = kept = 0
    for workspace_id in await tenant_workspaces(principal):
        async with workspace_connection(_in_workspace(principal, workspace_id)) as conn:
            left_here, kept_here = await _count_personal_memories(conn, principal)
            remaining += left_here
            kept += kept_here
    if remaining or kept:
        problems = []
        if remaining:
            problems.append(f"{remaining} embedding(s) still present after the row deletion")
        if kept:
            problems.append(
                f"{kept} memory row(s) not deleted: they are filed under this "
                f"account's login, which is a name IRA reserves for its own memory, "
                f"and deletion never touches a reserved name "
                f"(docs/security/RESERVED_OWNER_LOGIN_2026-10-19.md)")
        return StoreResult(
            store="pgvector", ok=False, unerasable=remaining + kept,
            error="; ".join(problems),
        )
    return StoreResult(store="pgvector", ok=True,
                       note="this person's memories were removed with their rows; "
                            "shared memories were not in scope")


async def erase_redis(principal, *, scope: str) -> StoreResult:
    """Everything under this tenant's Redis namespace.

    The namespace is derived by `core/identity/namespacing.py` rather than
    assembled here — two call sites that disagree about the scheme is how one
    tenant's keys survive another's deletion.
    """
    from core.identity.namespacing import tenant_prefix

    try:
        from utils.redis_client import get_redis

        client = get_redis()
    except Exception as exc:  # noqa: BLE001
        return StoreResult(
            store="redis", ok=False, error=f"redis unavailable: {type(exc).__name__}"
        )

    pattern = f"{tenant_prefix(principal)}*"
    deleted = 0
    try:
        cursor = 0
        while True:
            cursor, keys = await client.scan(cursor=cursor, match=pattern, count=500)
            if keys:
                deleted += await client.delete(*keys)
            if cursor == 0:
                break
    except Exception as exc:  # noqa: BLE001
        return StoreResult(
            store="redis", ok=False, deleted=deleted,
            error=f"{type(exc).__name__}: {exc}",
        )
    return StoreResult(store="redis", ok=True, deleted=deleted)


async def erase_object_storage(principal, *, scope: str) -> StoreResult:
    """Files, exports and voice artefacts.

    Phase 1 defined `ObjectStore` as an interface whose default implementation
    denies, and no backend is wired yet. This reports that honestly instead of
    returning a success for a store that does not exist — a green tick here
    would be the exact failure this module is written to avoid.
    """
    from core.interfaces.storage import ObjectStore  # noqa: F401  (documents the seam)

    return StoreResult(
        store="object_storage",
        ok=False,
        error="no object-storage backend is configured in this build",
        note=(
            "Nothing is stored there yet, so there is nothing to erase — but "
            "this is reported as unreached rather than done, so the answer does "
            "not silently become wrong when a backend is added."
        ),
    )


async def erase_audit(principal, *, scope: str) -> StoreResult:
    """Redact the audit trail. See ADR-0004.

    Content is erased; the proof that an action happened is kept. Records
    written before the chain-v2 migration cannot be redacted without making the
    chain unverifiable, and are counted rather than quietly skipped.
    """
    from trust.ledger import redact_records, unredactable_count

    if scope not in ("everything", "account"):
        return StoreResult(
            store="audit", ok=True,
            note=f"the audit trail is not in scope for {scope!r}",
        )

    user_key = principal.username or str(principal.user_id)
    try:
        # P10-03: both are tenant-bound now, and the principal is passed
        # explicitly rather than relying on a request ContextVar — erasure runs
        # from a worker as well as a request, and a redaction that silently
        # redacted nothing would report a completed erasure to somebody
        # exercising a right.
        redacted = await redact_records(
            user_id=user_key, reason=f"erasure: {scope}", principal=principal)
        residue = await unredactable_count(user_id=user_key, principal=principal)
    except Exception as exc:  # noqa: BLE001
        return StoreResult(
            store="audit", ok=False, error=f"{type(exc).__name__}: {exc}"
        )

    # Phase 7: Action Receipts are the same kind of thing as a decision record —
    # append-only evidence containing personal data — so they are REDACTED here
    # rather than deleted, on the line ADR-0004 drew. Failing to redact them
    # fails the whole audit store: a receipt still naming a message the user
    # asked to have erased is exactly the residue this report exists to surface.
    try:
        from core.connectors.store import redact_receipts

        redacted += await redact_receipts(principal)
    except Exception as exc:  # noqa: BLE001
        return StoreResult(
            store="audit", ok=False, redacted=redacted,
            error=(
                f"the trust ledger was redacted but Action Receipts were not "
                f"({type(exc).__name__}: {exc}). Receipts still name the "
                f"messages and documents IRA read."
            ),
        )

    # Blocker B2, ADR-0008. `agent_runs` and `tool_calls` are the two of the six
    # unreached tables that ARE audit-shaped: the fact that an agent ran, or that
    # a tool call was allowed or denied, has accountability value the user may
    # one day need. What has none is what they were ABOUT — `agent_runs.metadata`
    # is unbounded JSONB, and `tool_calls.purpose` / `.result_summary` are free
    # text describing what was read.
    #
    # So the shape survives and the content goes, on the same line ADR-0004 drew
    # for `permission_decisions` and Phase 7 drew for `action_receipts`.
    # `tool_calls.decision` stays: "IRA denied this" is a claim worth keeping,
    # and it says nothing about what "this" was once the purpose is blank.
    #
    # NOT fail-soft. A tool call still naming a document the user asked to have
    # erased is exactly the residue this report exists to surface, so a failure
    # here fails the audit store rather than being logged and forgotten.
    try:
        from core.identity.db_context import tenant_connection

        async with tenant_connection(principal) as conn:
            runs = await conn.execute(
                "UPDATE agent_runs SET metadata = '{}'::jsonb "
                "WHERE tenant_id = $1 AND metadata <> '{}'::jsonb",
                principal.tenant_id,
            )
            calls = await conn.execute(
                "UPDATE tool_calls SET purpose = '', result_summary = '' "
                "WHERE tenant_id = $1 AND (purpose <> '' OR result_summary <> '')",
                principal.tenant_id,
            )
            # Phase 3B Batch 1C. `biometric_audit` records that a voiceprint
            # match was attempted and how it scored. The statistics are the
            # evidence and stay; the attribution is the personal data and goes.
            # 038's append-only trigger permits exactly this — clearing
            # attribution to NULL — and refuses every other mutation, so the
            # redaction cannot become a rewrite.
            #
            # Scoped by BOTH the subject and the tenant, and both halves matter.
            #
            # `user_id` because that is what identifies the person; `tenant_id`
            # because an UPDATE requires the resulting row to remain visible
            # under the SELECT policy, and a row redacted out of its tenant
            # would be visible to nobody — PostgreSQL refuses that update
            # outright. It is also the right scope: erasing an account in one
            # tenant must not reach into another tenant's audit evidence, the
            # same rule every DELETE in `erase_postgres` follows.
            #
            # 038's `biometric_audit_attribution_implies_tenant` is what makes
            # this complete rather than merely safe: a row naming a person
            # always carries a tenant, so there is no attributed row this
            # statement cannot reach.
            bio = await conn.execute(
                "UPDATE biometric_audit SET user_id = NULL "
                "WHERE user_id = $1 AND tenant_id = $2",
                principal.user_id, principal.tenant_id,
            )

            # Batch 11. `tenant_invitations` is EXEMPT from deletion and stays
            # so: it is the record of how somebody came to hold access to an
            # organisation, and the membership it produced keeps working after
            # an erasure. `rollback/055` refuses to drop the table for the same
            # reason. Destroying it removes the audit trail for access that
            # survives it.
            #
            # But an accepted invitation retains the invitee's ADDRESS, so
            # erasure left a copy of it behind. That was recorded as a residual
            # risk from Batch 5 onward, with the end state already named:
            # redact the address, keep the row. This is that.
            #
            # `email` goes; `invited_by`, `role`, `accepted_at` and the token
            # hash stay, so "who let this person in, to what, and when" is still
            # answerable. The address is the only column that identifies the
            # person rather than the grant.
            #
            # Matched on the NORMALISED address, because that is what the
            # unique index and every lookup use — matching the raw column would
            # miss an invitation sent to `Alice@Example.com` for an account
            # stored as `alice@example.com`.
            #
            # Scoped by tenant as well as address, for the reason the biometric
            # redaction above records: an UPDATE must leave the row visible
            # under the SELECT policy, and erasing an account in one tenant must
            # not reach another tenant's evidence.
            # `users` carries no RLS, so this reads on the connection already
            # open. The row still exists at this point: `erase_postgres` removes
            # the account's data and its memberships, not the identity record —
            # that is what keeps "this account was erased" answerable.
            erased_address = await conn.fetchval(
                "SELECT email FROM users WHERE id = $1", principal.user_id)

            invites = "UPDATE 0"
            if erased_address:
                invites = await conn.execute(
                    "UPDATE tenant_invitations SET email = '' "
                    " WHERE tenant_id = $1 AND email <> '' "
                    "   AND normalise_email(email) = normalise_email($2)",
                    principal.tenant_id, erased_address,
                )
        for result in (runs, calls, bio, invites):
            try:
                redacted += int(str(result).rsplit(" ", 1)[-1])
            except ValueError:  # pragma: no cover - driver-dependent
                pass

        # Batch 11. `auth_events` is the same shape as `biometric_audit`: an
        # append-only table whose rows are evidence and whose attribution is
        # personal data. `061`'s trigger permits exactly the clearing below and
        # refuses deletion outright, so this is a redaction by construction
        # rather than by convention.
        #
        # Owned by `auth_audit` rather than written out here, because the
        # columns that count as attribution are `061`'s decision and this module
        # should not hold a second opinion about them. Its own connection: the
        # UPDATE it issues needs the row to stay visible under a SELECT policy
        # keyed on `app.user_id`, which is exactly what `tenant_connection`
        # binds — and the call is outside the block above only so the two
        # statements do not share a transaction that a failure in one would roll
        # the other back out of.
        #
        # NOT best-effort here, unlike `record`. Recording must never break a
        # login; erasing must never claim to have reached something it did not.
        from core.identity import auth_audit

        redacted += await auth_audit.redact_for(principal)
    except Exception as exc:  # noqa: BLE001
        return StoreResult(
            store="audit", ok=False, redacted=redacted,
            error=(
                f"the trust ledger and receipts were redacted but agent runs, "
                f"tool calls, biometric audit rows or authentication events "
                f"were not ({type(exc).__name__}: {exc}). Tool calls still "
                f"describe what IRA read, and audit rows still name the "
                f"subject."
            ),
        )

    if residue < 0:
        return StoreResult(
            store="audit", ok=False, redacted=redacted, unerasable=0,
            error="could not determine how many records remain unerasable",
        )
    return StoreResult(
        store="audit", ok=True, redacted=redacted, unerasable=residue,
        note=(
            "Content erased; the record that an action happened is kept so the "
            "audit trail stays verifiable."
            if residue == 0
            else (
                f"{residue} older record(s) keep their content: their integrity "
                f"proof includes it, so erasing it would make the trail "
                f"unverifiable."
            )
        ),
    )


async def erase_keys(principal, *, scope: str) -> StoreResult:
    """Destroy the tenant's data-encryption keys. Crypto-shredding, blocker B3.

    The other erasers delete rows, which means finding every row. This one
    destroys the key instead, and afterwards a row that was missed — in a
    replica, an unvacuumed page, a WAL segment — is ciphertext nothing can open.
    That is a different and stronger kind of deletion for the one category of
    data where it is available: connector credentials.

    Only under `account`. A user clearing their conversations has not asked to
    lose their connected accounts, and destroying the key would do exactly that
    with no way back — the narrower scopes deliberately leave it alone.

    Ordering, which matters: `erase_postgres` runs first (it is earlier in
    `KNOWN_STORES`) and its `account` scope deletes `connector_accounts` rows.
    Destroying the key afterwards is therefore belt and braces — it covers the
    copies the DELETE could not reach, not the rows it could.

    The honest bound, repeated from `030_key_management.sql` because it is the
    one thing a user could otherwise over-read: **a backup taken before the
    destruction still holds the wrapped key.** Shredding removes the key from
    the present, not from the past. `backup_residue_until` in the outcome is
    what the user is shown about that window.
    """
    if scope != "account":
        return StoreResult(
            store="keys", ok=True,
            note=(
                f"encryption keys are destroyed only when the whole account is "
                f"erased; {scope!r} leaves connected accounts working"
            ),
        )

    # Imported here rather than at module scope: `core/connectors/` owns
    # credentials, and the architecture test in tests/architecture/ checks that
    # nothing outside it reaches key material. `destroy_keys` returns none — it
    # destroys — which is why this import is permitted and `dek_for` is not.
    from core.connectors.keyring import destroy_keys

    try:
        result = await destroy_keys(
            principal,
            reason="account erasure requested by the user",
        )
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return StoreResult(
            store="keys", ok=False,
            error=f"{type(exc).__name__}: {exc}",
            note=(
                "The encryption keys were NOT destroyed. Any credential "
                "ciphertext that survived the row deletion remains openable."
            ),
        )

    # ── The second factor's key material (P2-05) ─────────────────────────────
    #
    # A separate key hierarchy and therefore a separate destruction.
    # `destroy_keys` above shreds `tenant_data_keys`, which protects connector
    # credentials; MFA seeds are sealed under per-user DEKs in `user_mfa_keys`,
    # deliberately NOT under the tenant key — a tenant-scoped key around a
    # secret a tenant administrator must never read would put the material that
    # opens it somewhere that administrator can reach.
    #
    # It belongs here rather than in `erase_postgres` for three reasons that all
    # come from this function already being the right shape: `keys` is in
    # KNOWN_STORES so the destruction is counted and reported rather than folded
    # into a row count; the `scope != "account"` guard above already stops a
    # narrower erasure from destroying a credential; and `tests/readiness/
    # test_erasure_coverage.py` checks SHREDDED tables against THIS function.
    #
    # The same honest bound applies as for the tenant key: a backup taken before
    # now still holds the wrapped DEK.
    mfa_shredded = 0
    try:
        from security.mfa_crypto import destroy_mfa_keys

        mfa_shredded = await destroy_mfa_keys(
            principal, reason="account erasure requested by the user")
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return StoreResult(
            store="keys", ok=False,
            error=f"{type(exc).__name__}: {exc}",
            note=(
                "The MFA key material was NOT destroyed. Any second-factor "
                "ciphertext that survived the row deletion remains openable — "
                "this erasure is NOT complete."
            ),
        )

    mfa_note = (
        f"; shredded {mfa_shredded} MFA key(s) — the second factor's seed is "
        f"permanently unopenable, including copies this deletion did not reach"
        if mfa_shredded else
        "; this account had no MFA key material to destroy"
    )

    if not result.versions_destroyed and not result.already_destroyed:
        return StoreResult(
            store="keys", ok=True,
            deleted=mfa_shredded,
            note=(
                "this account never had a connector encryption key, because no "
                "connector was ever connected — there was nothing to destroy"
                + mfa_note
            ),
        )
    return StoreResult(
        store="keys", ok=True,
        deleted=len(result.versions_destroyed) + mfa_shredded,
        note=(
            f"destroyed key version(s) {list(result.versions_destroyed)}; "
            f"credentials sealed under them are permanently unopenable, "
            f"including copies this deletion did not reach. A backup taken "
            f"BEFORE now still holds the wrapped key."
            if result.versions_destroyed
            else
            f"key version(s) {list(result.already_destroyed)} were already "
            f"destroyed; nothing further to do"
        ) + mfa_note,
    )


def default_erasers(principal, *, scope: str) -> dict[str, Eraser]:
    """The standard set, bound to one principal and scope."""
    return {
        "postgres": lambda: erase_postgres(principal, scope=scope),
        "pgvector": lambda: erase_vectors(principal, scope=scope),
        "redis": lambda: erase_redis(principal, scope=scope),
        "object_storage": lambda: erase_object_storage(principal, scope=scope),
        "audit": lambda: erase_audit(principal, scope=scope),
        "keys": lambda: erase_keys(principal, scope=scope),
    }


__all__ = [
    "BACKUP_RETENTION_DAYS",
    "KNOWN_STORES",
    "ErasureOutcome",
    "Eraser",
    "StoreResult",
    "default_erasers",
    "erase_audit",
    "erase_keys",
    "erase_object_storage",
    "erase_postgres",
    "erase_redis",
    "erase_vectors",
    "missing_store",
    "run_erasure",
]


# ── Whose rows a person's erasure and export reach (ADR-0010) ──────────────
#
# Most tables that "delete everything" and "export everything" cover have
# row-level policies that check only the tenant and the workspace. Through those
# policies alone, one person's erasure deleted every member's rows, and their
# export handed over every member's rows
# (`docs/testing/evidence/everything-scope-2026-10-20/`).
#
# The owner decided on 2026-10-20 (ADR-0010) that these actions reach only the
# person's own rows. This section is where each table says how to find them:
#
#   * `memory_embeddings`: the person's own memory namespaces, plus the Memory
#     Vault for the owner of a private install
#     (`retention.personal_memory_namespaces`, PR #67);
#   * a table whose column holds a NAME (ADR-0010 Group A): any name the
#     account is known by, its id as text included
#     (`retention._account_memory_names`). Writers store either the signed-in
#     account name or the user's id, and this matches both;
#   * a table whose column holds the user's ID: `principal.user_id`;
#   * `realtime_events`: through its session's `user_id`;
#   * a table that records only who CREATED a row, `created_by` (ADR-0010
#     Group B): the rows this person created. The owner decided (Q2) that those
#     are theirs. A row with no author is nobody's to erase or export: it is
#     kept, and erasure reports how many (`UNATTRIBUTED`). Writers record the
#     author from the connection's `app.user_id` only when it is an account, so
#     a row a background job writes has none; and no writer recorded one before
#     this change, so every older row has none;
#   * a table that had no person column until 095, `owner_user_id` (ADR-0010
#     Group C, Q3): the rows owned by this person. Writers record the owner as
#     Group B's record the author, and a row with no owner is kept and counted
#     the same way (Q4).
#
# 095 also gave the existing rows of both groups an owner where that is
# certain: in a tenant with exactly one membership row, every row is that
# member's. Elsewhere they stay without one.
#
# Every statement is written out in full rather than assembled, so no
# identifier is ever formatted into SQL.
#
# A table that is not listed here is still erased and exported workspace-wide,
# and `tests/tenancy/test_everything_is_personal.py` fails when a table that
# names a person is missing from this list.

MEMORY = "memory"
NAMES = "names"
USER_ID = "user_id"
AUTHOR = "author"
OWNER = "owned"

#: table -> (what is bound to the person parameter, the erasure DELETE, the
#: export SELECT). In each DELETE, $1 is the tenant and $2 the person; in each
#: SELECT, $1 is the person and $2 the row cap. `export.py` reads it too.
PERSONAL: dict[str, tuple[str, str, str]] = {
    "memory_embeddings": (
        MEMORY,
        "DELETE FROM memory_embeddings WHERE tenant_id = $1 AND user_id = ANY($2::text[])",
        "SELECT * FROM memory_embeddings WHERE user_id = ANY($1::text[]) LIMIT $2",
    ),
    "privacy_envelopes": (
        NAMES,
        "DELETE FROM privacy_envelopes WHERE tenant_id = $1 AND owner_id = ANY($2::text[])",
        "SELECT * FROM privacy_envelopes WHERE owner_id = ANY($1::text[]) LIMIT $2",
    ),
    "envelope_members": (
        NAMES,
        "DELETE FROM envelope_members WHERE tenant_id = $1 AND user_id = ANY($2::text[])",
        "SELECT * FROM envelope_members WHERE user_id = ANY($1::text[]) LIMIT $2",
    ),
    "commitments": (
        NAMES,
        "DELETE FROM commitments WHERE tenant_id = $1 AND user_id = ANY($2::text[])",
        "SELECT * FROM commitments WHERE user_id = ANY($1::text[]) LIMIT $2",
    ),
    "lifeos_events": (
        NAMES,
        "DELETE FROM lifeos_events WHERE tenant_id = $1 AND user_id = ANY($2::text[])",
        "SELECT * FROM lifeos_events WHERE user_id = ANY($1::text[]) LIMIT $2",
    ),
    "files": (
        NAMES,
        "DELETE FROM files WHERE tenant_id = $1 AND user_id = ANY($2::text[])",
        "SELECT * FROM files WHERE user_id = ANY($1::text[]) LIMIT $2",
    ),
    "cognitive_events": (
        NAMES,
        "DELETE FROM cognitive_events WHERE tenant_id = $1 AND user_id = ANY($2::text[])",
        "SELECT * FROM cognitive_events WHERE user_id = ANY($1::text[]) LIMIT $2",
    ),
    "cognitive_goals": (
        NAMES,
        "DELETE FROM cognitive_goals WHERE tenant_id = $1 AND owner_id = ANY($2::text[])",
        "SELECT * FROM cognitive_goals WHERE owner_id = ANY($1::text[]) LIMIT $2",
    ),
    "cognitive_predictions": (
        NAMES,
        "DELETE FROM cognitive_predictions WHERE tenant_id = $1 AND owner_id = ANY($2::text[])",
        "SELECT * FROM cognitive_predictions WHERE owner_id = ANY($1::text[]) LIMIT $2",
    ),
    "cognitive_prediction_outcomes": (
        NAMES,
        "DELETE FROM cognitive_prediction_outcomes WHERE tenant_id = $1 AND owner_id = ANY($2::text[])",
        "SELECT * FROM cognitive_prediction_outcomes WHERE owner_id = ANY($1::text[]) LIMIT $2",
    ),
    "cognitive_cycles": (
        NAMES,
        "DELETE FROM cognitive_cycles WHERE tenant_id = $1 AND owner_id = ANY($2::text[])",
        "SELECT * FROM cognitive_cycles WHERE owner_id = ANY($1::text[]) LIMIT $2",
    ),
    "realtime_events": (
        USER_ID,
        "DELETE FROM realtime_events WHERE tenant_id = $1 AND session_id IN "
        "(SELECT id FROM realtime_sessions WHERE user_id = $2)",
        "SELECT * FROM realtime_events WHERE session_id IN "
        "(SELECT id FROM realtime_sessions WHERE user_id = $1) LIMIT $2",
    ),
    "realtime_sessions": (
        USER_ID,
        "DELETE FROM realtime_sessions WHERE tenant_id = $1 AND user_id = $2",
        "SELECT * FROM realtime_sessions WHERE user_id = $1 LIMIT $2",
    ),
    "connector_oauth_states": (
        USER_ID,
        "DELETE FROM connector_oauth_states WHERE tenant_id = $1 AND user_id = $2",
        "SELECT * FROM connector_oauth_states WHERE user_id = $1 LIMIT $2",
    ),
    "projects": (
        AUTHOR,
        "DELETE FROM projects WHERE tenant_id = $1 AND created_by = $2",
        "SELECT * FROM projects WHERE created_by = $1 LIMIT $2",
    ),
    "agents": (
        AUTHOR,
        "DELETE FROM agents WHERE tenant_id = $1 AND created_by = $2",
        "SELECT * FROM agents WHERE created_by = $1 LIMIT $2",
    ),
    "decisions": (
        AUTHOR,
        "DELETE FROM decisions WHERE tenant_id = $1 AND created_by = $2",
        "SELECT * FROM decisions WHERE created_by = $1 LIMIT $2",
    ),
    "calendar_events": (
        AUTHOR,
        "DELETE FROM calendar_events WHERE tenant_id = $1 AND created_by = $2",
        "SELECT * FROM calendar_events WHERE created_by = $1 LIMIT $2",
    ),
    "reminders": (
        AUTHOR,
        "DELETE FROM reminders WHERE tenant_id = $1 AND created_by = $2",
        "SELECT * FROM reminders WHERE created_by = $1 LIMIT $2",
    ),
    "entities": (
        AUTHOR,
        "DELETE FROM entities WHERE tenant_id = $1 AND created_by = $2",
        "SELECT * FROM entities WHERE created_by = $1 LIMIT $2",
    ),
    "edges": (
        AUTHOR,
        "DELETE FROM edges WHERE tenant_id = $1 AND created_by = $2",
        "SELECT * FROM edges WHERE created_by = $1 LIMIT $2",
    ),
    "business_events": (
        AUTHOR,
        "DELETE FROM business_events WHERE tenant_id = $1 AND created_by = $2",
        "SELECT * FROM business_events WHERE created_by = $1 LIMIT $2",
    ),
    "strategy_predictions": (
        AUTHOR,
        "DELETE FROM strategy_predictions WHERE tenant_id = $1 AND created_by = $2",
        "SELECT * FROM strategy_predictions WHERE created_by = $1 LIMIT $2",
    ),
    "strategy_calibration": (
        AUTHOR,
        "DELETE FROM strategy_calibration WHERE tenant_id = $1 AND created_by = $2",
        "SELECT * FROM strategy_calibration WHERE created_by = $1 LIMIT $2",
    ),
    "model_performance": (
        AUTHOR,
        "DELETE FROM model_performance WHERE tenant_id = $1 AND created_by = $2",
        "SELECT * FROM model_performance WHERE created_by = $1 LIMIT $2",
    ),
    "tasks": (
        OWNER,
        "DELETE FROM tasks WHERE tenant_id = $1 AND owner_user_id = $2",
        "SELECT * FROM tasks WHERE owner_user_id = $1 LIMIT $2",
    ),
    "people": (
        OWNER,
        "DELETE FROM people WHERE tenant_id = $1 AND owner_user_id = $2",
        "SELECT * FROM people WHERE owner_user_id = $1 LIMIT $2",
    ),
    "notifications": (
        OWNER,
        "DELETE FROM notifications WHERE tenant_id = $1 AND owner_user_id = $2",
        "SELECT * FROM notifications WHERE owner_user_id = $1 LIMIT $2",
    ),
    "briefings": (
        OWNER,
        "DELETE FROM briefings WHERE tenant_id = $1 AND owner_user_id = $2",
        "SELECT * FROM briefings WHERE owner_user_id = $1 LIMIT $2",
    ),
    "heartbeat_surfaced": (
        OWNER,
        "DELETE FROM heartbeat_surfaced WHERE tenant_id = $1 AND owner_user_id = $2",
        "SELECT * FROM heartbeat_surfaced WHERE owner_user_id = $1 LIMIT $2",
    ),
}

#: Group B and C table -> how many of its rows, through the connection's
#: boundary, record no author or owner. Erasure keeps them and says how many
#: (ADR-0010 Q2, Q4). `notifications` is erased in both passes
#: (`WORKSPACE_OPTIONAL_TABLES`), and a workspace pass also sees the rows with
#: no workspace, so each pass counts only its own: the bound workspace's rows,
#: or, in the tenant pass where none is bound, the rows with none.
UNATTRIBUTED: dict[str, str] = {
    "projects": "SELECT count(*) FROM projects WHERE created_by IS NULL",
    "agents": "SELECT count(*) FROM agents WHERE created_by IS NULL",
    "decisions": "SELECT count(*) FROM decisions WHERE created_by IS NULL",
    "calendar_events": "SELECT count(*) FROM calendar_events WHERE created_by IS NULL",
    "reminders": "SELECT count(*) FROM reminders WHERE created_by IS NULL",
    "entities": "SELECT count(*) FROM entities WHERE created_by IS NULL",
    "edges": "SELECT count(*) FROM edges WHERE created_by IS NULL",
    "business_events": "SELECT count(*) FROM business_events WHERE created_by IS NULL",
    "strategy_predictions": "SELECT count(*) FROM strategy_predictions WHERE created_by IS NULL",
    "strategy_calibration": "SELECT count(*) FROM strategy_calibration WHERE created_by IS NULL",
    "model_performance": "SELECT count(*) FROM model_performance WHERE created_by IS NULL",
    "tasks": "SELECT count(*) FROM tasks WHERE owner_user_id IS NULL",
    "people": "SELECT count(*) FROM people WHERE owner_user_id IS NULL",
    "notifications": (
        "SELECT count(*) FROM notifications WHERE owner_user_id IS NULL AND workspace_id "
        "IS NOT DISTINCT FROM NULLIF(current_setting('app.workspace_id', true), '')::uuid"
    ),
    "briefings": "SELECT count(*) FROM briefings WHERE owner_user_id IS NULL",
    "heartbeat_surfaced": "SELECT count(*) FROM heartbeat_surfaced WHERE owner_user_id IS NULL",
}

#: Group B table -> the UPDATE that releases this person's rows that other
#: people's rows still point to, run before their DELETE (ADR-0011). $1 is the
#: tenant, $2 the person.
#:
#: The life graph keeps one entity per name per workspace, and `edges` cascades
#: from `entities`. Deleting an entity Alice created would also delete Bob's
#: edges to it. Instead, an entity that an edge she did not write still points
#: to keeps its type and name, which those edges need, and loses her
#: description, embedding and authorship. It then names no one, so the DELETE
#: passes it by and the kept count includes it.
#:
#: Her own edges are deleted first (`edges` precedes `entities` in every pass),
#: so they never hold an entity back. An edge with no author does: it is kept
#: (ADR-0010 Q4), so the cascade must not take it. Edges share their entities'
#: workspace (046's composite foreign keys), so a workspace pass sees every edge
#: that could hold one back.
SHARED: dict[str, str] = {
    "entities": (
        "UPDATE entities SET created_by = NULL, description = NULL, embedding = NULL, "
        "updated_at = NOW() WHERE tenant_id = $1 AND created_by = $2 AND EXISTS ("
        "SELECT 1 FROM edges e WHERE (e.src_entity_id = entities.id "
        "OR e.dst_entity_id = entities.id) AND e.created_by IS DISTINCT FROM $2)"
    ),
}


async def person_value(conn, table: str, principal) -> Any:
    """What `PERSONAL[table]`'s person parameter is bound to, read on `conn`."""
    from core.privacy.retention import (
        _account_memory_names,
        personal_memory_namespaces,
    )

    kind = PERSONAL[table][0]
    if kind == MEMORY:
        return await personal_memory_namespaces(conn, principal)
    if kind == NAMES:
        return sorted(await _account_memory_names(conn, principal))
    return principal.user_id  # USER_ID, AUTHOR and OWNER


async def delete_own_rows(conn, table: str, principal) -> int:
    """Delete this person's rows from `table`, and nobody else's."""
    statement = PERSONAL[table][1]
    result = await conn.execute(
        statement, principal.tenant_id, await person_value(conn, table, principal))
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except ValueError:  # pragma: no cover — driver-dependent
        return 0


async def read_own_rows(conn, table: str, principal, limit: int) -> list:
    """This person's rows from `table`, at most `limit` of them."""
    statement = PERSONAL[table][2]
    return await conn.fetch(
        statement, await person_value(conn, table, principal), limit)


async def release_shared(conn, table: str, principal) -> int:
    """Release this person's rows in `table` that other people's rows still
    point to (`SHARED`), and return how many; 0 for a table not listed."""
    statement = SHARED.get(table)
    if statement is None:
        return 0
    result = await conn.execute(
        statement, principal.tenant_id, await person_value(conn, table, principal))
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except ValueError:  # pragma: no cover — driver-dependent
        return 0


async def count_unattributed(conn, table: str) -> int:
    """Rows of `table` that record no author or owner, through `conn`'s
    boundary; 0 for a table outside Groups B and C."""
    statement = UNATTRIBUTED.get(table)
    if statement is None:
        return 0
    return int(await conn.fetchval(statement) or 0)


def kept_note(kept: dict[str, int], released: dict[str, int] | None = None) -> str:
    """The erasure note for rows kept because they record no author or owner.

    `released` counts the person's own rows among them, kept for other people's
    links with the person's content removed (ADR-0011)."""
    if not kept:
        return ""
    detail = ", ".join(f"{table} {n}" for table, n in sorted(kept.items()))
    note = (f"Kept {sum(kept.values())} row(s) that record no author or owner, "
            f"so they are not attributed to anyone (ADR-0010): {detail}.")
    if released:
        n = sum(released.values())
        it, points = ("It is", "it") if n == 1 else ("They are", "them")
        detail = ", ".join(f"{table} {k}" for table, k in sorted(released.items()))
        note += (f" You created {n} of them. {it} kept because other people's rows "
                 f"point to {points}, with your description and authorship removed "
                 f"(ADR-0011): {detail}.")
    return note
