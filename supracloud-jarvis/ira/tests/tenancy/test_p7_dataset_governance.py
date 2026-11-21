"""Phase 7 batch 01 — what migration 070 guarantees, asked of the live schema.

`BASELINE_MODEL_INVENTORY.md` §6, *"Absent — the whole of Phase 7"*:

    Datasets (raw/quarantined/reviewed/train/val/test)              absent
    Dataset cards, licences, PII/secret scanning, contamination     absent

Both rows describe a **directory layout**, and that is the easiest thing in
Phase 7's list to satisfy dishonestly. `mkdir -p datasets/reviewed` proves
nothing: a file that moved from `raw/` to `reviewed/` has been moved, not
reviewed.

So `070` makes the lifecycle enforced state, and this suite asserts the
enforcement against the database the application connects to — every test writes
a row and watches it be refused.

── The load-bearing invariant, and why it is not obvious ──────────────────

A check that passed is a fact **about the bytes it read**. Append one line to a
JSONL file and every previous check becomes a statement about a file that no
longer exists — while the `passed` record sits there looking current.

That is the most likely way this table would end up certifying unscanned data,
and it would be invisible: the dataset says `train`, the checks say `passed`,
and nothing anywhere disagrees. `dataset_checks.content_sha256` records what the
check actually read, the promotion gate compares it, and a content change on a
promoted dataset demotes it.

`test_checks_against_a_different_digest_do_not_count` and
`test_changing_the_content_demotes_a_promoted_dataset` are the two halves.
Neither is redundant: the first stops a stale check promoting, the second stops
a promotion surviving a later change.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
REQUIRED_CHECKS = ("pii", "secret", "contamination", "licence")

#: Satisfies the human-attribution constraints. Not a real person and not
#: presented as one — every use is inside a transaction that is rolled back.
A_PERSON = "Test Fixture Reviewer"
FIXTURE_BASIS = "Test fixture only. No lawful basis has been determined."


@pytest.fixture
async def db():
    """A connection in a transaction that is ALWAYS rolled back.

    A committed row here would be a committed statement that somebody reviewed
    a licence and recorded a lawful basis — the two things this phase exists to
    stop engineering doing on the owner's behalf.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


def _name() -> str:
    return f"p7ds{uuid.uuid4().hex[:12]}"


def _now() -> dt.datetime:
    """A bound datetime, not the string ``NOW()``.

    asyncpg binds parameters rather than interpolating them, so `'NOW()'`
    arrives as a five-character string and is rejected. P6-01's first draft did
    that in nine tests.
    """
    return dt.datetime.now(dt.timezone.utc)


async def _dataset(db, **kw):
    fields = {
        "name": _name(), "version": 1, "source": "synthetic fixture",
        "content_sha256": DIGEST, "record_count": 10,
    }
    fields.update(kw)
    cols = ", ".join(fields)
    args = ", ".join(f"${i}" for i in range(1, len(fields) + 1))
    return await db.fetchval(
        f"INSERT INTO training_datasets ({cols}) VALUES ({args}) RETURNING id",
        *fields.values())


async def _check(db, dataset_id, kind, *, digest=DIGEST, outcome="passed",
                 findings=0):
    return await db.fetchval(
        "INSERT INTO dataset_checks (dataset_id, check_kind, content_sha256, "
        "  outcome, finding_count, tool, tool_version) "
        "VALUES ($1,$2,$3,$4,$5,'fixture-scanner','0.0.0') RETURNING id",
        dataset_id, kind, digest, outcome, findings)


async def _all_checks(db, dataset_id, *, digest=DIGEST):
    for kind in REQUIRED_CHECKS:
        await _check(db, dataset_id, kind, digest=digest)


async def _govern(db, dataset_id, *, personal="no"):
    """Everything a human must record, so a test can isolate one missing piece."""
    await db.execute(
        "UPDATE training_datasets SET licence_classification = 'known_approved', "
        "  licence_classified_by = $2, licence_classified_at = NOW(), "
        "  lawful_basis = $3, lawful_basis_by = $2, lawful_basis_at = NOW(), "
        "  contains_personal_data = $4 WHERE id = $1",
        dataset_id, A_PERSON, FIXTURE_BASIS, personal)


async def _fully_governed(db, **kw):
    dataset_id = await _dataset(db, **kw)
    await _all_checks(db, dataset_id)
    await _govern(db, dataset_id)
    return dataset_id


# ─────────────────────────────────────────────────────────────────────────────
# The lifecycle is state, not a directory
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["reviewed", "train", "validation", "test"])
async def test_a_dataset_cannot_be_promoted_without_checks(db, state):
    """The whole point. Moving a file into `reviewed/` is not a review."""
    dataset_id = await _dataset(db)
    await _govern(db, dataset_id)
    with pytest.raises(asyncpg.CheckViolationError, match="no passing pii check"):
        await db.execute(
            "UPDATE training_datasets SET state = $2 WHERE id = $1",
            dataset_id, state)


@pytest.mark.parametrize("missing", REQUIRED_CHECKS)
async def test_every_required_check_is_required(db, missing):
    """All four by name. Parametrised so that removing one from the trigger's
    list fails here rather than being covered by whichever check happened to be
    tested."""
    dataset_id = await _dataset(db)
    await _govern(db, dataset_id)
    for kind in REQUIRED_CHECKS:
        if kind != missing:
            await _check(db, dataset_id, kind)
    with pytest.raises(asyncpg.CheckViolationError,
                       match=f"no passing {missing} check"):
        await db.execute(
            "UPDATE training_datasets SET state = 'reviewed' WHERE id = $1",
            dataset_id)


async def test_checks_against_a_different_digest_do_not_count(db):
    """**The invariant this table exists for.**

    Four checks, all `passed`, all against bytes that are not this dataset's.
    Without the digest comparison every one of them would promote it, and the
    row would look impeccable: state `reviewed`, four passes, no contradiction
    anywhere.
    """
    dataset_id = await _dataset(db, content_sha256=DIGEST)
    await _govern(db, dataset_id)
    await _all_checks(db, dataset_id, digest=OTHER_DIGEST)
    with pytest.raises(asyncpg.CheckViolationError, match="no passing pii check"):
        await db.execute(
            "UPDATE training_datasets SET state = 'reviewed' WHERE id = $1",
            dataset_id)


async def test_a_failed_check_does_not_promote(db):
    """A check that ran is not a check that passed — and `failed` is the state a
    scanner that found personal data leaves behind."""
    dataset_id = await _dataset(db)
    await _govern(db, dataset_id)
    for kind in REQUIRED_CHECKS:
        await _check(db, dataset_id, kind,
                     outcome="failed" if kind == "pii" else "passed",
                     findings=4 if kind == "pii" else 0)
    with pytest.raises(asyncpg.CheckViolationError, match="no passing pii check"):
        await db.execute(
            "UPDATE training_datasets SET state = 'reviewed' WHERE id = $1",
            dataset_id)


async def test_a_fully_governed_dataset_may_be_promoted(db):
    """The positive case. Without it every assertion here would pass against a
    trigger that refused everything, which is a different broken system and one
    that would look identical in a repository with no datasets."""
    dataset_id = await _fully_governed(db)
    await db.execute(
        "UPDATE training_datasets SET state = 'train' WHERE id = $1", dataset_id)
    assert await db.fetchval(
        "SELECT state FROM training_datasets WHERE id = $1", dataset_id) == "train"


@pytest.mark.parametrize("state", ["quarantined", "rejected", "shredded"])
async def test_the_safe_directions_are_never_blocked(db, state):
    """Quarantine, rejection and shredding are how somebody STOPS a dataset
    being used. A gate that blocked them would make the safe direction the hard
    one, and somebody would work around it by deleting the row — losing the
    record that the dataset ever existed."""
    dataset_id = await _dataset(db)
    await db.execute(
        "UPDATE training_datasets SET state = $2, record_count = 0 WHERE id = $1",
        dataset_id, state)
    assert await db.fetchval(
        "SELECT state FROM training_datasets WHERE id = $1", dataset_id) == state


async def test_changing_the_content_demotes_a_promoted_dataset(db):
    """**The other half of the digest rule.**

    The promotion gate fires on the transition INTO a promoted state, so it
    cannot see a content change that happens afterwards. Without this, an
    operator could promote a dataset with four valid checks and then update
    `content_sha256` — leaving it in `train` with every check describing
    different bytes.

    A demotion rather than a refusal, because re-ingesting data is normal and a
    refused UPDATE would push somebody to delete and recreate the row, losing
    its history.
    """
    dataset_id = await _fully_governed(db)
    await db.execute(
        "UPDATE training_datasets SET state = 'train' WHERE id = $1", dataset_id)

    await db.execute(
        "UPDATE training_datasets SET content_sha256 = $2 WHERE id = $1",
        dataset_id, OTHER_DIGEST)

    row = await db.fetchrow(
        "SELECT state, notes FROM training_datasets WHERE id = $1", dataset_id)
    assert row["state"] == "raw", (
        "a promoted dataset survived a content change, so its four passing "
        "checks now describe bytes nobody scanned")
    assert "content changed" in row["notes"], (
        "the demotion left no record of why, so an operator sees a dataset that "
        "fell out of train for no visible reason")


async def test_a_demoted_dataset_can_be_repromoted_after_rescanning(db):
    """The demotion must be recoverable, or it is a trap rather than a control.
    Re-run the checks at the new digest and the dataset promotes again."""
    dataset_id = await _fully_governed(db)
    await db.execute(
        "UPDATE training_datasets SET state = 'train' WHERE id = $1", dataset_id)
    await db.execute(
        "UPDATE training_datasets SET content_sha256 = $2 WHERE id = $1",
        dataset_id, OTHER_DIGEST)

    await _all_checks(db, dataset_id, digest=OTHER_DIGEST)
    await db.execute(
        "UPDATE training_datasets SET state = 'train' WHERE id = $1", dataset_id)
    assert await db.fetchval(
        "SELECT state FROM training_datasets WHERE id = $1", dataset_id) == "train"


# ─────────────────────────────────────────────────────────────────────────────
# The determinations engineering may not make
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["unknown", "review_required",
                                   "known_restricted"])
async def test_a_dataset_without_an_approved_licence_cannot_be_promoted(db, state):
    """A dataset has terms too, and *"we found it on the internet"* is not one.
    The same four states as `068`, for the same reason."""
    dataset_id = await _dataset(db)
    await _all_checks(db, dataset_id)
    await db.execute(
        "UPDATE training_datasets SET lawful_basis = $2, lawful_basis_by = $3, "
        "  lawful_basis_at = NOW(), contains_personal_data = 'no' WHERE id = $1",
        dataset_id, FIXTURE_BASIS, A_PERSON)
    if state == "known_restricted":
        await db.execute(
            "UPDATE training_datasets SET licence_classification = $2, "
            "  licence_classified_by = $3, licence_classified_at = NOW() "
            "WHERE id = $1", dataset_id, state, A_PERSON)
    else:
        await db.execute(
            "UPDATE training_datasets SET licence_classification = $2 WHERE id = $1",
            dataset_id, state)

    with pytest.raises(asyncpg.CheckViolationError,
                       match=f"its licence is {state}"):
        await db.execute(
            "UPDATE training_datasets SET state = 'reviewed' WHERE id = $1",
            dataset_id)


@pytest.mark.parametrize("process", [
    "migration", "automation", "system", "script", "collector", "engineering",
    "ira", "bot", "ci", "pipeline", "operator", "postgres", "unknown", "tbd",
    "ira_migrator", "ira_app", "MIGRATION", "  Automation  ", "", "ab",
])
async def test_a_dataset_licence_approved_by_a_process_is_refused(db, process):
    """`068` refuses this for model licences. A dataset licence is the same kind
    of claim, and a pipeline that could approve its own training data would have
    defeated both."""
    dataset_id = await _dataset(db)
    with pytest.raises(asyncpg.PostgresError,
                       match="dataset_licence_determination_is_attributed"):
        await db.execute(
            "UPDATE training_datasets SET licence_classification = 'known_approved', "
            "  licence_classified_by = $2, licence_classified_at = NOW() "
            "WHERE id = $1", dataset_id, process)


async def test_an_undetermined_licence_may_not_name_a_classifier(db):
    """`review_required` with a name attached reads as though somebody had
    reviewed it. The same rule `068` applies to the model register."""
    dataset_id = await _dataset(db)
    with pytest.raises(asyncpg.PostgresError,
                       match="undetermined_dataset_licence_answers_nothing"):
        await db.execute(
            "UPDATE training_datasets SET licence_classified_by = $2, "
            "  licence_classified_at = NOW() WHERE id = $1", dataset_id, A_PERSON)


async def test_a_dataset_without_a_lawful_basis_cannot_be_promoted(db):
    """**PP-05.** Why IRA may train on this data is a decision with a named
    author, and no engineering artefact may supply it."""
    dataset_id = await _dataset(db)
    await _all_checks(db, dataset_id)
    await db.execute(
        "UPDATE training_datasets SET licence_classification = 'known_approved', "
        "  licence_classified_by = $2, licence_classified_at = NOW(), "
        "  contains_personal_data = 'no' WHERE id = $1", dataset_id, A_PERSON)
    with pytest.raises(asyncpg.CheckViolationError,
                       match="no lawful basis is recorded"):
        await db.execute(
            "UPDATE training_datasets SET state = 'reviewed' WHERE id = $1",
            dataset_id)


@pytest.mark.parametrize("author", ["migration", "automation", "script", "",
                                    "ab", "ira_app"])
async def test_a_lawful_basis_attributed_to_a_process_is_refused(db, author):
    dataset_id = await _dataset(db)
    with pytest.raises(asyncpg.PostgresError, match="lawful_basis_is_attributed"):
        await db.execute(
            "UPDATE training_datasets SET lawful_basis = $2, lawful_basis_by = $3, "
            "  lawful_basis_at = NOW() WHERE id = $1",
            dataset_id, FIXTURE_BASIS, author)


async def test_a_lawful_basis_without_an_author_is_refused(db):
    """Recorded together or not at all. A basis with no author is a sentence
    somebody typed, and it would satisfy the promotion gate's non-empty test."""
    dataset_id = await _dataset(db)
    with pytest.raises(asyncpg.PostgresError, match="lawful_basis_is_attributed"):
        await db.execute(
            "UPDATE training_datasets SET lawful_basis = $2 WHERE id = $1",
            dataset_id, FIXTURE_BASIS)


async def test_the_personal_data_question_must_be_answered_by_a_human(db):
    """**A clean PII scan is evidence, not an answer.**

    The scanner finds what its patterns match. Free-text names, pseudonymous
    handles and re-identifiable combinations are personal data that no regex
    catches, so `contains_personal_data` is asked separately and defaults to
    `unknown`.
    """
    dataset_id = await _dataset(db)
    await _all_checks(db, dataset_id)
    await db.execute(
        "UPDATE training_datasets SET licence_classification = 'known_approved', "
        "  licence_classified_by = $2, licence_classified_at = NOW(), "
        "  lawful_basis = $3, lawful_basis_by = $2, lawful_basis_at = NOW() "
        "WHERE id = $1", dataset_id, A_PERSON, FIXTURE_BASIS)
    assert await db.fetchval(
        "SELECT contains_personal_data FROM training_datasets WHERE id = $1",
        dataset_id) == "unknown"
    with pytest.raises(asyncpg.CheckViolationError,
                       match="contains personal data"):
        await db.execute(
            "UPDATE training_datasets SET state = 'reviewed' WHERE id = $1",
            dataset_id)


async def test_a_dataset_declaring_personal_data_may_still_be_promoted(db):
    """`yes` is an answer, not a refusal.

    Personal data with a lawful basis is lawful; the control is that somebody
    said so. A gate that refused `yes` outright would push people to record
    `no`, which is the one outcome that must never be convenient.
    """
    dataset_id = await _fully_governed(db)
    await db.execute(
        "UPDATE training_datasets SET contains_personal_data = 'yes' WHERE id = $1",
        dataset_id)
    await db.execute(
        "UPDATE training_datasets SET state = 'train' WHERE id = $1", dataset_id)
    assert await db.fetchval(
        "SELECT state FROM training_datasets WHERE id = $1", dataset_id) == "train"


# ─────────────────────────────────────────────────────────────────────────────
# Check records, shape and privilege
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_passing_check_cannot_report_findings(db):
    """A pass with findings is the shape a scanner that "handled" what it found
    would produce, and it is the one record that must never exist."""
    dataset_id = await _dataset(db)
    with pytest.raises(asyncpg.PostgresError, match="a_pass_found_nothing"):
        await _check(db, dataset_id, "pii", outcome="passed", findings=3)


async def test_a_failing_check_must_report_findings(db):
    """The mirror. A failure it cannot substantiate is a failure nobody can act
    on, and would let a broken scanner quarantine everything."""
    dataset_id = await _dataset(db)
    with pytest.raises(asyncpg.PostgresError, match="a_failure_found_something"):
        await _check(db, dataset_id, "pii", outcome="failed", findings=0)


@pytest.mark.parametrize("kind", ["", "vibes", "manual", "eyeball", "PII"])
async def test_an_unrecognised_check_kind_is_refused(db, kind):
    """A check nobody wrote a scanner for cannot be recorded into existence.
    `PII` is included because a case mismatch would silently satisfy nothing —
    the promotion gate looks for `pii`."""
    dataset_id = await _dataset(db)
    with pytest.raises(asyncpg.PostgresError):
        await _check(db, dataset_id, kind)


async def test_a_check_must_name_its_tool_and_version(db):
    """A pass by a tool nobody can identify is not reproducible. P6-04 made the
    same demand of a benchmark score with no method."""
    dataset_id = await _dataset(db)
    for tool, version in (("", "1.0"), ("scanner", "")):
        savepoint = db.transaction()
        await savepoint.start()
        with pytest.raises(asyncpg.PostgresError):
            await db.execute(
                "INSERT INTO dataset_checks (dataset_id, check_kind, "
                "  content_sha256, outcome, tool, tool_version) "
                "VALUES ($1,'pii',$2,'passed',$3,$4)",
                dataset_id, DIGEST, tool, version)
        await savepoint.rollback()


@pytest.mark.parametrize("digest", ["abc", "sha256:xyz", "sha256:" + "a" * 63,
                                    "a" * 64, ""])
async def test_a_malformed_content_digest_is_refused(db, digest):
    """A truncated or placeholder digest binds a check to nothing while looking
    like it binds it to something."""
    with pytest.raises(asyncpg.PostgresError):
        await _dataset(db, content_sha256=digest)


async def test_a_shredded_dataset_holds_no_records(db):
    """Shredding destroys content and keeps the row, because erasure needs the
    record that the dataset existed. A shredded row still claiming ten thousand
    records is claiming the content is there."""
    dataset_id = await _dataset(db, record_count=10)
    with pytest.raises(asyncpg.PostgresError,
                       match="shredded_datasets_are_not_usable"):
        await db.execute(
            "UPDATE training_datasets SET state = 'shredded' WHERE id = $1",
            dataset_id)


async def test_two_datasets_cannot_share_a_name_and_version(db):
    name = _name()
    await _dataset(db, name=name, version=1)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _dataset(db, name=name, version=1)


async def test_checks_are_removed_with_their_dataset(db):
    """ON DELETE CASCADE. An orphaned check row would be a pass belonging to no
    dataset, and the next dataset to reuse the id would inherit it."""
    dataset_id = await _dataset(db)
    await _all_checks(db, dataset_id)
    await db.execute("DELETE FROM training_datasets WHERE id = $1", dataset_id)
    assert await db.fetchval(
        "SELECT count(*) FROM dataset_checks WHERE dataset_id = $1",
        dataset_id) == 0


@pytest.mark.parametrize("table", ["training_datasets", "dataset_checks"])
@pytest.mark.parametrize("privilege", ["INSERT", "UPDATE", "DELETE", "TRUNCATE"])
async def test_the_runtime_role_cannot_write_the_governance_tables(
        db, table, privilege):
    """A process that answers requests could otherwise certify its own training
    data, or record a check that never ran.

    `042`'s ALTER DEFAULT PRIVILEGES grants `ira_app` full DML on every new
    table owned by `ira_schema_owner`, so `070`'s `GRANT SELECT` grants nothing
    by itself — P5-01 shipped exactly that mistake on `067`. The explicit
    REVOKEs are what make this pass.
    """
    assert await db.fetchval(
        "SELECT has_table_privilege('ira_app', $1, $2)", table,
        privilege) is False


@pytest.mark.parametrize("table", ["training_datasets", "dataset_checks"])
async def test_the_runtime_role_can_read_them(db, table):
    assert await db.fetchval(
        "SELECT has_table_privilege('ira_app', $1, 'SELECT')", table) is True


@pytest.mark.parametrize("function", [
    "training_dataset_promotion_gate", "training_dataset_content_changed",
])
async def test_the_gate_functions_pin_their_search_path(db, function):
    """R-45 found twelve functions with an unpinned `search_path`, two of them
    security guards. A session that put its own `dataset_checks` earlier on the
    path would have the gate read a table it controls."""
    config = await db.fetchval(
        "SELECT proconfig FROM pg_proc WHERE proname = $1", function)
    assert config and any(c.startswith("search_path=") for c in config), (
        f"{function} does not pin its search_path")


@pytest.mark.parametrize("trigger", ["training_dataset_promotion",
                                     "training_dataset_content_change"])
async def test_the_triggers_exist_and_are_enabled(db, trigger):
    """A disabled trigger is invisible in the schema dump everyone reads.
    `tgenabled` is `"char"` and asyncpg returns bytes."""
    row = await db.fetchrow(
        "SELECT tgenabled FROM pg_trigger WHERE tgname = $1 AND NOT tgisinternal",
        trigger)
    assert row is not None, f"{trigger} does not exist"
    assert row["tgenabled"] in (b"O", "O"), (
        f"{trigger} exists but is disabled, so it enforces nothing")


async def test_the_two_triggers_fire_in_the_order_the_migration_relies_on(db):
    """**A dependency on PostgreSQL's naming rule, made explicit.**

    Same-timing row triggers fire in NAME order.
    `training_dataset_content_change` must run before
    `training_dataset_promotion`, so the demotion sets `state = 'raw'` before
    the gate inspects `NEW.state` — otherwise a content change on a promoted
    dataset would be REFUSED rather than demoted, and the migration's comment
    would be wrong.

    Asserted because renaming either trigger would silently invert it, and the
    symptom would be an error message about checks rather than a demotion.
    """
    names = [r["tgname"] for r in await db.fetch(
        "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE c.relname = 'training_datasets' AND NOT t.tgisinternal "
        "ORDER BY t.tgname")]
    assert names.index("training_dataset_content_change") < \
        names.index("training_dataset_promotion"), (
        f"the triggers now fire in the order {names}, so the promotion gate "
        f"sees the old state and refuses a content change instead of demoting "
        f"it")


async def test_no_real_dataset_carries_a_determination(db):
    """**The honesty check.**

    PP-05: no corpus exists, and no lawful basis has been determined. If a
    committed row ever carries one, either the owner recorded it — in which case
    PP-05 closes and this test is updated deliberately — or an engineering
    artefact recorded it, which is the thing Phase 7 must not do.

    Fixtures are exempt by naming themselves, the same rule P5-01 applied to
    `commercial_licence`.
    """
    rows = await db.fetch(
        "SELECT name, licence_classified_by, lawful_basis_by "
        "  FROM training_datasets "
        " WHERE licence_classification IN ('known_approved', 'known_restricted') "
        "    OR lawful_basis IS NOT NULL")
    liars = [r["name"] for r in rows
             if "fixture" not in ((r["licence_classified_by"] or "")
                                  + (r["lawful_basis_by"] or "")).lower()]
    assert not liars, (
        f"datasets {liars} carry a licence determination or a lawful basis "
        f"recorded by something that does not name itself a fixture. PP-05 is "
        f"an owner decision.")
