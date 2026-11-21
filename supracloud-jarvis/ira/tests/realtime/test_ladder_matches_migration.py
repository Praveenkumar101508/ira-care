"""The Python vocabulary and the SQL that enforces it must be the same thing.

Phase 6 states its rules twice: once in `core/realtime` where the process reads
them, and once in `postgres/027_realtime.sql` where the database enforces them.
That duplication is deliberate — the database constraint is the one that cannot
be bypassed, and the Python is the one that produces a comprehensible error at
the boundary.

Duplication that nothing checks is just two sources of truth. So these tests
parse the migration and compare it, field by field, against the constants. Same
arrangement as Phase 3's catalogues and Phase 4's tool policies.

The failure mode being prevented is specific and quiet: someone adds a metric
key to `ALLOWED_METRIC_KEYS`, the application accepts it, and every insert then
fails a CHECK constraint at runtime — in the middle of a live call, while
trying to record that something had already gone wrong.

These tests need no database. They read the file.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.realtime.capability import LADDER, CapabilityLevel
from core.realtime.session import EndReason, EventKind, SessionState
from core.realtime.telemetry import ALLOWED_METRIC_KEYS
from core.realtime.transport import KNOWN_REGIONS

# tests/realtime/ -> tests/ -> ira/ -> supracloud-jarvis/
MIGRATION = (
    Path(__file__).resolve().parents[3] / "postgres" / "027_realtime.sql"
)


@pytest.fixture(scope="module")
def sql() -> str:
    assert MIGRATION.is_file(), f"missing {MIGRATION}"
    return MIGRATION.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def code(sql: str) -> str:
    """The migration with comments stripped.

    Every vocabulary below also appears in the prose at the top of the file, so
    scanning the raw text would pass on documentation alone — which is exactly
    the state these tests exist to catch.
    """
    return re.sub(r"--[^\n]*", "", sql)


def _check_vocabulary(code: str, column: str) -> set[str]:
    """The literals inside `CHECK (<column> IN (...))`."""
    match = re.search(
        rf"CHECK\s*\(\s*{re.escape(column)}\s+IN\s*\((.*?)\)\s*\)",
        code,
        re.IGNORECASE | re.DOTALL,
    )
    assert match, f"no CHECK ... {column} IN (...) found in the migration"
    return set(re.findall(r"'([^']+)'", match.group(1)))


# ── The capability ladder ────────────────────────────────────────────────────


def test_the_seeded_ladder_matches_the_python_ladder(code: str):
    """Rung by rung, field by field. A mismatch means the budget the docs quote
    and the budget the code measures against are different numbers."""
    block = code.split("INSERT INTO realtime_capability_profiles", 1)[1]
    block = block.split("ON CONFLICT", 1)[0]

    rows = re.findall(
        r"\(\s*'(\w+)'\s*,\s*(\d+)\s*,\s*(TRUE|FALSE)\s*,\s*(TRUE|FALSE)\s*,"
        r"\s*(\d+|NULL)\s*,\s*(\d+)\s*\)",
        block,
        re.IGNORECASE,
    )
    assert len(rows) == len(LADDER), (
        f"the migration seeds {len(rows)} rung(s), the Python ladder has "
        f"{len(LADDER)}"
    )

    seeded = {
        level: (
            int(rung),
            avatar.upper() == "TRUE",
            audio.upper() == "TRUE",
            None if budget.upper() == "NULL" else int(budget),
            int(kbps),
        )
        for level, rung, avatar, audio, budget, kbps in rows
    }

    for profile in LADDER:
        assert profile.level.value in seeded, (
            f"rung {profile.level.value!r} is in the Python ladder but is not "
            f"seeded by the migration"
        )
        assert seeded[profile.level.value] == (
            profile.rung,
            profile.renders_avatar,
            profile.produces_audio,
            profile.target_first_audio_ms,
            profile.min_downlink_kbps,
        ), f"rung {profile.level.value!r} differs between the migration and Python"


def test_the_capability_enum_matches_the_schema_check(code: str):
    assert _check_vocabulary(code, "level") == {
        level.value for level in CapabilityLevel
    }


def test_the_ladder_is_a_ladder():
    """Rungs are contiguous from 1, and get cheaper as they descend.

    A ladder with a gap, or one where descending asks for MORE bandwidth, is
    not a degradation path — it is a way to make a struggling connection worse.
    """
    rungs = [p.rung for p in LADDER]
    assert rungs == list(range(1, len(LADDER) + 1)), f"rungs are not contiguous: {rungs}"
    assert rungs == sorted(rungs), "LADDER must be ordered richest first"

    for richer, poorer in zip(LADDER, LADDER[1:]):
        assert poorer.min_downlink_kbps <= richer.min_downlink_kbps, (
            f"descending from {richer.level.value} to {poorer.level.value} "
            f"asks for MORE bandwidth"
        )


def test_only_the_floor_is_silent():
    """Exactly one rung produces no audio, and it is the last one.

    If a middle rung were silent, descending past it would take the user
    through a state where IRA cannot speak and then back to one where it can —
    and `best_supported` would be able to select it.
    """
    silent = [p for p in LADDER if not p.produces_audio]
    assert len(silent) == 1, f"expected exactly one silent rung, got {silent}"
    assert silent[0] is LADDER[-1]


def test_every_audible_rung_states_a_budget():
    """A budget is the number the measurement is judged against. A rung that
    produces audio without one cannot be held to anything."""
    for profile in LADDER:
        if profile.produces_audio:
            assert profile.target_first_audio_ms is not None, (
                f"{profile.level.value} produces audio but states no budget"
            )
            assert profile.target_first_audio_ms > 0
        else:
            assert profile.target_first_audio_ms is None, (
                f"{profile.level.value} produces no audio but claims a budget — "
                f"a number here would read as a target it can never miss"
            )


# ── The vocabularies the database enforces ───────────────────────────────────


def test_the_metric_allow_list_matches_the_check_constraint(code: str):
    """The one that fails at the worst moment if it drifts.

    An extra key in Python is accepted by the application and rejected by the
    database — mid-call, while recording a problem.
    """
    guard = code.split("realtime_metrics_are_measurements(m JSONB)", 1)[1]
    guard = guard.split("$$", 2)[1]
    # Scoped to the key list specifically. A blanket scan for quoted words
    # would also collect 'number', 'boolean' and 'object' from the
    # jsonb_typeof comparisons beside it, and report a difference that is not
    # one.
    keys = re.search(r"kv\.k\s+NOT\s+IN\s*\((.*?)\)", guard, re.DOTALL | re.IGNORECASE)
    assert keys, "the metric key allow-list is no longer a `kv.k NOT IN (...)` list"
    sql_keys = set(re.findall(r"'(\w+)'", keys.group(1)))

    assert sql_keys == set(ALLOWED_METRIC_KEYS), (
        "the metric allow-list differs between telemetry.py and "
        f"027_realtime.sql.\n  only in SQL:    {sorted(sql_keys - ALLOWED_METRIC_KEYS)}"
        f"\n  only in Python: {sorted(ALLOWED_METRIC_KEYS - sql_keys)}"
    )


def test_the_metric_guard_restricts_types_and_not_just_key_names(code: str):
    """Blocking the key 'transcript' would be defeated by calling it 'note'.

    The property that actually holds is that no STRING can be stored, under
    any key. This asserts the type restriction is present, because an
    allow-list alone would look correct and protect much less.
    """
    guard = code.split("realtime_metrics_are_measurements(m JSONB)", 1)[1]
    assert "jsonb_typeof" in guard
    assert re.search(r"NOT\s+IN\s*\(\s*'number'\s*,\s*'boolean'\s*\)", guard), (
        "the guard no longer restricts value types to numbers and booleans — "
        "a string of any key name could then be stored"
    )


def test_the_session_states_match_the_schema(code: str):
    assert _check_vocabulary(code, "state") == {s.value for s in SessionState}


def _effective_vocabulary(column: str, constraint: str) -> tuple[set[str], str]:
    """The vocabulary a freshly migrated database actually enforces.

    P14-01b. This file used to read `027` alone, which was right until a later
    migration touched one of these constraints — `084` widens `end_reason` — at
    which point comparing the enum against `027` reports a drift that is not one
    while missing any that is.

    So the chain is composed rather than one file read: start at `027`'s inline
    CHECK, then walk every higher-numbered migration that redefines the named
    constraint and take the last. That is what `apply_migrations` does, in
    number order, and it keeps this test correct for the NEXT such migration
    instead of needing to be repointed at each one.

    Returns the vocabulary and the file it came from, so a failure says which
    migration it was measured against.
    """
    postgres = MIGRATION.parent
    latest = _check_vocabulary(
        re.sub(r"--[^\n]*", "", MIGRATION.read_text(encoding="utf-8")), column)
    source = MIGRATION.name

    for path in sorted(postgres.glob("[0-9][0-9][0-9]_*.sql")):
        if path.name <= MIGRATION.name:
            continue
        body = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
        if f"ADD CONSTRAINT {constraint}" not in body:
            continue
        block = body.split(f"ADD CONSTRAINT {constraint}", 1)[1].split(";", 1)[0]
        latest = set(re.findall(r"'([^']+)'", block))
        source = path.name
    return latest, source


def test_the_end_reasons_match_the_schema():
    """The enum and the CHECK the database ends up with, not the one 027 shipped."""
    vocabulary, source = _effective_vocabulary(
        "end_reason", "realtime_sessions_end_reason_check")
    declared = {r.value for r in EndReason}
    assert vocabulary == declared, (
        f"EndReason and the schema disagree (schema measured from {source}).\n"
        f"  only in SQL:    {sorted(vocabulary - declared)}\n"
        f"  only in Python: {sorted(declared - vocabulary)}")


def test_the_end_reason_vocabulary_is_actually_composed():
    """The helper above must be reading the chain, not just 027.

    Without this, a bug that made `_effective_vocabulary` ignore later
    migrations would be invisible: the test above would go red and somebody
    would "fix" it by deleting the enum member. `unknown` is in `084` and not in
    `027`, so it is the evidence that composition happened.
    """
    vocabulary, source = _effective_vocabulary(
        "end_reason", "realtime_sessions_end_reason_check")
    assert source != MIGRATION.name, (
        "the effective vocabulary came from 027, so no later migration was "
        "composed — if 084 was reverted, this test and the enum must change "
        "together")
    assert "unknown" in vocabulary
    base = _check_vocabulary(
        re.sub(r"--[^\n]*", "", MIGRATION.read_text(encoding="utf-8")),
        "end_reason")
    assert "unknown" not in base, (
        "027 was edited to add the value instead of a new migration adding it; "
        "applied migration files are immutable")


def test_the_event_kinds_match_the_schema(code: str):
    assert _check_vocabulary(code, "kind") == {k.value for k in EventKind}


def test_the_regions_match_the_schema(code: str):
    assert _check_vocabulary(code, "provider_region") == set(KNOWN_REGIONS)


def test_the_providers_the_schema_accepts_include_the_default(code: str):
    """'none' must be a storable value. It is what a private-edition install
    records on every session, and a schema that could not store it would make
    the honest answer the one that fails."""
    providers = _check_vocabulary(code, "provider")
    assert "none" in providers
    assert "livekit" in providers


# ── The structural no-content guarantees ─────────────────────────────────────


def test_the_disclosure_columns_are_generated_and_false(code: str):
    """`audio_stored` and `transcript_stored` must be unwritable.

    If these ever become ordinary columns, the Privacy Centre's claim stops
    being backed by the schema and becomes a value someone remembered to set.
    """
    for column in ("audio_stored", "transcript_stored"):
        assert re.search(
            rf"{column}\s+BOOLEAN\s+GENERATED\s+ALWAYS\s+AS\s*\(\s*FALSE\s*\)\s+STORED",
            code,
            re.IGNORECASE,
        ), (
            f"{column} is no longer GENERATED ALWAYS AS (FALSE) STORED. It is "
            f"the database guarantee behind a claim shown to users."
        )


def test_no_column_in_this_migration_can_hold_free_text(code: str):
    """The structural claim: a transcript has nowhere to go.

    Every TEXT column must be constrained — by a CHECK vocabulary or by a
    foreign key to the ladder. A bare `TEXT` column, or any BYTEA, is a place
    content could be put later without anybody noticing.
    """
    assert not re.search(r"\bBYTEA\b", code, re.IGNORECASE), (
        "a BYTEA column appeared in the realtime schema — that is where audio "
        "would be stored"
    )

    creates = re.findall(
        r"CREATE TABLE IF NOT EXISTS (realtime_\w+)\s*\((.*?)\n\);",
        code,
        re.DOTALL,
    )
    assert creates, "no realtime tables parsed — the scan is wrong"

    unconstrained: list[str] = []
    for table, body in creates:
        # Split on top-level commas, not on newlines: a column's CHECK is
        # routinely on the line below its type, and a line-based scan reports
        # every one of them as unconstrained.
        definitions: list[str] = []
        depth = 0
        current: list[str] = []
        for char in body:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if char == "," and depth == 0:
                definitions.append("".join(current))
                current = []
            else:
                current.append(char)
        definitions.append("".join(current))

        for definition in definitions:
            flat = " ".join(definition.split())
            match = re.match(r"(\w+)\s+TEXT\b(.*)", flat, re.IGNORECASE)
            if not match:
                continue
            column, rest = match.group(1), match.group(2).upper()
            if "CHECK" in rest or "REFERENCES" in rest or "PRIMARY KEY" in rest:
                continue
            unconstrained.append(f"{table}.{column}")

    assert not unconstrained, (
        f"unconstrained TEXT column(s) in the realtime schema: {unconstrained}. "
        f"Every TEXT column here must be a fixed vocabulary (CHECK) or a key. "
        f"A free-text column is where a transcript ends up."
    )


def test_the_migration_enables_and_forces_rls(sql: str):
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql, (
        "FORCE is what stops the table owner bypassing the policy — without it "
        "the isolation tests pass while protecting nothing."
    )
    assert "WITH CHECK" in sql
    assert "NULLIF(current_setting('app.tenant_id', true), '')" in sql


@pytest.mark.parametrize("table", ["realtime_sessions", "realtime_events"])
def test_every_realtime_table_is_listed_in_the_rls_loop(sql: str, table: str):
    """The policy loop is driven by an array literal. A table added to the
    schema but forgotten there gets no policy at all — and, unlike a syntax
    error, that fails OPEN."""
    loop = sql.split("scoped TEXT[] := ARRAY[", 1)[1].split("]", 1)[0]
    assert f"'{table}'" in loop, f"{table} is missing from the RLS policy loop"


def test_measurements_cannot_be_revised_but_can_be_erased(sql: str):
    """UPDATE refused, DELETE permitted.

    Both halves matter. A revisable latency figure is worse than none; a
    telemetry row that erasure cannot reach would make Phase 5's deletion
    promise false for anyone who used a live session.
    """
    assert "realtime_events_are_not_revisable" in sql
    assert "BEFORE UPDATE ON realtime_events" in sql
    assert "BEFORE UPDATE OR DELETE ON realtime_events" not in sql, (
        "the trigger now blocks DELETE too, which puts these rows out of "
        "reach of erasure — see ADR-0004 for where that line is drawn"
    )


def test_the_migration_has_a_rollback():
    rollback = MIGRATION.parent / "rollback" / "027_realtime_down.sql"
    assert rollback.is_file(), f"027_realtime.sql has no rollback at {rollback}"
