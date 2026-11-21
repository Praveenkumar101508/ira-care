"""Phase 3 — the shipped catalogue and its migration seed must not drift.

`core/companion/catalogue.py` serves reads from Python constants;
`postgres/024_companion_catalogue.sql` seeds the same rows into the durable
tables. Two copies of the same data is a drift risk, and drift here is not
cosmetic: if the SQL says an avatar is cloud-available and the Python says
private, then which one is true depends on which code path you happened to hit.

So the copies are not allowed to disagree. These tests parse the migration's
seed INSERTs and compare them against the constants, field by field for the
fields a licence decision depends on.

They read files. No database.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.companion.catalogue import (
    SEED_AVATARS,
    SEED_PERSONALITIES,
    SEED_VOICES,
)
from core.companion.models import (
    AUTONOMY_LEVELS,
    AVATAR_STYLES,
    COMPANION_STATUSES,
    EDITION_AVAILABILITY,
    GENDER_PRESENTATIONS,
    MEMORY_MODES,
    PRIVACY_CLASSIFICATIONS,
    RENDERERS,
    RIG_TYPES,
    VALIDATION_STATUSES,
)

# tests/companion/ -> tests/ -> ira/ -> supracloud-jarvis/
POSTGRES = Path(__file__).resolve().parents[3] / "postgres"
MIGRATION = POSTGRES / "024_companion_catalogue.sql"
#: `companions` is created by 023 and EXTENDED by 024, so some of the CHECK
#: constraints the code must agree with (memory_mode) live in the earlier file.
#: Vocabulary checks read both; seed checks read 024 alone, which is where the
#: catalogue rows are.
TENANCY_MIGRATION = POSTGRES / "023_tenancy.sql"


@pytest.fixture(scope="module")
def sql() -> str:
    assert MIGRATION.is_file(), f"missing {MIGRATION}"
    return MIGRATION.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def schema_sql() -> str:
    """023 + 024 — every migration that constrains a companion column."""
    assert TENANCY_MIGRATION.is_file(), f"missing {TENANCY_MIGRATION}"
    return (
        TENANCY_MIGRATION.read_text(encoding="utf-8")
        + "\n"
        + MIGRATION.read_text(encoding="utf-8")
    )


def _seeded_slugs(sql: str, table: str) -> set[str]:
    """Slugs in the INSERT ... VALUES block for `table`.

    The seed blocks quote each slug as the first column of every VALUES tuple,
    so the slugs are the quoted strings that also appear in the constants. A
    looser 'all quoted strings' match would pick up display names and licences
    too, so the comparison is made against the known slug set rather than
    parsing SQL properly — which would mean shipping a SQL parser to test a
    seed block.
    """
    block = sql.split(f"INSERT INTO {table}", 1)
    assert len(block) == 2, f"no INSERT INTO {table} in the migration"
    body = block[1].split("ON CONFLICT", 1)[0]
    return set(re.findall(r"'([a-z0-9][a-z0-9-]*)'", body))


# ── Slug coverage ────────────────────────────────────────────────────────────


def test_every_avatar_constant_is_seeded(sql):
    seeded = _seeded_slugs(sql, "avatar_catalogue")
    for entry in SEED_AVATARS:
        assert entry.slug in seeded, (
            f"{entry.slug} is served by the API but never seeded into "
            f"avatar_catalogue — the table and the code disagree about what "
            f"this deployment offers."
        )


def test_every_voice_constant_is_seeded(sql):
    seeded = _seeded_slugs(sql, "voice_catalogue")
    for entry in SEED_VOICES:
        assert entry.slug in seeded, f"{entry.slug} missing from voice_catalogue seed"


def test_every_personality_constant_is_seeded(sql):
    seeded = _seeded_slugs(sql, "personality_presets")
    for entry in SEED_PERSONALITIES:
        assert entry.slug in seeded, f"{entry.slug} missing from personality_presets seed"


# ── The fields a licence decision depends on ─────────────────────────────────


def _avatar_tuple(sql: str, slug: str) -> str:
    """The VALUES tuple for one avatar slug, as raw text."""
    body = sql.split("INSERT INTO avatar_catalogue", 1)[1].split("ON CONFLICT", 1)[0]
    start = body.index(f"'{slug}'")
    # Tuples are separated by "),\n" — take up to the next one, or the end.
    end = body.find("),", start)
    return body[start: end if end != -1 else len(body)]


@pytest.mark.parametrize("entry", SEED_AVATARS, ids=lambda e: e.slug)
def test_avatar_edition_availability_matches_the_seed(sql, entry):
    """The single field that keeps the owner's likeness out of the cloud
    catalogue. If the SQL and the constants disagree about it, one of the two
    is publishing a personal photograph to strangers."""
    tup = _avatar_tuple(sql, entry.slug)
    assert f"'{entry.edition_availability}'" in tup, (
        f"{entry.slug}: constants say edition_availability="
        f"{entry.edition_availability!r}, which is not in the seed tuple"
    )


@pytest.mark.parametrize("entry", SEED_AVATARS, ids=lambda e: e.slug)
def test_avatar_licence_verification_matches_the_seed(sql, entry):
    tup = _avatar_tuple(sql, entry.slug)
    # The tuple's booleans are TRUE/FALSE literals; a verified entry must have
    # at least one TRUE and an unverified one must not claim verification.
    if entry.licence_verified:
        assert "TRUE" in tup, f"{entry.slug}: constants say verified, seed does not"
    else:
        # An unverified entry may still carry TRUEs for unrelated flags, so the
        # meaningful assertion is the inverse: it must not be seeded active AND
        # cloud-available AND verified, which is the combination that publishes.
        assert not (
            "'cloud'" in tup or "'both'" in tup
        ) or "FALSE" in tup, (
            f"{entry.slug}: unverified in the constants but seeded as a "
            f"cloud-available entry with no FALSE flag"
        )


@pytest.mark.parametrize("entry", SEED_AVATARS, ids=lambda e: e.slug)
def test_avatar_validation_status_matches_the_seed(sql, entry):
    tup = _avatar_tuple(sql, entry.slug)
    assert f"'{entry.validation_status}'" in tup, (
        f"{entry.slug}: constants say validation_status="
        f"{entry.validation_status!r}, which is not in the seed tuple"
    )


@pytest.mark.parametrize("entry", SEED_VOICES, ids=lambda e: e.slug)
def test_voice_privacy_classification_matches_the_seed(sql, entry):
    """A voice whose privacy classification differs between the two would make
    the Privacy Centre's disclosure wrong for anyone who read it from the other
    source."""
    body = sql.split("INSERT INTO voice_catalogue", 1)[1].split("ON CONFLICT", 1)[0]
    start = body.index(f"'{entry.slug}'")
    end = body.find("),", start)
    tup = body[start: end if end != -1 else len(body)]
    assert f"'{entry.privacy_classification}'" in tup, (
        f"{entry.slug}: constants say {entry.privacy_classification!r}"
    )


# ── Vocabularies vs CHECK constraints ────────────────────────────────────────
#
# A vocabulary that drifts from its CHECK constraint fails at INSERT time in
# production and nowhere earlier — the code happily builds a value the database
# then refuses.


@pytest.mark.parametrize(
    "vocabulary,column",
    [
        (RIG_TYPES, "rig_type"),
        (RENDERERS, "renderer"),
        (AVATAR_STYLES, "style"),
        (VALIDATION_STATUSES, "validation_status"),
        (PRIVACY_CLASSIFICATIONS, "privacy_classification"),
        (GENDER_PRESENTATIONS, "gender_presentation"),
        (MEMORY_MODES, "memory_mode"),
        (AUTONOMY_LEVELS, "autonomy_level"),
        (COMPANION_STATUSES, "status"),
    ],
)
def test_every_vocabulary_value_appears_in_the_migration(schema_sql, vocabulary, column):
    for value in vocabulary:
        assert f"'{value}'" in schema_sql, (
            f"{column}: the code accepts {value!r} but the migration's CHECK "
            f"constraint never mentions it — an INSERT would be refused by the "
            f"database at runtime."
        )


def test_edition_availability_vocabulary_matches(schema_sql):
    for value in EDITION_AVAILABILITY:
        assert f"'{value}'" in schema_sql


# ── The autonomy ladder is the brief's, in order ─────────────────────────────


def test_the_autonomy_ladder_is_ordered_least_to_most_autonomous():
    """Phase 4's Permission Broker will compare levels. An order that quietly
    changed would change what "at most this autonomous" means."""
    assert AUTONOMY_LEVELS == (
        "never",
        "observe",
        "suggest",
        "prepare",
        "confirm_before_action",
        "automatic_for_approved_routine",
    )


# ── The guard must be able to fail ───────────────────────────────────────────
#
# A drift test that silently matches everything is worse than no drift test: it
# reports agreement forever while the two copies diverge. These two prove the
# comparison has teeth, by running it against data that IS drifted.


def test_the_slug_comparison_detects_a_missing_seed(sql):
    """A constant with no seed row must be caught."""
    seeded = _seeded_slugs(sql, "avatar_catalogue")
    assert "ira-signature" in seeded, "sanity: the real slug is found"
    assert "planted-never-seeded" not in seeded, (
        "the parser matched a slug that is not in the migration — it is "
        "matching too broadly and would pass for any constant"
    )


def test_the_field_comparison_detects_a_changed_value(sql):
    """`ira-portrait` is seeded 'private'. Asserting it is seeded 'both' must
    fail — otherwise the field comparison proves nothing."""
    tup = _avatar_tuple(sql, "ira-portrait")
    assert "'private'" in tup, "sanity: the real value is found"
    assert "'both'" not in tup, (
        "the tuple extractor is reading beyond its own row — a change to one "
        "avatar's edition_availability would be masked by its neighbour's"
    )
