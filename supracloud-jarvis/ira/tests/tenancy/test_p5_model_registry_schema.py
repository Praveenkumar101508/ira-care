"""Phase 5 batch 01 — what migration 067 guarantees, asked of the live schema.

`BASELINE_MODEL_INVENTORY.md` §8 is the brief:

    FINDING MOD-02 — model artefacts are neither hashed nor pinned.
    No allowlist prevents an arbitrary `ollama pull`.
    No separation between development, staging and production model registries.

and §6 lists the registry columns MOD-03 wants. This suite asserts the schema
delivers them, against the database the application actually connects to.

── Why a database test rather than a file test ────────────────────────────

Reading `067_model_registry.sql` and checking it contains the word CHECK proves
that somebody typed a constraint. It does not prove PostgreSQL enforces one —
and the difference is the whole finding. Every assertion below writes a row and
watches it be refused.

── The rollback and the destructive cases are NOT here ────────────────────

They run in CI on a disposable database (`iratest067`), because proving that
`rollback/067` refuses while a model is serving means dropping the registry
afterwards, and a destructive rollback test must never be able to reach the
development database. This suite only writes and rolls back.
"""
from __future__ import annotations

import os
import uuid

import pytest

from tests.tenancy._licence_fixture import approved_licence

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

HASH = "sha256:" + "a" * 64
OTHER_HASH = "sha256:" + "b" * 64


@pytest.fixture
async def db():
    """A connection in a transaction that is ALWAYS rolled back.

    The registry is platform configuration on a shared development database, so
    a test that committed would leave a model registered for every later test —
    and `model_registry_one_serving_version` would then fail somebody else's
    run for a reason that had nothing to do with their change.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


def _key() -> str:
    return f"m{uuid.uuid4().hex[:12]}"


async def _register(db, **kw):
    fields = {
        "model_key": _key(), "version": 1, "base_model": "org/model",
        "revision": "9f8e7d6c", "artifact_hash": HASH,
        "environment": "development",
    }
    fields.update(kw)
    cols = ", ".join(fields)
    args = ", ".join(f"${i}" for i in range(1, len(fields) + 1))
    return await db.fetchval(
        f"INSERT INTO model_registry ({cols}) VALUES ({args}) RETURNING id",
        *fields.values())


async def _serving(db):
    """The SET fragment that puts a row into service, under 068.

    068 makes `status = 'serving'` require a `model_licence_register` row
    classified `known_approved`, so `commercial_licence = '<string>'` no longer
    suffices — which is the entire point of that migration, and is asserted
    directly in `test_p6_licence_register_schema.py`.

    The BEFORE trigger runs ahead of the table's CHECK constraints, so a row
    with no `licence_id` fails on the trigger and 067's `serving_is_licensed`
    never gets the chance to fire. The two tests below that exist to prove
    `serving_is_licensed` therefore supply a valid licence_id and withhold the
    free-text field instead — otherwise they would silently stop testing the
    constraint they name while still passing.
    """
    licence_id = await approved_licence(db)
    return (f"commercial_licence = 'test-fixture-not-a-determination', "
            f"licence_recorded_at = NOW(), rollout_percentage = 100, "
            f"licence_id = '{licence_id}'::uuid"), licence_id


# ─────────────────────────────────────────────────────────────────────────────
# MOD-02 — pinning and hashing
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("revision", ["latest", "LATEST", "main", "master",
                                      "head", "HEAD", ""])
async def test_a_revision_that_does_not_pin_is_refused(db, revision):
    """"`voice/tts_indic.py` calls `from_pretrained()` **without revision
    pinning** — it fetches whatever the Hub serves at that moment."

    A moving reference is the defect. `latest` is the obvious one; `main` and
    `head` are the same defect spelled in a way that looks deliberate, which is
    why they are refused too and why the check is case-insensitive.
    """
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc:
        await _register(db, revision=revision)
    assert "revision_is_actually_pinned" in str(exc.value)


@pytest.mark.parametrize("bad", [
    "not-a-hash",
    "sha256:short",
    "a" * 64,                       # right length, no algorithm
    "sha256:" + "A" * 64,           # uppercase — two spellings of one hash
    "md5:" + "a" * 32,              # a real hash, wrong algorithm
])
async def test_an_artefact_hash_that_verifies_nothing_is_refused(db, bad):
    """A malformed hash is worse than a missing one: the column is populated,
    every "is the hash recorded" check passes, and the value can never match
    anything computed from a real artefact."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc:
        await _register(db, artifact_hash=bad)
    assert "artifact_hash_is_a_sha256" in str(exc.value)


async def test_a_registration_without_a_hash_is_refused(db):
    """NOT NULL rather than "recommended". A registry entry with no hash cannot
    answer "is the artefact on disk the artefact we approved"."""
    with pytest.raises(asyncpg.exceptions.NotNullViolationError):
        await db.execute(
            "INSERT INTO model_registry (model_key, version, base_model, "
            " revision, environment) VALUES ($1, 1, 'org/m', 'abc123', "
            " 'development')", _key())


async def test_a_pinned_registration_is_accepted(db):
    """The positive case. Without it every test above would pass against a
    table that refuses everything, which is not the same as a table that
    refuses the right things."""
    assert await _register(db) is not None


# ─────────────────────────────────────────────────────────────────────────────
# MOD-01 — Phase 6's gate, not Phase 5's answer
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_model_with_no_recorded_licence_cannot_serve(db):
    """MOD-01 "blocks Phase 6" per the inventory. This is what makes it a block
    rather than a note.

    Phase 5 does not decide any model's licence — that is a legal determination
    and not an engineering task. What it can do is make the unrecorded state
    unable to serve, so Phase 6 has something to unblock.
    """
    key = _key()
    await _register(db, model_key=key)
    licence_id = await approved_licence(db)
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc:
        await db.execute(
            "UPDATE model_registry SET status = 'serving', "
            "  rollout_percentage = 100, licence_id = $2 WHERE model_key = $1",
            key, licence_id)
    assert "serving_is_licensed" in str(exc.value)


async def test_a_licence_string_alone_is_not_enough(db):
    """Both halves, or neither. A licence with no `licence_recorded_at` is a
    value somebody typed; the timestamp is what says a determination was made
    and when — which is the part an auditor asks about."""
    key = _key()
    await _register(db, model_key=key)
    licence_id = await approved_licence(db)
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc:
        await db.execute(
            "UPDATE model_registry SET status = 'serving', "
            "  rollout_percentage = 100, licence_id = $2, "
            "  commercial_licence = 'test-fixture-not-a-determination' "
            " WHERE model_key = $1", key, licence_id)
    assert "serving_is_licensed" in str(exc.value)


async def test_a_licensed_model_may_serve(db):
    key = _key()
    await _register(db, model_key=key)
    lic, _ = await _serving(db)
    await db.execute(
        f"UPDATE model_registry SET status = 'serving', {lic} "
        f" WHERE model_key = $1", key)
    assert await db.fetchval(
        "SELECT status FROM model_registry WHERE model_key = $1", key) == "serving"


async def test_no_real_model_has_a_licence_recorded_anywhere(db):
    """**The honesty check.**

    Phase 5 must not quietly answer MOD-01. If a future batch seeds the registry
    with `qwen3` and `commercial_licence = 'apache-2.0'`, the repository has made
    a legal representation nobody verified — and `MODEL_LICENSE_REGISTER.md`
    still says the position is unrecorded, so the two documents would disagree
    with the database.

    Fixtures are exempt by naming themselves: any licence recorded by a test
    must say it is not a determination.
    """
    rows = await db.fetch(
        "SELECT model_key, commercial_licence FROM model_registry "
        " WHERE commercial_licence IS NOT NULL")
    liars = [r["model_key"] for r in rows
             if "fixture" not in (r["commercial_licence"] or "").lower()]
    assert not liars, (
        f"a commercial licence is recorded for {liars}. Determining a model's "
        f"commercial terms is Phase 6 (MOD-01) and a legal determination — "
        f"Phase 5 may make the unrecorded state refuse to serve, and may not "
        f"fill the field in.")


# ─────────────────────────────────────────────────────────────────────────────
# MOD-03 — versioning, rollout, rollback, environment separation
# ─────────────────────────────────────────────────────────────────────────────


async def test_only_one_version_serves_per_model_and_environment(db):
    """Two serving versions makes "which model answered this request" —
    the question a rollback exists to act on — unanswerable."""
    key = _key()
    # `rollout_percentage` goes with it: 067 refuses a serving row that
    # reaches nobody.
    lic, _ = await _serving(db)
    await _register(db, model_key=key, version=1)
    await _register(db, model_key=key, version=2, revision="1a2b3c4d")
    await db.execute(
        f"UPDATE model_registry SET status='serving', {lic} "
        " WHERE model_key = $1 AND version = 1", key)
    with pytest.raises(asyncpg.exceptions.UniqueViolationError) as exc:
        await db.execute(
            f"UPDATE model_registry SET status='serving', {lic} "
            " WHERE model_key = $1 AND version = 2", key)
    assert "model_registry_one_serving_version" in str(exc.value)


async def test_the_same_version_may_serve_in_two_environments(db):
    """That is what separate registries MEANS. A model serving in staging and
    not yet in production is the normal case, and a uniqueness rule that
    forbade it would force a fork of the whole table per environment."""
    key = _key()
    # `rollout_percentage` goes with it: 067 refuses a serving row that
    # reaches nobody.
    lic, _ = await _serving(db)
    await _register(db, model_key=key, version=1, environment="staging")
    await _register(db, model_key=key, version=1, environment="production")
    for env in ("staging", "production"):
        await db.execute(
            f"UPDATE model_registry SET status='serving', {lic} "
            " WHERE model_key = $1 AND environment = $2", key, env)
    assert await db.fetchval(
        "SELECT count(*) FROM model_registry WHERE model_key = $1 "
        "  AND status = 'serving'", key) == 2


async def test_an_unknown_environment_is_refused(db):
    """"No separation between development, staging and production model
    registries" is the finding. A free-text column would let `prod`, `Production`
    and `prd` become three registries nobody meant to create."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await _register(db, environment="prod")


async def test_a_rolled_back_version_must_name_its_replacement(db):
    """"This model is no longer serving", with no target, is an outage
    described as a status."""
    key = _key()
    await _register(db, model_key=key, version=1)
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc:
        await db.execute(
            "UPDATE model_registry SET status = 'rolled_back' "
            " WHERE model_key = $1", key)
    assert "rolled_back_names_its_target" in str(exc.value)


async def test_a_rollback_target_that_points_somewhere_real_is_accepted(db):
    key = _key()
    old = await _register(db, model_key=key, version=1)
    await _register(db, model_key=key, version=2, revision="1a2b3c4d")
    await db.execute(
        "UPDATE model_registry SET status = 'rolled_back', rollback_target = $2 "
        " WHERE model_key = $1 AND version = 2", key, old)
    assert await db.fetchval(
        "SELECT rollback_target FROM model_registry WHERE model_key = $1 "
        "  AND version = 2", key) == old


async def test_a_rollback_target_cannot_be_deleted_out_from_under_it(db):
    """ON DELETE RESTRICT. A rollback target that can vanish is a rollback plan
    with no rollback in it, and the failure surfaces at the worst moment."""
    key = _key()
    old = await _register(db, model_key=key, version=1)
    await _register(db, model_key=key, version=2, revision="1a2b3c4d")
    await db.execute(
        "UPDATE model_registry SET status='rolled_back', rollback_target = $2 "
        " WHERE model_key = $1 AND version = 2", key, old)
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await db.execute("DELETE FROM model_registry WHERE id = $1", old)


@pytest.mark.parametrize("pct", [-1, 101, 1000])
async def test_a_rollout_percentage_outside_zero_to_a_hundred_is_refused(db, pct):
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await _register(db, rollout_percentage=pct)


# ─────────────────────────────────────────────────────────────────────────────
# The allowlist
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_allowlist_pins_its_revisions_too(db):
    """Same rule, because the allowlist is the half that governs FETCHING —
    and an unpinned allowlist entry permits whatever the Hub serves today."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc:
        await db.execute(
            "INSERT INTO model_pull_allowlist (source, identifier, revision) "
            "VALUES ('huggingface', $1, 'main')", _key())
    assert "allowlist_revision_is_pinned" in str(exc.value)


async def test_an_unknown_source_cannot_be_allowlisted(db):
    """A source nobody wrote a fetcher for cannot be allowlisted into
    existence — otherwise the allowlist grows entries that permit fetches the
    code cannot perform, and the register stops describing the system."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db.execute(
            "INSERT INTO model_pull_allowlist (source, identifier, revision) "
            "VALUES ('some-mirror', $1, 'abc123')", _key())


async def test_an_allowlist_entry_may_have_no_hash_yet(db):
    """A real state, not a loophole: the hash cannot be known before the first
    pull. What must never happen is a LOAD proceeding against a NULL, and that
    is the loader's refusal to make — asserted in P5-03, not here."""
    ident = _key()
    await db.execute(
        "INSERT INTO model_pull_allowlist (source, identifier, revision) "
        "VALUES ('huggingface', $1, 'abc123')", ident)
    assert await db.fetchval(
        "SELECT artifact_hash FROM model_pull_allowlist WHERE identifier = $1",
        ident) is None


async def test_the_same_artefact_may_be_allowlisted_at_two_revisions(db):
    """Pinning does not mean one version forever. Upgrading is adding the new
    revision, which is what makes the old one auditable afterwards."""
    ident = _key()
    for rev in ("abc123", "def456"):
        await db.execute(
            "INSERT INTO model_pull_allowlist (source, identifier, revision) "
            "VALUES ('huggingface', $1, $2)", ident, rev)
    assert await db.fetchval(
        "SELECT count(*) FROM model_pull_allowlist WHERE identifier = $1",
        ident) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Authority
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_runtime_role_cannot_register_or_allowlist_anything():
    """**The load-bearing grant.**

    If the process that CHECKS the allowlist can also ADD to it, then a
    request-handling path talked into writing one entry has defeated the whole
    control — and MOD-02 exists because arbitrary fetches were possible.

    Asserted through a real `ira_app` connection rather than by reading the
    GRANT statement: `042` already produced a case where a rollback quietly
    restored a blanket grant that the migration text did not mention.
    """
    conn = await asyncpg.connect(DSN)
    try:
        assert await conn.fetchval(
            "SELECT has_table_privilege('ira_app','model_registry','SELECT')")
        for table in ("model_registry", "model_pull_allowlist"):
            for priv in ("INSERT", "UPDATE", "DELETE"):
                assert not await conn.fetchval(
                    "SELECT has_table_privilege('ira_app', $1, $2)",
                    table, priv), (
                    f"ira_app can {priv} {table}. The runtime role must not be "
                    f"able to register a model or widen the pull allowlist.")
    finally:
        await conn.close()


async def test_a_serving_model_that_reaches_nobody_is_refused(db):
    """**A trap found by writing the rollback test, made unrepresentable.**

    `rollout_percentage` defaults to 0. An operator who sets `status='serving'`
    and nothing else creates a model that serves NOBODY — and the symptom is
    not an error: requests simply stop being answered by it, silently, while
    the registry looks correct. Restoring a previous version during a rollback
    is exactly when somebody would do that.
    """
    key = _key()
    await _register(db, model_key=key)
    # A licence_id, for the same reason as the two `serving_is_licensed` tests
    # above: 068's BEFORE trigger runs ahead of the CHECK constraints, so
    # without one this fails on the licence gate and quietly stops testing the
    # rollout constraint it is named for.
    licence_id = await approved_licence(db)
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc:
        await db.execute(
            "UPDATE model_registry SET status = 'serving', "
            "  commercial_licence = 'test-fixture-not-a-determination', "
            "  licence_recorded_at = NOW(), licence_id = $2 "
            " WHERE model_key = $1", key, licence_id)
    assert "serving_reaches_somebody" in str(exc.value)
