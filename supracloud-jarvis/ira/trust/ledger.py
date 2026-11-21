"""
trust/ledger.py — hash-chained action records (brief §9).

Chaining is identical to coding/command_audit.py (sha256 over prev_hash +
canonical JSON of the record body) but persisted in Postgres (trust_ledger,
migration 020, append-only trigger). The pure functions (compute_hash,
verify_chain) are separated from the thin DB layer so the chain logic is
fully unit-testable without a database.

Anchor discipline: call export_head() after important actions and store the
value somewhere this database cannot rewrite (a file the owner keeps, a
printed Proof Card, another machine). verify() against records + that
anchor is what makes tampering evident.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from core.identity.db_context import MissingTenantContext
from utils.db import acquire

logger = logging.getLogger("ira.trust.ledger")


class LedgerNotBound(RuntimeError):
    """A ledger write was attempted with no tenant context to write it under.

    Distinct from "the database is down" on purpose, and that distinction is the
    whole of P9-02.

    ── What this exists to stop happening a third time ────────────────────

    `append_record`'s own docstring records the first time: *"023 made the
    column NOT NULL without one, and every ledger write had been failing since,
    silently, because the callers are fail-soft."*

    It happened again. `trust_ledger` has RLS **enabled and forced** with a
    policy keyed on `app.tenant_id`; `acquire()` never sets that GUC, so the
    INSERT is refused, and every caller wraps the append in `except` and logs
    "trust ledger unavailable". Probed against the real schema:

        BARE  INSERT -> refused: new row violates row-level security policy
        BARE  SELECT -> 0 rows
        BOUND INSERT -> succeeded
        BOUND SELECT -> 65 rows

    A fail-soft writer in front of a tamper-evident audit record turns every
    schema tightening into silent data loss. The binding below is this
    occurrence's fix; making the swallow impossible is Phase 10's subject, and
    this exception type is what a caller will be able to distinguish on.
    """


def _bound_principal():
    """The principal this request is running as, or None.

    Phase 8 bound this in the workspace dependency, after membership was
    verified, precisely so a chokepoint forty-three modules away could attribute
    a call without every caller threading a principal through. The ledger is
    another such chokepoint, so it reads the same source rather than growing a
    parallel one.

    Returns None outside a request — a worker, a private install, a test — and
    None means "no tenant context", never "any tenant".
    """
    try:
        from core.runtime.request_context import get_principal
        return get_principal()
    except Exception:  # noqa: BLE001 — no context is not an error
        return None


class LedgerTenantMismatch(RuntimeError):
    """A caller named a tenant that is not the tenant it is running as.

    Separate from :class:`LedgerNotBound` because it is a different mistake with
    a different fix: not "nobody told us whose action this is" but "two sources
    disagree about it". Refusing is the only honest answer — writing under the
    bound tenant would silently ignore the argument, and writing under the named
    one would let an argument override a verified membership, which is the whole
    thing `core.identity.db_context` exists to prevent.
    """


def _binding_for(tenant_id: Optional[str], principal):
    """The Principal this append will bind the connection to, or raise.

    Resolution order, most specific first:

      1. an explicit `principal=` — how background work names its tenant
         (`worker/briefing.py` and five others already do exactly this via
         `default_workspace_job_principal`);
      2. the request principal Phase 8 bound in the workspace dependency, AFTER
         membership was verified;
      3. in the PRIVATE edition only, the single tenant that edition has.

    Step 3 is a fallback and fallbacks are how defaults become leaks, so it is
    gated on the edition rather than on "did we find one". In `private` there is
    exactly one tenant and `PRIVATE_TENANT_ID` is a constant, not a guess. In
    `cloud` there is no such thing as "the tenant", so there is no step 3 and the
    append raises instead — a worker that forgot to pass a principal gets an
    error naming the fix, not somebody else's audit trail.
    """
    resolved = principal if principal is not None else _bound_principal()

    if resolved is None:
        from core.editions import is_cloud
        if is_cloud():
            raise LedgerNotBound(
                "trust ledger append with no tenant context. trust_ledger has "
                "FORCE ROW LEVEL SECURITY keyed on app.tenant_id, so an unbound "
                "connection cannot write it — and because every caller is "
                "fail-soft, the write would be swallowed and the audit trail "
                "would simply stay empty. Pass principal=... (background work: "
                "core.identity.background.default_workspace_job_principal) or "
                "run inside a request, which binds one."
            )
        from core.identity.principal import PRIVATE_TENANT_ID, system_principal
        resolved = system_principal(
            tenant_id=PRIVATE_TENANT_ID, reason="trust_ledger"
        )

    if tenant_id is not None and str(tenant_id) != str(
        getattr(resolved, "tenant_id", None)
    ):
        raise LedgerTenantMismatch(
            f"append_record was asked to write tenant {tenant_id} while running "
            f"as tenant {getattr(resolved, 'tenant_id', None)}. Refusing rather "
            f"than picking one."
        )
    return resolved


#: Namespace half of the per-tenant advisory lock. Any stable int32 will do; it
#: is `hashtext` of the table name so a second lock elsewhere in the schema
#: cannot collide with this one by accident.
_LOCK_NAMESPACE_SQL = "hashtext('trust_ledger')"

GENESIS = "0" * 64

LEDGER_KINDS = frozenset({
    "action", "approval", "execution", "verification", "rollback",
    "security_event",
})


# ── Pure chain logic ──────────────────────────────────────────────────────────

def canonical(body: dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def compute_hash(prev_hash: str, body: dict[str, Any]) -> str:
    """Chain v1: sha256 over prev_hash ++ the canonical body itself.

    Unchanged, and still what every pre-existing record used. Kept because
    rewriting history to a new format would destroy the very property the
    chain exists to provide.
    """
    return hashlib.sha256(prev_hash.encode("ascii") + canonical(body)).hexdigest()


def body_digest(body: dict[str, Any]) -> str:
    """sha256 of the canonical body, on its own."""
    return hashlib.sha256(canonical(body)).hexdigest()


def compute_hash_v2(prev_hash: str, body: dict[str, Any]) -> str:
    """Chain v2: sha256 over prev_hash ++ the body's DIGEST, not the body.

    ── Why a second construction exists ─────────────────────────────────────

    Phase 5 owes a decision that Phases 2 and 4 both deferred: how a user can
    have their data erased when the audit log is append-only and holds real
    content. The ledger records `resource` and `purpose` — a file path, an
    email address, "email Sarah about the divorce". Claiming the audit log
    holds no personal data would simply be false.

    Under v1, `record` is *inside* the hash pre-image, so redacting it makes
    `compute_hash` disagree with the stored hash and the chain reads as
    tampered. There is no way to erase content and keep the proof.

    v2 hashes the body's digest instead. Redaction can then drop the body and
    keep `body_digest`, and every link still verifies exactly as before. What
    is lost after redaction is only the ability to show *what the record said*
    — not the ability to prove nothing was inserted, removed or reordered.

    This is additive. Records written before the migration stay v1 and verify
    the way they always did; `verify_chain` picks the construction per record
    from whether `body_digest` is present.
    """
    return hashlib.sha256(
        prev_hash.encode("ascii") + body_digest(body).encode("ascii")
    ).hexdigest()



# ── Anchors, and the era they belong to (P10-04) ─────────────────────────────

#: What an anchor decision can say. Deliberately three strings and not a bool.
#:
#: The owner's rule is *"an anchor may only verify the chain model it was
#: originally created for"*, and a boolean cannot express the third case: an
#: anchor that is neither verified nor invalid, because it anchors something
#: this deployment used to be. Collapsing it to `False` would report a genuine
#: historical record as tampering; collapsing it to `True` would claim a global
#: anchor proves a tenant's chain.
ANCHOR_VERIFIED = "VERIFIED"
ANCHOR_MISMATCH = "ANCHOR_MISMATCH"
ANCHOR_LEGACY_GLOBAL = "LEGACY_GLOBAL_ANCHOR/NOT_VALID_FOR_TENANT_CHAIN"
ANCHOR_WRONG_TENANT = "ANCHOR_BELONGS_TO_ANOTHER_TENANT"

#: The scope stamped into every anchor issued since P9-02.
ANCHOR_SCOPE_TENANT = "tenant"
#: The scope of anything issued before it — including anchors that carry no
#: scope at all, which is what a pre-P9-02 anchor looks like.
ANCHOR_SCOPE_LEGACY = "legacy_global"


def anchor_scope(anchor: dict[str, Any]) -> str:
    """Which chain model an anchor was created for.

    **Absence means legacy.** A pre-P9-02 anchor is a bare `{"seq", "hash"}`
    with no scope field, because there was only one chain and nothing to
    disambiguate. Reading a missing scope as "tenant" would be exactly the
    silent reinterpretation the owner's decision forbids — the anchor would then
    be checked against a chain it never anchored, and fail, and be reported as
    tampering.
    """
    scope = str(anchor.get("scope") or "").strip()
    return scope if scope in (ANCHOR_SCOPE_TENANT, ANCHOR_SCOPE_LEGACY) \
        else ANCHOR_SCOPE_LEGACY


def verify_against_anchor(records: list[dict[str, Any]], anchor: dict[str, Any],
                          *, tenant_id: Optional[str] = None,
                          allow_partial: bool = False) -> str:
    """Check a tenant's records against an externally held anchor.

    Returns one of the four `ANCHOR_*` strings. It does not raise for a failed
    comparison — the answer IS the return value, and a caller that wants an
    exception can compare.

    ── The three ways this can decline to verify, and why they differ ──────

    * `LEGACY_GLOBAL_ANCHOR/NOT_VALID_FOR_TENANT_CHAIN` — the anchor predates
      the cutover (migration `077`). It anchored one global chain; there is no
      global chain to compare it to any more. **Not verified, and not invalid.**
      The evidence is still real, it just does not answer this question.
    * `ANCHOR_BELONGS_TO_ANOTHER_TENANT` — a tenant-era anchor naming a
      different tenant. A separate answer from a mismatch because the fix is
      different: use the right anchor, not investigate a breach.
    * `ANCHOR_MISMATCH` — a tenant-era anchor for THIS tenant whose hash does
      not match. That is the one that means something is wrong with the chain.

    Rolling the first two into the third is how a migration gets reported as an
    intrusion, and how a real intrusion gets dismissed as "probably the old
    anchor thing".
    """
    scope = anchor_scope(anchor)
    if scope == ANCHOR_SCOPE_LEGACY:
        return ANCHOR_LEGACY_GLOBAL

    claimed = anchor.get("tenant_id")
    if tenant_id is not None and claimed is not None \
            and str(claimed) != str(tenant_id):
        return ANCHOR_WRONG_TENANT

    try:
        verify_chain(records, expected_head=str(anchor.get("hash")),
                     allow_partial=allow_partial)
    except ValueError:
        return ANCHOR_MISMATCH
    return ANCHOR_VERIFIED


async def chain_eras(*, principal: Optional[Any] = None) -> list[dict[str, Any]]:
    """This deployment's recorded chain eras, oldest first.

    Read from `trust_ledger_chain_eras` (migration `077`) rather than from a
    constant: every install cut over at a different `seq`, at a different time,
    and a constant changes with a deploy — so the one fact an auditor needs
    would be a property of the code they are running rather than of the database
    they are auditing.

    Empty list when the table is absent, which means `077` has not been applied
    and the boundary is genuinely unknown. NOT an assumption that everything is
    tenant-era: an unknown boundary must not be reported as a known one.
    """
    binding = _binding_for(None, principal)
    from core.identity.db_context import tenant_connection

    try:
        async with tenant_connection(binding) as conn:
            rows = await conn.fetch(
                "SELECT era, scope, ends_at_seq, recorded_at, note "
                "FROM trust_ledger_chain_eras ORDER BY era ASC")
        return [dict(r) for r in rows]
    except (LedgerNotBound, LedgerTenantMismatch, MissingTenantContext):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("chain_eras failed: %s", exc)
        return []


#: Value written into `record` when a record's content has been erased. Kept
#: recognisable so a reader can tell "erased on request" apart from "empty".
REDACTION_TOMBSTONE: dict[str, Any] = {"__redacted__": True}


def is_redacted(record: Any) -> bool:
    return isinstance(record, dict) and record.get("__redacted__") is True


def verify_chain(records: list[dict[str, Any]],
                 *, expected_head: Optional[str] = None,
                 allow_partial: bool = False) -> int:
    """Verify an ordered list of ledger records ({seq, user_id, action_id,
    task_id, kind, record, prev_hash, hash}). Returns the count; raises
    ValueError naming the first broken link. With expected_head (an anchor
    stored outside the DB) also confirms the final hash.

    allow_partial=True verifies a WINDOW of the chain (e.g. the most recent
    N records) — it seeds the expected prev_hash from the window's first
    record instead of requiring it to start at GENESIS, so internal linkage
    and per-record hashes are still checked."""
    if allow_partial and records:
        prev = records[0].get("prev_hash", GENESIS)
    else:
        prev = GENESIS
    count = 0
    for rec in records:
        body = {k: rec[k] for k in
                ("seq", "user_id", "action_id", "task_id", "kind", "record", "prev_hash")}
        if body["prev_hash"] != prev:
            raise ValueError(f"broken prev_hash link at seq={body['seq']}")

        stored_digest = rec.get("body_digest")
        if stored_digest:
            # ── Chain v2 ────────────────────────────────────────────────────
            # hash = sha256(prev_hash ++ body_digest), so the link verifies
            # whether or not the body is still present.
            if not is_redacted(body["record"]):
                # Content intact: the digest must still match the body, or
                # someone edited the record and left the digest behind.
                if body_digest(body) != stored_digest:
                    raise ValueError(f"body digest mismatch at seq={body['seq']}")
            if hashlib.sha256(
                prev.encode("ascii") + str(stored_digest).encode("ascii")
            ).hexdigest() != rec["hash"]:
                raise ValueError(f"hash mismatch at seq={body['seq']}")
        else:
            # ── Chain v1 ────────────────────────────────────────────────────
            # A v1 record cannot be redacted and still verified: its content is
            # inside the hash pre-image. Refusing loudly is the honest answer —
            # silently accepting it would let anyone blank a v1 record and have
            # the chain still report "verified".
            if is_redacted(body["record"]):
                raise ValueError(
                    f"seq={body['seq']} is redacted but has no body_digest: a "
                    f"v1 record's content is inside its hash, so its integrity "
                    f"cannot be verified once erased"
                )
            if compute_hash(prev, body) != rec["hash"]:
                raise ValueError(f"hash mismatch at seq={body['seq']}")

        prev = rec["hash"]
        count += 1
    if expected_head is not None and prev != expected_head:
        raise ValueError("chain head does not match the external anchor")
    return count


# ── DB layer ──────────────────────────────────────────────────────────────────

async def append_record(kind: str, *, user_id: str, action_id: str,
                        task_id: Optional[str] = None,
                        record: Optional[dict[str, Any]] = None,
                        tenant_id: Optional[str] = None,
                        principal: Optional[Any] = None,
                        journal_entry_id: Optional[str] = None) -> dict[str, Any]:
    """Append one chained record. Fail-LOUD, on a TENANT-BOUND connection.

    ── Why the connection is bound (P9-02) ─────────────────────────────────

    `trust_ledger` carries `ENABLE`/`FORCE ROW LEVEL SECURITY` with a policy
    whose `WITH CHECK` is `tenant_id = app.tenant_id`. This function used to
    write through a bare `acquire()`, which never sets that GUC, so every INSERT
    was refused — and every caller catches, logs "trust ledger unavailable" and
    continues, so nothing reported it. See :class:`LedgerNotBound` for the
    probe. `tenant_connection` is the one sanctioned place that sets the GUC,
    and it sets it from a Principal, so that is what this uses.

    ── Why `seq` is now reserved rather than predicted ─────────────────────

    `seq` is a global `BIGSERIAL`, but the head read is RLS-filtered and
    therefore per tenant. The old `head.seq + 1` was already only a guess (a
    rolled-back transaction burns a sequence value); once several tenants share
    the sequence it is reliably wrong. It mattered because `seq` is INSIDE the
    hashed body while the stored value came from the column default, so the two
    disagreed and the record could never verify. Probed on a three-tenant
    database:

        predicted seq=864   stored seq=867   -> verify_chain: body digest
                                                mismatch at seq=867

    `nextval` is called explicitly and the same value is both hashed and
    inserted, so what verifies is what is stored. The chain therefore has
    monotonic-but-GAPPED sequence numbers per tenant, which `verify_chain`
    already tolerates: it checks `prev_hash` linkage, never contiguity.

    ── Why an advisory lock as well as `FOR UPDATE` ────────────────────────

    `FOR UPDATE` locks the head ROW, so it serialises nothing when a tenant has
    no rows yet — two concurrent first appends would both read GENESIS and fork
    the chain. Single-tenant that was a once-per-install race; per tenant it is
    a once-per-tenant race, i.e. every new customer. The transaction-scoped
    advisory lock is keyed on the tenant, so appends serialise within a tenant
    and never across them.

    `tenant_id`, when given, is an ASSERTION checked against the bound tenant —
    not an instruction. See :class:`LedgerTenantMismatch`.

    Reads (`records_for`, `recent_records`, `export_head`) are still unbound and
    still return nothing under RLS. That is deliberate and owned: they are
    Phase 10's subject, and fixing the write first is what makes there be
    anything to read.

    ── `journal_entry_id`: a replay lands once (P1-09) ─────────────────────

    `trust/journal.py` holds records the ledger could not take and replays them
    here. Each carries the id of its journal entry, stored in `record` (so it is
    inside the hashed body). Under the same per-tenant advisory lock that
    serialises appends, a record already holding that id is returned as it is,
    marked `duplicate`, and nothing is inserted. A replay interrupted between
    this commit and the journal's "reconciled" line, and run again, therefore
    adds nothing twice, whichever process runs it.
    """
    if kind not in LEDGER_KINDS:
        raise ValueError(f"unknown ledger kind: {kind!r}")
    if not user_id or not action_id:
        raise ValueError("user_id and action_id are required")
    payload = record or {}
    if journal_entry_id is not None:
        payload = {**payload, "journal_entry_id": str(journal_entry_id)}

    binding = _binding_for(tenant_id, principal)
    bound_tenant = str(binding.tenant_id)

    from core.identity.db_context import tenant_connection

    # tenant_connection opens the transaction and sets app.tenant_id inside it;
    # both end together, so a pooled connection is returned carrying neither.
    async with tenant_connection(binding) as conn:
        await conn.execute(
            f"SELECT pg_advisory_xact_lock({_LOCK_NAMESPACE_SQL}, hashtext($1))",
            bound_tenant,
        )
        if journal_entry_id is not None:
            held = await _held_journal_entry(conn, action_id=action_id, kind=kind,
                                             journal_entry_id=str(journal_entry_id))
            if held is not None:
                logger.info("ledger append skipped: journal entry %s is already seq=%s",
                            journal_entry_id, held["seq"])
                return {"seq": int(held["seq"]), "hash": held["hash"],
                        "prev_hash": held["prev_hash"], "body_digest": held["body_digest"],
                        "kind": kind, "action_id": action_id, "duplicate": True,
                        "tenant_id": bound_tenant}
        head = await conn.fetchrow(
            "SELECT seq, hash FROM trust_ledger ORDER BY seq DESC LIMIT 1 "
            "FOR UPDATE")
        prev_hash = head["hash"] if head else GENESIS
        next_seq = int(await conn.fetchval(
            "SELECT nextval(pg_get_serial_sequence('trust_ledger', 'seq'))"))
        body = {"seq": next_seq, "user_id": user_id, "action_id": action_id,
                "task_id": task_id, "kind": kind, "record": payload,
                "prev_hash": prev_hash}
        # New records are chain v2 so they remain verifiable after a
        # redaction. See compute_hash_v2 for why that needed a second
        # construction rather than a change to the first.
        content_digest = body_digest(body)
        digest = compute_hash_v2(prev_hash, body)
        await conn.execute(
            "INSERT INTO trust_ledger (seq, user_id, action_id, task_id, kind, "
            "record, prev_hash, hash, body_digest, tenant_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
            next_seq, user_id, action_id, task_id, kind, json.dumps(payload),
            prev_hash, digest, content_digest, binding.tenant_id)
    logger.info("ledger append: seq=%s kind=%s action=%s tenant=%s",
                next_seq, kind, action_id, bound_tenant)
    _ledger_accepted_write()
    return {**body, "hash": digest, "body_digest": content_digest,
            "tenant_id": bound_tenant}


async def _held_journal_entry(conn: Any, *, action_id: str, kind: str,
                              journal_entry_id: str) -> Optional[Any]:
    """The row already holding this replayed journal entry, or None.

    Run on `append_record`'s connection, after its per-tenant advisory lock, so
    the check and the insert that follows it cannot be split by another append.
    `action_id` narrows it to the indexed column first.
    """
    return await conn.fetchrow(
        "SELECT seq, hash, prev_hash, body_digest FROM trust_ledger "
        "WHERE action_id = $1 AND kind = $2 "
        "AND record->>'journal_entry_id' = $3 ORDER BY seq ASC LIMIT 1",
        action_id, kind, journal_entry_id)


def _ledger_accepted_write() -> None:
    """The ledger takes writes: let the fallback journal replay its backlog (P1-09).

    After the commit, and never able to fail the append it follows: the record
    is already durable, and a replay problem is the journal's to report.
    """
    try:
        from trust.journal import ledger_accepted_write

        ledger_accepted_write()
    except Exception as exc:  # noqa: BLE001
        logger.error("ledger journal replay could not be scheduled: %s", exc)


async def redact_records(*, user_id: str, reason: str = "erasure request",
                         principal: Optional[Any] = None) -> int:
    """Erase the CONTENT of one user's ledger records, keeping the chain intact.

    Returns how many records were **actually changed**.

    What survives: `seq`, `at`, `user_id`, `action_id`, `kind`, `prev_hash`,
    `hash` and `body_digest`. So it remains provable that an action of a given
    kind happened at a given point in the chain, and that nothing was inserted,
    removed or reordered around it.

    What is erased: `record` — the payload, which is where `resource` and
    `purpose` live and therefore where personal data is.

    **v1 records are skipped, not silently blanked.** Their content is inside
    their hash, so erasing it would make the chain unverifiable from that point
    on. The caller is told the count so it can report honestly rather than
    claiming a complete erasure it did not perform.

    ── Why this is the worst place the unbound read could have been (P10-03) ──

    This ran on a bare `acquire()`. Under `trust_ledger`'s FORCE RLS the SELECT
    matched nothing, so it returned `0` and the UPDATE never ran — and `0` is
    reported to the Privacy Centre as **"nothing to erase"**. Somebody
    exercising a right to erasure was told their audit content had been dealt
    with, on a code path that had touched no row at all.

    Every other unbound read in this module returned a misleading emptiness.
    This one returned a misleading *completion*.

    `user_id` is TEXT — a username or an id string, not a tenant-unique key — so
    two tenants can hold the same value. The binding is what makes "this user's
    records" mean one tenant's, and `test_p10_redaction` drives exactly that
    collision.

    ── The count is the mutation, not a guess about it ────────────────────

    It used to `SELECT` matching rows, then `UPDATE` on a **wider** predicate
    (every row with a `body_digest`, including ones already tombstoned), then
    return the length of the SELECT. Two predicates and one number: the report
    described a query, not a change. `UPDATE … RETURNING seq` makes the returned
    count the set of rows the database actually modified.
    """
    binding = _binding_for(None, principal)
    from core.identity.db_context import tenant_connection

    async with tenant_connection(binding) as conn:
        # The append-only trigger permits exactly this transition; see
        # postgres/026_privacy.sql. RETURNING is what makes the count real.
        rows = await conn.fetch(
            "UPDATE trust_ledger SET record = $2::jsonb "
            "WHERE user_id = $1 AND body_digest IS NOT NULL "
            "AND record <> $2::jsonb "
            "RETURNING seq",
            user_id, json.dumps(REDACTION_TOMBSTONE),
        )
    logger.info("ledger redaction: user=%s tenant=%s records=%s reason=%s",
                user_id, binding.tenant_id, len(rows), reason)
    return len(rows)


async def unredactable_count(*, user_id: str,
                             principal: Optional[Any] = None) -> int:
    """How many of a user's records CANNOT be erased (chain v1).

    Exists so the Privacy Centre can state the residue plainly instead of
    implying a deletion was total. Fail-soft: **-1 means "could not
    determine"**, which the caller must not report as zero.

    P10-03 bound it. Unbound it returned `0` — *"no residue"* — which is the
    single most misleading answer this function can give: it is the number the
    Privacy Centre prints when it wants to tell somebody their erasure was
    complete. `-1` already existed for "could not determine"; the defect was
    that an unbound read never reached it, because RLS does not error, it just
    matches nothing.

    A missing tenant context raises rather than returning `-1`: `-1` says "the
    database could not tell me", and nobody having said whose records these are
    is not that.
    """
    binding = _binding_for(None, principal)
    from core.identity.db_context import tenant_connection

    try:
        async with tenant_connection(binding) as conn:
            return int(await conn.fetchval(
                "SELECT COUNT(*) FROM trust_ledger "
                "WHERE user_id = $1 AND body_digest IS NULL",
                user_id,
            ) or 0)
    except (LedgerNotBound, LedgerTenantMismatch, MissingTenantContext):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("unredactable_count failed: %s", exc)
        return -1


def _rows_as_records(rows) -> list[dict[str, Any]]:
    """asyncpg rows → records shaped the way `verify_chain` expects.

    ── A defect found by P10-02, at the surface that shows it ─────────────

    `record` is `jsonb`, and asyncpg hands it back as a **string** unless a
    codec says otherwise. `append_record` hashes a **dict**. So every reader
    that passed these rows to `verify_chain` was comparing a digest over
    `{"n": 1}` with a digest over `'{"n": 1}'`, and got:

        FAILED: body digest mismatch at seq=…

    on records that were perfectly intact. `/api/v1/trust/ledger` reported
    exactly that for any real row, and nothing caught it because the endpoint's
    tests supplied hand-built dicts — a fake in the shape the code wanted rather
    than the shape the database returns.

    Decoded here, once, rather than at each caller: three call sites decoding
    identically is three chances for one of them to stop.
    """
    out = []
    for row in rows:
        item = dict(row)
        value = item.get("record")
        if isinstance(value, (str, bytes)):
            try:
                item["record"] = json.loads(value)
            except (ValueError, TypeError):
                # Leave it alone rather than guess. A record that will not parse
                # is evidence of something, and replacing it with {} would erase
                # the evidence to make a verifier happy.
                pass
        out.append(item)
    return out


async def records_for(action_id: str, *,
                      principal: Optional[Any] = None) -> list[dict[str, Any]]:
    """All ledger records for one action, oldest first, for ONE tenant.

    ── Why this is not fail-soft in the way it looks (P10-01) ──────────────

    It still returns `[]` when the database is unreachable — a Proof Card is
    worth more without its records than not at all. What it will NOT do is
    return `[]` because nobody said whose records these are.

    That distinction is the whole batch. This function read through a bare
    `acquire()`, and `trust_ledger` carries FORCE RLS keyed on `app.tenant_id`,
    so an unbound read matched no rows — and `[]` is a *plausible, benign*
    answer. The write defect P9-02 fixed at least had a refusal underneath it;
    an unbound read has nothing. Emptiness and "nothing happened" are the same
    string.

    So `LedgerNotBound` propagates rather than being flattened into `[]`. A
    caller that cannot tell "no records" from "we never asked properly" will
    show an empty audit trail and call it clean.
    """
    binding = _binding_for(None, principal)
    from core.identity.db_context import tenant_connection

    try:
        async with tenant_connection(binding) as conn:
            rows = await conn.fetch(
                "SELECT seq, at, user_id, action_id, task_id, kind, record, "
                "prev_hash, hash, body_digest FROM trust_ledger WHERE action_id = $1 "
                "ORDER BY seq ASC", action_id)
        return _rows_as_records(rows)
    except (LedgerNotBound, LedgerTenantMismatch, MissingTenantContext):
        # Load-bearing, and it took a plant to prove it. `_binding_for` runs
        # OUTSIDE this try, so removing this clause changed nothing and no test
        # noticed — the guard was decorative.
        #
        # What it actually guards is the case that IS inside the try:
        # `tenant_connection` raises `MissingTenantContext` when the principal
        # carries no tenant_id, and without this that refusal would be caught by
        # the fail-soft handler below and returned as `[]`. A principal that
        # cannot name a tenant would produce a clean, empty audit trail — the
        # same defect this batch exists to close, arriving through a different
        # door.
        raise
    except Exception as exc:
        logger.error("records_for failed: %s", exc)
        return []


async def recent_records(*, limit: int = 50,
                         principal: Optional[Any] = None) -> list[dict[str, Any]]:
    """Newest ledger records ACROSS ACTIONS, for one tenant, bounded.

    "Across actions" no longer means "across the deployment". The policy scopes
    the read to the bound tenant, which is the correct answer for an operator
    surface in a multi-tenant process and a *changed* answer for a single-tenant
    one — there, the bound tenant is the only tenant, so the result is the same
    rows it always was.

    Same refusal discipline as `records_for`: a database that is down yields
    `[]`; a missing tenant context raises.
    """
    limit = max(1, min(int(limit), 250))
    binding = _binding_for(None, principal)
    from core.identity.db_context import tenant_connection

    try:
        async with tenant_connection(binding) as conn:
            rows = await conn.fetch(
                "SELECT seq, at, user_id, action_id, task_id, kind, record, "
                "prev_hash, hash, body_digest FROM trust_ledger ORDER BY seq DESC LIMIT $1",
                limit,
            )
        return _rows_as_records(rows)
    except (LedgerNotBound, LedgerTenantMismatch, MissingTenantContext):
        raise
    except Exception as exc:
        logger.error("recent_records failed: %s", exc)
        return []


async def export_head(*, principal: Optional[Any] = None) -> Optional[dict[str, Any]]:
    """This TENANT's chain head, for external anchoring. Fail-soft: None.

    ── The head is per tenant now, and that is a change of meaning ─────────

    Since P9-02 `prev_hash` links within a tenant — the head read is
    RLS-filtered — so there is no such thing as "the" chain head any more.
    Returning a global one would hand a tenant an anchor that verifies nothing
    they can see.

    **An anchor taken before that is not comparable to one taken after**, and
    this function cannot tell the difference. Deciding what to do about stored
    anchors is P10-04's, and it is named here rather than left for whoever first
    finds an anchor that no longer matches. `tenant_id` is returned alongside so
    an anchor records what it is an anchor OF.

    `{"seq": 0, "hash": GENESIS}` still means "this tenant has no records yet",
    which is a real answer. `None` still means the database could not be
    reached. Neither is reachable without a tenant: that raises.
    """
    binding = _binding_for(None, principal)
    from core.identity.db_context import tenant_connection

    try:
        async with tenant_connection(binding) as conn:
            row = await conn.fetchrow(
                "SELECT seq, at, hash FROM trust_ledger ORDER BY seq DESC LIMIT 1")
        head = dict(row) if row else {"seq": 0, "hash": GENESIS}
        head["tenant_id"] = str(binding.tenant_id)
        # P10-04: every anchor issued from here says which chain model it
        # anchors. An anchor that does not carry its scope is, by
        # `anchor_scope`, a legacy one — so stamping this is what stops a
        # freshly-taken tenant anchor being mistaken for a pre-cutover global
        # one the first time somebody tries to verify with it.
        head["scope"] = ANCHOR_SCOPE_TENANT
        return head
    except (LedgerNotBound, LedgerTenantMismatch, MissingTenantContext):
        raise
    except Exception as exc:
        logger.error("export_head failed: %s", exc)
        return None


__all__ = ["GENESIS", "LEDGER_KINDS", "canonical", "compute_hash",
           "verify_chain", "append_record", "records_for", "recent_records",
           "export_head", "LedgerNotBound", "LedgerTenantMismatch",
           "ANCHOR_VERIFIED", "ANCHOR_MISMATCH", "ANCHOR_LEGACY_GLOBAL",
           "ANCHOR_WRONG_TENANT", "ANCHOR_SCOPE_TENANT", "ANCHOR_SCOPE_LEGACY",
           "anchor_scope", "verify_against_anchor", "chain_eras"]
