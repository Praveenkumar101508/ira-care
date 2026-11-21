"""Retention, enforced by something that runs (Phase 4 batch 06).

`026` gave every user `retention_settings` and `core/privacy/store.py` has read
and written them since. Reading the write path carefully, each of the three
settings fails in a different way, and only one of them is the simple case:

    conversation_retention_days   NOTHING reads it outside the settings page.
                                  No conversation has ever expired.

    memory_retention_days         Becomes a per-row `expires_at` at write time
                                  (`RetentionSettings.expiry_for`). The function
                                  that acts on `expires_at` —
                                  `store.expire_due_memories` — HAS NO CALLERS,
                                  and rows written before the user chose a
                                  window carry `expires_at IS NULL`, so they
                                  could never expire even once it did run.

    voice_retention_hours         Nothing to do — see below.

A retention period that is recorded and not applied is worse than none: the user
was told their conversations expire after N days, and they did not.
`IMPLEMENTED_VS_PLANNED.md` has carried `IMPLEMENTED_NOT_FULLY_VERIFIED` against
this since Phase 0, which understated it — the settings were implemented, the
enforcement did not exist.

── Memories expire twice, deliberately ─────────────────────────────────────

`expire_due_memories` soft-deletes (`deleted_at = NOW()`), and its own docstring
gives the argument against stopping there: *"a memory that is merely filtered on
read but still sitting in the table is one an unrelated query will find."* That
is right, and the function does not act on it.

So this module does both, in order:

  1. call `expire_due_memories`, which honours a per-row `expires_at` that may
     fall EARLIER than the window (a memory given a short life of its own, or a
     user who later shortened the setting);
  2. hard-delete rows older than `memory_retention_days`, which is the only
     thing that reaches rows written before the setting existed, and the only
     thing that makes the promise terminal.

Step 1 is not redundant with step 2 and neither subsumes the other. Giving the
orphaned function its first caller is half the point of this batch.

── `voice_retention_hours` has nothing to expire ───────────────────────────

It governs raw audio, and IRA stores none: `realtime_sessions.audio_stored` is
`GENERATED ALWAYS AS (FALSE)`, so the column cannot be true. This module says so
rather than running a DELETE that always matches zero rows and reporting it as
work done — which is how a control comes to look implemented.

── `auth_events` is reported, never deleted ────────────────────────────────

`061` made it append-only with no DELETE policy, DELETE revoked from `ira_app`,
and a trigger that raises for every role including the owner. That was
deliberate: *"the first thing an intruder does is remove the entry that shows
them arriving."* The consequence is that per-row expiry is **not expressible** —
this module counts what is past retention and says so.

Removal is `scripts/phase4/expire_auth_events.py`, added by the same batch: a
privileged operator path that exports the window, verifies the export, deletes
inside one transaction, records the pass in the log as `log_expired`, and
asserts the append-only trigger is enabled afterwards. `064` deliberately adds
no DELETE policy, no grant to `ira_app` and no SECURITY DEFINER wrapper, so
nothing the application can reach gained the ability to delete history.

Writing a job HERE that quietly dropped the trigger would have defeated the
control by way of the feature that was supposed to respect it. Refusing, and
naming what does it instead, is the point.

── Deletion is scoped by the bound connection, never by a WHERE alone ──────

Every statement runs through `workspace_connection`, so RLS decides which rows
are reachable and the predicate only narrows by AGE. A retention job that
trusted a `WHERE tenant_id = $1` would be one typo from expiring another
tenant's data, and the erasure work in Phase 9 settled that argument already.

It must be `workspace_connection` and not `tenant_connection`. All three tables
this touches — `conversations`, `messages`, `memory_embeddings` — carry the
workspace policies Batch 5C forced, so a tenant-bound connection leaves
`app.workspace_id` unset, every DELETE matches **zero rows silently**, and the
pass reports success having removed nothing. That is the P2-05 shape: the
failure is invisible precisely because RLS is doing its job.

The consequence is that a pass covers ONE workspace — the principal's bound
one. Nothing here picks a second: `enforce_for` counts the caller's other
workspaces and says how many were not covered, because "retention enforced" and
"retention enforced in one workspace of five" are different claims and only the
report can tell them apart.

── A memory window reaches the account's own memories, nobody else's (#30) ──

The workspace policy on `memory_embeddings` has no user clause, so a DELETE
narrowed by age alone removed every user's old memories in the workspace: one
person choosing 30 days cost a colleague who had chosen nothing. The memory
DELETE is therefore narrowed by AGE and by OWNERSHIP, under the rule the owner
decided on 2026-10-19 (`_own_memory_namespaces`):

  * a row is the account's own when its `user_id` is the account's UUID or a
    name the account is known by;
  * a reserved namespace (`memory.namespaces`) is never one account's own. The
    Memory Vault, `system`, meeting transcripts and the reference corpora are
    shared, and age out through the workspace's weekly purge
    (`memory.store.purge_old_memories`), not through one person's setting.

Conversations are narrowed by the POLICY rather than here (ADR-0009, 093):
`conversations` and `messages` carry `owner_user_id`, and their policies admit
only `owner_user_id = app.user_id`, which `workspace_connection` sets from the
verified principal. The conversation DELETEs below therefore reach only this
user's rows. A conversation nobody could be attributed keeps owner NULL and is
reached by no personal window.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("ira.privacy.retention")


@dataclass(frozen=True, slots=True)
class RetentionOutcome:
    """What a retention pass actually removed, counted rather than assumed."""

    conversations_deleted: int = 0
    messages_deleted: int = 0
    #: Marked `deleted_at` because their own `expires_at` had passed.
    memories_expired: int = 0
    #: Removed from the table because they were older than the window.
    memories_deleted: int = 0
    #: Rows past their retention window that this process may NOT remove.
    #: Non-zero is not a failure — it is the append-only guarantee working, and
    #: it is what an operator needs in order to act.
    auth_events_past_retention: int = 0
    #: Other workspaces this user belongs to, which this pass did NOT touch.
    workspaces_not_covered: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def anything_removed(self) -> bool:
        return bool(self.conversations_deleted or self.messages_deleted
                    or self.memories_expired or self.memories_deleted)


def _rowcount(result) -> int:
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):  # pragma: no cover - driver-dependent
        return 0


async def _own_memory_namespaces(conn, principal) -> list[str]:
    """The `memory_embeddings.user_id` values that are this account's own (#30).

    `user_id` is a free-form namespace, not a principal (`memory.namespaces`),
    so "this user's memories" has to be spelled out. An account owns rows filed
    under:

      * its UUID, as text;
      * `principal.username`: chat files memories under it
        (`api/routes/chat.py::_workspace_username`). It is the normalised email
        in the cloud edition and the account name in the private one;
      * the same names read from `users` for this id: `normalised_email`,
        `legacy_username`, and `external_subject` for a password account, which
        is where private provisioning keeps the account name. The scheduled pass
        builds its principal with `username=""` (`core/jobs/scheduler.py`); by
        the principal alone it would match none of the rows chat wrote, and
        report success.

    A reserved namespace is never an account's own, whatever the account is
    called: `is_reserved_namespace` compares the way delegated usernames are
    checked, so `Owner` does not claim the Memory Vault either. Rows an account
    filed under such a name are kept rather than deleted, which is the safe way
    to be wrong.

    `users` carries no RLS (`core/privacy/erasure.py` reads it the same way), so
    this reads on the connection already open.
    """
    from memory.namespaces import is_reserved_namespace

    names = await _account_memory_names(conn, principal)
    return sorted(n for n in names if not is_reserved_namespace(n))


async def personal_memory_namespaces(conn, principal) -> list[str]:
    """The `memory_embeddings.user_id` values one person's memory actions reach.

    The owner decided on 2026-10-20
    (`docs/security/MEMORY_ACTIONS_ARE_PERSONAL_2026-10-20.md`): listing,
    reviewing, editing, deleting, exporting and erasing memories reach only the
    person's own memories. That is `_own_memory_namespaces`, plus the Memory
    Vault for the owner of a private install, whose vault it is
    (`memory.vault.may_recall_vault`).

    Never reached: another person's memories, the reference corpora
    (`technical_library`, `self_knowledge`), meeting transcripts, `system`
    rows, and the shared workspace vault of the cloud edition, which workspace
    admins manage through `/api/v1/memory`.

    The workspace policy on `memory_embeddings` has no user clause, so this
    list is what keeps the actions personal. A database rule to the same
    effect is a separate decision.
    """
    from memory.namespaces import VAULT_NAMESPACE
    from memory.vault import may_recall_vault

    names = set(await _own_memory_namespaces(conn, principal))
    # The same test chat recall uses to include the vault, so the two cannot
    # disagree about whose vault it is.
    if may_recall_vault(principal):
        names.add(VAULT_NAMESPACE)
    return sorted(names)


async def _reserved_account_names(conn, principal) -> list[str]:
    """This account's names that are reserved, so `_own_memory_namespaces`
    leaves them out.

    Empty for every account a current install can create: delegated usernames
    are refused if reserved (`security/delegated.py`), and a new install refuses
    a reserved owner login (`core/identity/provisioning.py`). It is not empty
    for an install whose owner was already called, say, `owner` before that
    rule existed. Their chat memories sit under the reserved name and are kept,
    and erasure counts them as not deleted instead of reporting success
    (`core/privacy/erasure.py::erase_vectors`).
    """
    from memory.namespaces import is_reserved_namespace

    names = await _account_memory_names(conn, principal)
    return sorted(n for n in names if is_reserved_namespace(n))


async def _account_memory_names(conn, principal) -> set[str]:
    """Every name this account's memories can be filed under, reserved or not."""
    names = {str(principal.user_id)}
    if principal.username:
        names.add(principal.username)
    row = await conn.fetchrow(
        "SELECT normalised_email, legacy_username, "
        "       CASE WHEN auth_provider = 'password' THEN external_subject END "
        "         AS account_name "
        "  FROM users WHERE id = $1",
        principal.user_id)
    if row is not None:
        names.update(v for v in (row["normalised_email"], row["legacy_username"],
                                 row["account_name"]) if v)
    return names


async def enforce_for(
    principal, *, auth_event_retention_days: Optional[int] = None,
) -> RetentionOutcome:
    """Apply this user's retention settings. Returns what was removed.

    Runs under `workspace_connection`, so every statement is already narrowed to
    what this principal may reach; the predicates below only add age. The pass
    therefore covers the principal's BOUND workspace and no other — see the
    module docstring, and `workspaces_not_covered` on the result.

    A setting of 0 or None means "keep indefinitely" and is skipped rather than
    treated as "delete everything", which is the interpretation that would make
    a misconfiguration catastrophic instead of inert.
    """
    from core.identity.db_context import workspace_connection
    from core.privacy.store import expire_due_memories, load_settings
    from memory.namespaces import RETENTION_EXEMPT_NAMESPACES

    settings = await load_settings(principal)
    notes: list[str] = []
    conversations = messages = memories = 0

    # Step 1 of the two-stage memory expiry. Separate from the connection below
    # because it is the existing mechanism, called rather than reimplemented —
    # it honours a per-row `expires_at` that this module does not know about.
    expired = await expire_due_memories(principal)

    async with workspace_connection(principal) as conn:
        days = settings.conversation_retention_days
        if days and days > 0:
            # Messages first, then conversations. The FK is ON DELETE CASCADE,
            # so deleting the conversation would take the messages anyway — but
            # then the count would be a guess, and a retention report that
            # cannot say how many messages went is not evidence of anything.
            deleted = await conn.execute(
                "DELETE FROM messages WHERE conversation_id IN ("
                "  SELECT id FROM conversations "
                "   WHERE created_at < NOW() - ($1 || ' days')::interval)",
                str(days))
            messages = _rowcount(deleted)
            deleted = await conn.execute(
                "DELETE FROM conversations "
                " WHERE created_at < NOW() - ($1 || ' days')::interval",
                str(days))
            conversations = _rowcount(deleted)
        else:
            notes.append("conversations are kept indefinitely by this user's setting")

        days = settings.memory_retention_days
        if days and days > 0:
            # Step 2: by AGE, not by `expires_at`. A memory written before the
            # user chose a window has `expires_at IS NULL` and step 1 cannot
            # see it; without this the setting would apply only to the future.
            #
            # And by OWNERSHIP (#30): the workspace policy has no user clause,
            # so an age-only DELETE took every user's old memories in the
            # workspace. Only rows filed under this account's own names go.
            # Shared namespaces (vault, system, meetings) age out through the
            # workspace's weekly purge instead.
            #
            # The exemption is kept as well: no account name can be a
            # reference corpus, but if one ever were, the Technical Library
            # and self-knowledge would still not be one person's to delete.
            own = await _own_memory_namespaces(conn, principal)
            deleted = await conn.execute(
                "DELETE FROM memory_embeddings "
                " WHERE created_at < NOW() - ($1 || ' days')::interval "
                "   AND user_id = ANY($2::text[]) "
                "   AND user_id <> ALL($3::text[])",
                str(days), own, sorted(RETENTION_EXEMPT_NAMESPACES))
            memories = _rowcount(deleted)
            notes.append(
                "memories shared across the workspace (Memory Vault, system, "
                "meeting transcripts) are not removed by one person's window; "
                "the workspace's weekly purge ages them out")
        else:
            notes.append("memories are kept indefinitely by this user's setting")

        # Raw voice audio is never stored — `realtime_sessions.audio_stored` is
        # GENERATED ALWAYS AS (FALSE), so the column cannot be true. Running a
        # DELETE here would match zero rows forever and report it as work.
        if settings.voice_retention_hours:
            notes.append(
                "voice_retention_hours has nothing to expire: no raw audio is "
                "stored (realtime_sessions.audio_stored is GENERATED FALSE)")

        past = 0
        if auth_event_retention_days and auth_event_retention_days > 0:
            past = int(await conn.fetchval(
                "SELECT count(*) FROM auth_events "
                " WHERE created_at < NOW() - ($1 || ' days')::interval",
                str(auth_event_retention_days)) or 0)
            if past:
                notes.append(
                    f"{past} authentication event(s) are past retention and were "
                    f"NOT deleted: 061 makes the log append-only for every role, "
                    f"which is deliberate. Removal is an operator action: "
                    f"python -m scripts.phase4.expire_auth_events expire "
                    f"--days {auth_event_retention_days} --export <path> --confirm"
                )

        # A pass covers the bound workspace. Saying how many it did not cover
        # is the difference between a partial result and a partial result that
        # looks complete.
        uncovered = int(await conn.fetchval(
            "SELECT count(*) FROM workspace_memberships "
            " WHERE user_id = $1 AND status = 'active' AND workspace_id <> $2",
            principal.user_id, principal.workspace_id) or 0)
        if uncovered:
            notes.append(
                f"this pass covered one workspace; {uncovered} other workspace(s) "
                f"this user belongs to were not touched and need their own pass")

    logger.info("retention pass removed %d conversation(s), %d message(s), "
                "%d memor(ies); expired %d by their own expires_at",
                conversations, messages, memories, expired)
    return RetentionOutcome(
        conversations_deleted=conversations,
        messages_deleted=messages,
        memories_expired=expired,
        memories_deleted=memories,
        auth_events_past_retention=past,
        workspaces_not_covered=uncovered,
        notes=tuple(notes),
    )


__all__ = ["RetentionOutcome", "enforce_for"]
