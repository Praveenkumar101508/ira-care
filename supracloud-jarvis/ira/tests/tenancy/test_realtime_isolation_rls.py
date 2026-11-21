"""Phase 6 — cross-tenant isolation and the no-content guarantee, against a
real database.

Two things are asserted here that cannot be asserted anywhere else:

  * **Isolation.** `realtime_sessions` records which external provider carried
    a user's live microphone. A cross-tenant read here answers "who else has
    heard this person?" for the wrong person.

  * **The no-content guarantee, enforced rather than promised.** Every test in
    tests/realtime/test_telemetry_refuses_content.py exercises the APPLICATION
    guard, which a direct database write goes around. These tests attack the
    schema itself: they try to store a transcript by four different routes and
    require the database to refuse each one.

Running them: see the header of test_tenant_isolation_rls.py. The chain must
include 027_realtime.sql.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN, reason="IRA_TEST_DATABASE_DSN not set — real-database RLS tests skipped"
)

TENANT_A = uuid.UUID("f2f2f2f2-0000-4000-8000-00000000000a")
TENANT_B = uuid.UUID("f3f3f3f3-0000-4000-8000-00000000000b")

REALTIME_SCOPED = ("realtime_sessions", "realtime_events")

TRANSCRIPT = "I told IRA about my divorce"


@pytest.fixture
def db():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    yield conn
    conn.close()


def _as_tenant(conn, tenant: uuid.UUID):
    cur = conn.cursor()
    cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))
    return cur


def _clear_tenant(conn):
    cur = conn.cursor()
    cur.execute("SELECT set_config('app.tenant_id', '', false)")
    return cur


@pytest.fixture
def seeded(db):
    """One live session per tenant, each with one event."""
    run = uuid.uuid4().hex[:12]
    cur = db.cursor()
    cur.execute(
        "INSERT INTO tenants (id, name, slug, edition) VALUES "
        "(%s,'RT A',%s,'cloud'), (%s,'RT B',%s,'cloud') "
        "ON CONFLICT (id) DO NOTHING",
        (str(TENANT_A), f"rt-a-{run}", str(TENANT_B), f"rt-b-{run}"),
    )

    ids: dict[str, dict] = {}
    for key, tenant in (("a", TENANT_A), ("b", TENANT_B)):
        user_id = uuid.uuid4()
        cur.execute(
            "INSERT INTO users (id, external_subject) VALUES (%s,%s)",
            (str(user_id), f"rt-{key}-{run}"),
        )
        cur = _as_tenant(db, tenant)
        cur.execute(
            "INSERT INTO realtime_sessions "
            "(tenant_id, user_id, provider, provider_region, state, "
            " opened_level, current_level) "
            "VALUES (%s,%s,'livekit','eu','live','full_avatar','full_avatar') "
            "RETURNING id",
            (str(tenant), str(user_id)),
        )
        session_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO realtime_events (tenant_id, session_id, kind, level, "
            "at_ms, metrics) VALUES (%s,%s,'first_audio','full_avatar',%s,%s) "
            "RETURNING id",
            (str(tenant), str(session_id), 420, json.dumps({"first_audio_ms": 420})),
        )
        ids[key] = {
            "tenant": tenant,
            "user_id": user_id,
            "session_id": session_id,
            "event_id": cur.fetchone()[0],
        }
    _clear_tenant(db)
    yield ids

    cur = db.cursor()
    for entry in ids.values():
        cur.execute("SELECT set_config('app.tenant_id', %s, false)",
                    (str(entry["tenant"]),))
        cur.execute("DELETE FROM realtime_events WHERE session_id = %s",
                    (str(entry["session_id"]),))
        cur.execute("DELETE FROM realtime_sessions WHERE id = %s",
                    (str(entry["session_id"]),))
    _clear_tenant(db)


# ── The harness must be honest before it claims anything ─────────────────────


def test_app_role_is_not_superuser(db):
    """A superuser bypasses RLS entirely, making every test below vacuous.

    Not a formality. The first run of this suite was pointed at a superuser
    DSN and six isolation tests failed — which was the harness being wrong, not
    the schema. Without this guard the same mistake in the other direction
    (everything passing) would have looked like proof.
    """
    cur = db.cursor()
    cur.execute("SELECT usesuper FROM pg_user WHERE usename = current_user")
    row = cur.fetchone()
    assert row is not None, "current_user is not in pg_user"
    assert row[0] is False, (
        "IRA_TEST_DATABASE_DSN points at a SUPERUSER. Superusers bypass "
        "row-level security, so every isolation assertion in this file would "
        "pass while protecting nothing. Point it at the non-superuser "
        "application role (see the header of test_tenant_isolation_rls.py)."
    )


def test_the_app_role_does_not_own_the_realtime_tables(db):
    """The other bypass. A table's owner ignores its policies unless FORCE is
    set — and FORCE is asserted separately below, so this keeps the two
    protections from being tested by the same fact."""
    cur = db.cursor()
    cur.execute(
        "SELECT tablename, tableowner FROM pg_tables "
        "WHERE tablename = ANY(%s) AND tableowner = current_user",
        (list(REALTIME_SCOPED),),
    )
    owned = cur.fetchall()
    assert owned == [], (
        f"the test role owns {owned} — owner bypass would mask a missing policy"
    )


# ── Isolation ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("table", REALTIME_SCOPED)
def test_row_level_security_is_enabled_and_forced(db, table):
    """FORCE is what stops the table owner bypassing the policy. Without it
    these tests pass while protecting nothing."""
    cur = db.cursor()
    cur.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relname = %s", (table,),
    )
    enabled, forced = cur.fetchone()
    assert enabled, f"{table} does not have RLS enabled"
    assert forced, f"{table} does not FORCE RLS — the owner bypasses the policy"


def test_a_tenant_cannot_see_another_tenants_sessions(seeded, db):
    """The provider-routing record is the answer to "who else has heard me?".
    Reading it across tenants answers it for the wrong person."""
    cur = _as_tenant(db, TENANT_B)
    cur.execute("SELECT count(*) FROM realtime_sessions WHERE id = %s",
                (str(seeded["a"]["session_id"]),))
    assert cur.fetchone()[0] == 0

    cur = _as_tenant(db, TENANT_A)
    cur.execute("SELECT count(*) FROM realtime_sessions WHERE id = %s",
                (str(seeded["a"]["session_id"]),))
    assert cur.fetchone()[0] == 1, "tenant A cannot see its own session either"


def test_a_tenant_cannot_see_another_tenants_events(seeded, db):
    cur = _as_tenant(db, TENANT_B)
    cur.execute("SELECT count(*) FROM realtime_events WHERE id = %s",
                (str(seeded["a"]["event_id"]),))
    assert cur.fetchone()[0] == 0


def test_no_tenant_context_sees_nothing(seeded, db):
    """A dropped `SET LOCAL` must deny, not expose everything. NULLIF keeps a
    cleared context denying cleanly instead of raising a uuid cast error."""
    cur = _clear_tenant(db)
    for table in REALTIME_SCOPED:
        cur.execute(f"SELECT count(*) FROM {table}")
        assert cur.fetchone()[0] == 0, f"{table} is readable with no tenant context"


def test_a_tenant_cannot_write_a_session_into_another_tenant(seeded, db):
    """WITH CHECK. Without it, isolation stops reads and permits writes."""
    cur = _as_tenant(db, TENANT_B)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO realtime_sessions (tenant_id, state) VALUES (%s,'requested')",
            (str(TENANT_A),),
        )


def test_a_tenant_cannot_attach_an_event_to_another_tenants_session(seeded, db):
    cur = _as_tenant(db, TENANT_B)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO realtime_events (tenant_id, session_id, kind, at_ms) "
            "VALUES (%s,%s,'connected',10)",
            (str(TENANT_A), str(seeded["a"]["session_id"])),
        )


def test_a_tenant_cannot_delete_another_tenants_session(seeded, db):
    cur = _as_tenant(db, TENANT_B)
    cur.execute("DELETE FROM realtime_sessions WHERE id = %s",
                (str(seeded["a"]["session_id"]),))
    assert cur.rowcount == 0, "a cross-tenant delete removed rows"

    cur = _as_tenant(db, TENANT_A)
    cur.execute("SELECT count(*) FROM realtime_sessions WHERE id = %s",
                (str(seeded["a"]["session_id"]),))
    assert cur.fetchone()[0] == 1, "the session was deleted by the other tenant"


# ── The no-content guarantee, against the database itself ────────────────────


def test_a_transcript_cannot_be_stored_under_an_obvious_key(seeded, db):
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO realtime_events (tenant_id, session_id, kind, at_ms, metrics) "
            "VALUES (%s,%s,'first_audio',100,%s)",
            (str(TENANT_A), str(seeded["a"]["session_id"]),
             json.dumps({"transcript": TRANSCRIPT})),
        )


def test_a_transcript_cannot_be_stored_under_an_innocent_key(seeded, db):
    """Renaming the key must not help. The guard restricts VALUE TYPES, not
    just key names — which is the property that actually holds."""
    cur = _as_tenant(db, TENANT_A)
    for key in ("note", "text", "detail", "x"):
        with pytest.raises(psycopg2.Error), db.cursor() as attempt:
            attempt.execute("SELECT set_config('app.tenant_id', %s, false)",
                            (str(TENANT_A),))
            attempt.execute(
                "INSERT INTO realtime_events (tenant_id, session_id, kind, at_ms, metrics) "
                "VALUES (%s,%s,'first_audio',100,%s)",
                (str(TENANT_A), str(seeded["a"]["session_id"]),
                 json.dumps({key: TRANSCRIPT})),
            )


def test_a_transcript_cannot_be_stored_inside_a_permitted_key(seeded, db):
    """`rtt_ms` is allow-listed. A string in it is still refused."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO realtime_events (tenant_id, session_id, kind, at_ms, metrics) "
            "VALUES (%s,%s,'first_audio',100,%s)",
            (str(TENANT_A), str(seeded["a"]["session_id"]),
             json.dumps({"rtt_ms": f"48; {TRANSCRIPT}"})),
        )


def test_a_transcript_cannot_be_nested_inside_a_permitted_key(seeded, db):
    """A nested object is not a number either. Without this, `{"rtt_ms":
    {"said": "..."}}` would pass a naive key check."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO realtime_events (tenant_id, session_id, kind, at_ms, metrics) "
            "VALUES (%s,%s,'first_audio',100,%s)",
            (str(TENANT_A), str(seeded["a"]["session_id"]),
             json.dumps({"rtt_ms": {"said": TRANSCRIPT}})),
        )


def test_a_real_measurement_is_accepted(seeded, db):
    """Confirms the four refusals above are refusing content and not simply
    refusing everything."""
    cur = _as_tenant(db, TENANT_A)
    cur.execute(
        "INSERT INTO realtime_events (tenant_id, session_id, kind, at_ms, metrics) "
        "VALUES (%s,%s,'first_audio',420,%s) RETURNING id",
        (str(TENANT_A), str(seeded["a"]["session_id"]),
         json.dumps({"first_audio_ms": 420, "budget_ms": 1200, "within_budget": True})),
    )
    assert cur.fetchone()[0] is not None


def test_the_disclosure_columns_cannot_be_written(seeded, db):
    """`audio_stored` is GENERATED ALWAYS AS (FALSE). The Privacy Centre reads
    this value; a schema that let it be set to TRUE would turn a guarantee into
    a field someone remembered to fill in."""
    cur = _as_tenant(db, TENANT_A)
    for column in ("audio_stored", "transcript_stored"):
        with pytest.raises(psycopg2.Error), db.cursor() as attempt:
            attempt.execute("SELECT set_config('app.tenant_id', %s, false)",
                            (str(TENANT_A),))
            attempt.execute(
                f"UPDATE realtime_sessions SET {column} = TRUE WHERE id = %s",
                (str(seeded["a"]["session_id"]),),
            )


def test_the_disclosure_columns_read_false(seeded, db):
    cur = _as_tenant(db, TENANT_A)
    cur.execute(
        "SELECT audio_stored, transcript_stored FROM realtime_sessions WHERE id = %s",
        (str(seeded["a"]["session_id"]),),
    )
    assert cur.fetchone() == (False, False)


def test_no_realtime_table_has_a_column_that_could_hold_audio(db):
    """Structural, checked against the live catalogue rather than the file.

    A later migration could add a BYTEA or a bare TEXT column without touching
    027_realtime.sql, and the file-based scan in
    tests/realtime/test_ladder_matches_migration.py would not see it.
    """
    cur = db.cursor()
    cur.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_name LIKE 'realtime%%' AND data_type IN "
        "('bytea','json','xml')"
    )
    assert cur.fetchall() == [], "a column that could hold audio appeared"


# ── Measurements are not revisable, but are erasable ─────────────────────────


def test_an_event_cannot_be_edited(seeded, db):
    """A latency figure changed after the fact is worse than no figure."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute("UPDATE realtime_events SET at_ms = 1 WHERE id = %s",
                    (str(seeded["a"]["event_id"]),))
    assert "measurement log" in str(exc.value)


def test_an_event_can_be_deleted_so_erasure_can_reach_it(seeded, db):
    """The other half. A telemetry row out of erasure's reach would make
    Phase 5's deletion promise false for anyone who used a live session."""
    cur = _as_tenant(db, TENANT_A)
    cur.execute("DELETE FROM realtime_events WHERE id = %s",
                (str(seeded["a"]["event_id"]),))
    assert cur.rowcount == 1


def test_deleting_a_session_takes_its_events_with_it(seeded, db):
    """ON DELETE CASCADE. Orphaned telemetry rows would survive an erasure that
    reported success."""
    cur = _as_tenant(db, TENANT_A)
    cur.execute("DELETE FROM realtime_sessions WHERE id = %s",
                (str(seeded["a"]["session_id"]),))
    cur.execute("SELECT count(*) FROM realtime_events WHERE session_id = %s",
                (str(seeded["a"]["session_id"]),))
    assert cur.fetchone()[0] == 0


def test_erasure_reaches_the_realtime_tables():
    """The scope table in core/privacy/erasure.py must name them.

    Phase 6 adds user-linked rows; Phase 5 promises they can be deleted. A
    table nobody added to the scope is not covered by that promise, and the
    omission is invisible — the erasure reports success either way.

    Reads `SUBJECT_TABLES` rather than grepping the source of
    `erase_postgres`. The old form searched the function body for the strings
    `"voice"`, `"realtime_sessions"` and so on, which passes on a COMMENT
    mentioning them — and P12-05 moved the scope table out of that function
    anyway, leaving the grep looking at the wrong text.
    """
    from core.privacy.erasure import SUBJECT_TABLES

    for scope in ("voice", "everything", "account"):
        assert scope in SUBJECT_TABLES, f"scope {scope!r} no longer exists"
        for table in ("realtime_sessions", "realtime_events"):
            assert table in SUBJECT_TABLES[scope], (
                f"erasure scope {scope!r} does not reach {table} — a user who "
                f"used a live session cannot have their provider-routing "
                f"history deleted"
            )


# ── Coherence ────────────────────────────────────────────────────────────────


def test_a_terminal_session_must_give_a_reason(seeded, db):
    """Without this, a session can end with no explanation and the UI has
    nothing to show for it."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO realtime_sessions (tenant_id, state) VALUES (%s,'ended')",
            (str(TENANT_A),),
        )


def test_audio_cannot_be_reported_without_a_connection(seeded, db):
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO realtime_sessions (tenant_id, first_audio_ms) VALUES (%s,300)",
            (str(TENANT_A),),
        )


def test_an_unknown_provider_cannot_be_recorded(seeded, db):
    """The provider name is a disclosure. A free-form one would let an
    unreviewed vendor appear in the record as though it had been chosen."""
    cur = _as_tenant(db, TENANT_A)
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO realtime_sessions (tenant_id, provider) VALUES (%s,'mystery-sfu')",
            (str(TENANT_A),),
        )


def test_the_capability_ladder_is_readable_by_every_tenant(seeded, db):
    """Global reference data, deliberately without RLS: it is a fixed product
    vocabulary, identical for every tenant, holding nothing about anybody."""
    for tenant in (TENANT_A, TENANT_B):
        cur = _as_tenant(db, tenant)
        cur.execute("SELECT count(*) FROM realtime_capability_profiles")
        assert cur.fetchone()[0] == 4
