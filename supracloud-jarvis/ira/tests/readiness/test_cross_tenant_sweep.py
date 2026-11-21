"""Phase 9 — cross-tenant isolation, swept rather than sampled.

Seven suites already prove tenant isolation for the tables their phase added
(`tests/tenancy/test_*_isolation_rls.py`). Each one names its tables in a
constant. That is the right shape for a phase, and the wrong shape for a launch
gate, because it can only be wrong in one direction: a table nobody listed is a
table nobody tested. Migration 030 could add a tenant-scoped table with no
policy on it and every existing suite would still pass.

So this suite asks the database what exists instead of being told:

    every table with a `tenant_id` column must ENABLE **and** FORCE row-level
    security and carry at least one policy

and then, separately and more awkwardly:

    every table with a `user_id` column but **no** `tenant_id` is a table whose
    isolation has no database-level backstop at all, and the set of those is
    pinned to a list that may only shrink.

── The second one was a finding. It is now closed ───────────────────────────

Five tables predated Phase 2 and were not part of its retrofit:

    cognitive_events   commitments   envelope_members   files   lifeos_events

They held user data keyed by `user_id TEXT`, with no `tenant_id` and no RLS.
Their queries did filter — `WHERE user_id = $1` was present at each call site —
and that was precisely the problem. Application-level filtering is the
dependency Phase 2 removed for the other 34 tables, on the grounds that one
forgotten WHERE clause is a cross-tenant leak with nothing underneath it.

`031_pretenancy_isolation.sql` gives all of them `tenant_id`, backfills to the
canonical private tenant, and enables *and forces* RLS with the same policy every
other scoped table uses. `KNOWN_UNPROTECTED` is now empty and stays that way: the
sweep below fails on any table that has an owner column and no `tenant_id`, so
the hole cannot reopen quietly.

── The sixth table this sweep missed, and the reason it did ─────────────────

`privacy_envelopes` has exactly the same defect and was **not** in the original
five, because this sweep looked for a column literally named `user_id` and that
table calls its owner `owner_id`. A sweep whose entire purpose is finding
unprotected owner data missed one by spelling.

`OWNER_COLUMNS` below is the repair: the search now covers every owner-column
name in the schema rather than the one that happened to be checked. The table is
fixed in the same change — but the wider sweep is the more valuable half, because
the next table to get this wrong will not be one anybody is currently looking at.
"""
from __future__ import annotations

import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="IRA_TEST_DATABASE_DSN not set — real-database sweep skipped"
)

#: Tables holding user data with no `tenant_id` and therefore no RLS backstop.
#: See the module docstring. This list may shrink and may never grow.
KNOWN_UNPROTECTED: frozenset[str] = frozenset()

#: Every column name this schema uses to mean "the person this row belongs to".
#: A table with any of these and no `tenant_id` is unprotected, regardless of
#: which one it picked — the omission that let `privacy_envelopes` sit outside
#: the original finding for a whole phase.
#:
#: Derived from the schema rather than guessed: these are the owner-shaped TEXT
#: and UUID columns present across the migrations. Adding a seventh spelling
#: means adding it here, which is a line a reviewer sees.
OWNER_COLUMNS: tuple[str, ...] = ("user_id", "owner_id", "subject_user_id")

#: Session settings that a policy may key on INSTEAD of `app.user_id`, for rows
#: whose legitimate reader cannot supply a user id.
#:
#: There are two, and each had to earn it. `app.verification_token_hash` exists
#: because email verification is performed by an UNAUTHENTICATED caller
#: presenting a token: the user is unknown until the token has been looked up,
#: so a policy on `app.user_id` cannot be satisfied by the only path that
#: legitimately reads `email_verification_tokens`.
#:
#: Batch 9 added `app.reset_token_hash` on the same grounds, and the grounds are
#: even plainer there: the legitimate reader of a password-reset token cannot
#: sign in, which is the entire reason they are holding one. Demanding
#: `app.user_id` would make the row unreadable to exactly the party entitled to
#: use it.
#:
#: The bar is NOT "a user predicate would be inconvenient". It is that the only
#: legitimate reader cannot supply one.
#:
#: A policy keyed this way is not weaker than the user-keyed one it replaces —
#: it is stronger. The connection may see exactly the row whose secret it has
#: already presented, so a query that forgot its WHERE clause still enumerates
#: nothing, and an unbound connection sees none at all.
POSSESSION_SETTINGS: tuple[str, ...] = (
    "app.verification_token_hash", "app.reset_token_hash",
)

#: Sanity floor. If the sweep finds fewer tenant-scoped tables than the phases
#: are known to have added, the query is wrong and every assertion below is
#: vacuous — the same trap as running an isolation suite against a superuser.
MINIMUM_SCOPED_TABLES = 30


@pytest.fixture
def db():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    yield conn
    conn.close()


def _columns_exist_clause(column: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM information_schema.columns col "
        "        WHERE col.table_schema = 'public' "
        f"          AND col.table_name = c.relname AND col.column_name = '{column}')"
    )


def _tables_with(db, *, having: str, lacking: str | None = None) -> set[str]:
    """Ordinary tables in `public` with one column and optionally without another."""
    where = [f"n.nspname = 'public'", "c.relkind = 'r'", _columns_exist_clause(having)]
    if lacking:
        where.append("NOT " + _columns_exist_clause(lacking))
    cur = db.cursor()
    cur.execute(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE " + " AND ".join(where)
    )
    return {row[0] for row in cur.fetchall()}


def test_app_role_is_not_superuser(db):
    """A superuser bypasses RLS, which would make this whole sweep vacuous."""
    cur = db.cursor()
    cur.execute("SELECT usesuper FROM pg_user WHERE usename = current_user")
    row = cur.fetchone()
    assert row is not None and row[0] is False, (
        "the sweep is connected as a superuser; RLS does not apply to superusers, "
        "so every isolation assertion below would pass while protecting nothing"
    )


def test_the_sweep_actually_found_the_schema(db):
    """Guards against a query that silently matches nothing."""
    scoped = _tables_with(db, having="tenant_id")
    assert len(scoped) >= MINIMUM_SCOPED_TABLES, (
        f"the sweep found only {len(scoped)} tenant-scoped tables "
        f"({sorted(scoped)}), fewer than the {MINIMUM_SCOPED_TABLES} the "
        "migrations are known to create. The introspection query is wrong, or "
        "the migration chain did not fully apply — either way the assertions in "
        "this suite are not testing what they claim to."
    )


def test_every_tenant_scoped_table_enables_and_forces_rls(db):
    """The drift guard. A new tenant-scoped table without a policy fails here."""
    cur = db.cursor()
    cur.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
        "       (SELECT count(*) FROM pg_policies p WHERE p.tablename = c.relname) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' AND "
        + _columns_exist_clause("tenant_id") +
        " ORDER BY c.relname"
    )
    rows = cur.fetchall()
    assert rows, "no tenant-scoped tables found at all"

    offenders = []
    for name, enabled, forced, policies in rows:
        problems = []
        if not enabled:
            problems.append("RLS not enabled")
        if not forced:
            # The one that looks fine and protects nothing: without FORCE, the
            # table owner — usually the application role — bypasses the policy.
            problems.append("RLS not FORCED (the owner bypasses the policy)")
        if not policies:
            problems.append("no policy")
        if problems:
            offenders.append(f"{name}: {', '.join(problems)}")

    assert not offenders, (
        "tenant-scoped table(s) without complete row-level security:\n  "
        + "\n  ".join(offenders)
        + "\n\nA table with a tenant_id column holds data belonging to a "
          "specific tenant. Follow the pattern in 023_tenancy.sql: ENABLE, "
          "FORCE, and a tenant_isolation policy using "
          "NULLIF(current_setting('app.tenant_id', true), '')::uuid."
    )


def _user_scoped_tables(db) -> set[str]:
    """Tables isolated by a FORCED RLS policy keyed on the OWNER, not the tenant.

    Phase 3B introduced a second isolation model. A `USER_PRIVATE` table —
    `totp_secrets`, `voice_profiles` — carries no `tenant_id` on purpose: its
    policy predicates on `app.user_id`, which is **stricter** than a tenant
    predicate, because it denies a tenant administrator as well as another
    tenant. A second factor that every admin in the tenant could read would be
    the same bug one level up.

    The original sweep knew only about `tenant_id`, so it flagged those tables
    as unprotected. That is the same shape of gap the `owner_id` widening fixed:
    a sweep that recognises one spelling of protection misses the others. This
    recognises a forced policy whose expression references an owner column.
    """
    cur = db.cursor()
    cur.execute(
        """
        SELECT c.relname
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_policies  p ON p.tablename = c.relname AND p.schemaname = 'public'
         WHERE n.nspname = 'public'
           AND c.relkind = 'r'
           AND c.relrowsecurity
           AND c.relforcerowsecurity
           AND (p.qual LIKE %s OR p.with_check LIKE %s)
        """,
        ("%app.user_id%", "%app.user_id%"),
    )
    return {row[0] for row in cur.fetchall()}


def _deny_all_tables(db) -> set[str]:
    """Tables with RLS enabled and NO policy — readable by nobody but the owner.

    `legacy_identity_map` is the case: migration compatibility data, no API
    exposes it, no authorisation decision reads it. With RLS on and no
    permissive policy, every row fails the default-deny, so the application role
    reads zero rows.

    This deliberately does NOT accept `REVOKE`-based protection, which is what
    034 originally used. Every documented provisioning step for this schema ends
    with ``GRANT ALL ON ALL TABLES IN SCHEMA public TO ira_app``, which runs
    after the migrations and hands the grant straight back — so a REVOKE in a
    migration is undone by the standard setup procedure and protects nothing.
    Grants and RLS are independent gates; only the RLS one survives a later
    blanket grant, so only that one is recognised here.
    """
    cur = db.cursor()
    cur.execute(
        """
        SELECT c.relname
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind = 'r'
           AND c.relrowsecurity
           AND NOT EXISTS (
               SELECT 1 FROM pg_policies p
                WHERE p.schemaname = 'public' AND p.tablename = c.relname
           )
        """
    )
    return {row[0] for row in cur.fetchall()}


def _unprotected_owner_tables(db) -> set[str]:
    """Tables with ANY owner column and no isolation of any recognised kind.

    Three kinds are recognised, because the schema now uses three:

      * `tenant_id` plus a tenant policy — the Phase 2/3 model;
      * a FORCED policy on `app.user_id` — the Phase 3B `USER_PRIVATE` model,
        which is stricter;
      * RLS enabled with no policy at all — deny-all, stricter still.

    The union across `OWNER_COLUMNS`, not just `user_id`. Checking one spelling
    is how `privacy_envelopes` sat outside blocker B1 for a whole phase while
    having exactly the defect B1 described.
    """
    found: set[str] = set()
    for column in OWNER_COLUMNS:
        found |= _tables_with(db, having=column, lacking="tenant_id")
    return (found - _user_scoped_tables(db) - _deny_all_tables(db)
            - _possession_scoped_tables(db))


def _possession_scoped_tables(db) -> set[str]:
    """The fourth kind: FORCED RLS where EVERY policy keys on a possession
    setting from `POSSESSION_SETTINGS`.

    Every policy, not any — a table with one token-keyed policy and one
    `USING (true)` beside it is not isolated, and permissive policies combine
    with OR so the loose one would decide the outcome.
    """
    if not POSSESSION_SETTINGS:
        return set()
    predicate = " OR ".join(
        f"expr LIKE '%{setting}%'" for setting in POSSESSION_SETTINGS
    )
    with db.cursor() as cur:
        cur.execute(f"""
            SELECT c.relname
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public'
               AND c.relkind = 'r'
               AND c.relforcerowsecurity
               AND EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid)
               AND NOT EXISTS (
                     SELECT 1 FROM pg_policy p,
                          LATERAL (SELECT coalesce(pg_get_expr(p.polqual, p.polrelid), '')
                                       || coalesce(pg_get_expr(p.polwithcheck, p.polrelid), '')
                                     AS expr) e
                      WHERE p.polrelid = c.oid AND NOT ({predicate})
                   )
        """)
        return {r[0] for r in cur.fetchall()}


def test_the_owner_column_sweep_is_not_vacuous(db):
    """`OWNER_COLUMNS` must name columns this schema actually uses.

    A typo — `owner_uid` for `owner_id` — would make the widened sweep search
    for nothing and pass, which is the failure mode the widening was meant to
    remove. So each name is required to exist somewhere in the schema.
    """
    cur = db.cursor()
    cur.execute(
        "SELECT DISTINCT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name = ANY(%s)",
        (list(OWNER_COLUMNS),),
    )
    present = {row[0] for row in cur.fetchall()}
    missing = sorted(set(OWNER_COLUMNS) - present)
    assert not missing, (
        f"OWNER_COLUMNS names column(s) no table has: {missing}. A sweep that "
        f"searches for a column that does not exist finds nothing and passes."
    )


def test_no_table_holds_owner_data_without_tenant_isolation(db):
    """**Blocker B1, now closed and held closed.**

    A table holding an owner column without `tenant_id` is a place where
    cross-tenant isolation depends entirely on somebody remembering a WHERE
    clause. Six such tables predated Phase 2; `031_pretenancy_isolation.sql`
    retrofitted all six, and `KNOWN_UNPROTECTED` is now empty.

    This is the assertion that stops it reopening. It searches every owner
    column spelling, so the next table to get this wrong is caught even if it
    invents a new name for its owner — provided that name is in `OWNER_COLUMNS`,
    which the test above requires to be real.
    """
    unprotected = _unprotected_owner_tables(db)
    new = unprotected - KNOWN_UNPROTECTED
    assert not new, (
        f"table(s) holding owner data with no tenant_id and no RLS: {sorted(new)}.\n"
        "Isolation for such a table rests entirely on every query remembering "
        "WHERE user_id = ..., with no database-level backstop if one forgets. "
        "Either add tenant_id and a policy (see 031_pretenancy_isolation.sql for "
        "the retrofit pattern, including the backfill and the application-side "
        "`owner_connection` change that stops the table silently returning "
        "nothing), or — if the table genuinely holds no per-owner data — drop "
        "the owner column.\n"
        "Adding it to KNOWN_UNPROTECTED is not the fix. That list is empty and "
        "is meant to stay empty; it recorded a debt Phase 2 left behind, and "
        "that debt is paid."
    )


def test_the_debt_list_is_empty_and_the_sweep_can_still_find_things(db):
    """Two things at once, because an empty list makes the first one cheap.

    `KNOWN_UNPROTECTED` being empty means `test_no_table_holds_owner_data...`
    would also pass if `_unprotected_owner_tables` silently returned nothing —
    a broken sweep and a clean schema look identical. So this asserts the sweep
    still finds tables when it is asked a question with a known answer.
    """
    assert KNOWN_UNPROTECTED == frozenset(), (
        f"KNOWN_UNPROTECTED is no longer empty: {sorted(KNOWN_UNPROTECTED)}. "
        f"B1 was closed by fixing the tables, not by listing them."
    )

    # A question with a known answer: tables having `user_id` at all. The six
    # retrofitted tables kept their owner column alongside the new tenant_id,
    # so this must be non-empty or the introspection is broken.
    with_owner = set()
    for column in OWNER_COLUMNS:
        with_owner |= _tables_with(db, having=column)
    assert len(with_owner) >= 10, (
        f"the owner-column sweep found only {len(with_owner)} tables "
        f"({sorted(with_owner)}); the introspection is wrong, and the "
        f"'no unprotected tables' result above therefore proves nothing"
    )


def test_unset_tenant_context_sees_nothing_anywhere(db):
    """The property the whole scheme rests on, checked across every table.

    Phase 2 proves this for its own tables. Sweeping it matters because the
    failure mode is per-table: a policy written with `current_setting(...)`
    lacking the NULLIF guard raises on a *cleared* context instead of returning
    nothing, and an application that catches the error and retries would then be
    running unscoped.
    """
    cur = db.cursor()
    cur.execute("SELECT set_config('app.tenant_id', '', false)")

    scoped = sorted(_tables_with(db, having="tenant_id"))
    leaked, raised = [], []
    for table in scoped:
        try:
            cur.execute(f'SELECT count(*) FROM "{table}"')  # noqa: S608 - introspected
            if cur.fetchone()[0] != 0:
                leaked.append(table)
        except psycopg2.Error as exc:
            raised.append(f"{table}: {type(exc).__name__}")
            db.rollback()

    assert not leaked, (
        f"table(s) returned rows with no tenant context set: {leaked}. "
        "An unset or cleared app.tenant_id must match no rows."
    )
    assert not raised, (
        f"table(s) raised rather than returning nothing on a cleared tenant "
        f"context: {raised}. Use "
        "NULLIF(current_setting('app.tenant_id', true), '')::uuid — a bare "
        "cast of the empty string raises 'invalid input syntax for type uuid', "
        "which application code may catch and treat as a transient fault."
    )
