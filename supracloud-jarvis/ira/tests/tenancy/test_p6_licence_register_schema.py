"""Phase 6 batch 01 — what migration 068 guarantees, asked of the live schema.

MOD-01 is *"the licence position of every configured model is unrecorded"*, and
`067` answered it with

    CONSTRAINT serving_is_licensed CHECK (
        status <> 'serving'
        OR (commercial_licence IS NOT NULL AND licence_recorded_at IS NOT NULL))

which `IMPLEMENTED_VS_PLANNED.md` described as *"the field is a gate, not an
answer"*. It is not a gate. `commercial_licence` is free text, so a single
character satisfies it — and the first test below is the demonstration, run
against the deployed schema rather than argued.

That is the defect `068` closes: not an empty field, but a control that reads
like one and is not.

── The line, and where each half is enforced ──────────────────────────────

    Engineering may record what a publisher SAYS.
    Only a named human may record what it MEANS for IRA.

`068` enforces the second half in three places, and each is tested separately
because each fails differently:

  * `undetermined_rows_answer_nothing` — a row engineering may write carries no
    answers, so a collector cannot make the determination in all but name.
  * `determination_names_a_human` — an approval attributed to a process is
    rejected, so no migration can seed one.
  * the `model_registry_licence_gate` trigger — serving requires an approved
    row, so an unread licence fails closed at the point of use.

── Why a database test rather than a file test ────────────────────────────

Reading the SQL and finding the word CHECK proves somebody typed a constraint,
not that PostgreSQL enforces one. Every assertion below writes a row and watches
it be refused. The rollback and the destructive cases run in CI on a disposable
database, for the reason P5-01 recorded: a destructive rollback test must never
be able to reach the development database.
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

HASH = "sha256:" + "a" * 64
DIGEST = "sha256:" + "b" * 64
OTHER_DIGEST = "sha256:" + "c" * 64

#: A name that satisfies `determination_names_a_human`. Not a real person and
#: not presented as one — every use of it is inside a transaction that is rolled
#: back, and nothing it writes reaches a committed row.
A_PERSON = "Test Verifier"


@pytest.fixture
async def db():
    """A connection in a transaction that is ALWAYS rolled back.

    The register is platform configuration on a shared development database. A
    test that committed an approval would be a committed licence determination
    — which is the one thing this whole phase exists to prevent — and it would
    also let `model_registry_one_serving_version` fail somebody else's run.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


def _now() -> dt.datetime:
    """A bound datetime, not the string ``NOW()``.

    asyncpg binds parameters rather than interpolating them, so `'NOW()'`
    arrives as a five-character string and is rejected. The first draft of this
    file did that in nine tests.
    """
    return dt.datetime.now(dt.timezone.utc)


def _ident() -> str:
    return f"p6/{uuid.uuid4().hex[:12]}"


async def _evidence(db, **kw):
    """A licence row in a state ENGINEERING is allowed to create."""
    fields = {
        "source": "ollama", "identifier": _ident(), "reference": "14b",
        "licence_identifier": "Apache License", "artifact_digest": DIGEST,
        "evidence_url": "https://registry.ollama.ai/v2/library/x/manifests/14b",
        "evidence_method": "ollama-registry-manifest",
        "classification": "review_required",
    }
    fields.update(kw)
    cols = ", ".join(fields)
    args = ", ".join(f"${i}" for i in range(1, len(fields) + 1))
    return await db.fetchval(
        f"INSERT INTO model_licence_register ({cols}, evidence_retrieved_at) "
        f"VALUES ({args}, NOW()) RETURNING id", *fields.values())


async def _approve(db, licence_id, by=A_PERSON, **kw):
    """What only a named human may do."""
    sets = {
        "classification": "known_approved", "classified_by": by,
        "determination_note": "read the licence text shipped with the artefact",
        "commercial_use": "permitted", "hosted_saas_redistribution": "permitted",
    }
    sets.update(kw)
    assign = ", ".join(f"{c} = ${i}" for i, c in enumerate(sets, start=1))
    return await db.execute(
        f"UPDATE model_licence_register SET {assign}, classified_at = NOW() "
        f"WHERE id = ${len(sets) + 1}", *sets.values(), licence_id)


async def _register(db, licence_id=None, **kw):
    fields = {
        "model_key": f"m{uuid.uuid4().hex[:12]}", "version": 1,
        "base_model": "org/model", "revision": "9f8e7d6c",
        "artifact_hash": HASH, "environment": "development",
    }
    if licence_id is not None:
        fields["licence_id"] = licence_id
    fields.update(kw)
    cols = ", ".join(fields)
    args = ", ".join(f"${i}" for i in range(1, len(fields) + 1))
    return await db.fetchval(
        f"INSERT INTO model_registry ({cols}) VALUES ({args}) RETURNING id",
        *fields.values())


# ─────────────────────────────────────────────────────────────────────────────
# The defect 068 exists for
# ─────────────────────────────────────────────────────────────────────────────


async def test_067s_free_text_licence_field_still_accepts_anything(db):
    """**The finding, demonstrated rather than asserted.**

    `serving_is_licensed` is satisfied by the string `x`. This test does NOT
    fail — it passes, and what it proves is that `067`'s constraint could never
    have established a licence position. It is kept green deliberately so that
    the reason `068` exists cannot be argued away later, and so that anyone
    tightening `commercial_licence` itself finds this test and reads why the
    tightening went somewhere else.

    The row it writes cannot serve, because the trigger `068` adds refuses it —
    which is the next test.
    """
    await _register(db, commercial_licence="x", licence_recorded_at=_now())
    # No exception. A one-character legal determination.


async def test_a_model_cannot_serve_without_a_licence_register_row(db):
    """The gate, at the point it matters. Free text is no longer enough.

    `CheckViolationError` rather than `RaiseError`: the trigger raises with
    `ERRCODE = 'check_violation'` deliberately, so that a caller catching
    integrity errors catches this too. A plpgsql `RAISE` with no ERRCODE would
    arrive as P0001 and slip past every such handler — which is how a fail-closed
    gate becomes a 500 nobody classifies.
    """
    with pytest.raises(asyncpg.CheckViolationError, match="no licence register row"):
        await _register(db, status="serving", rollout_percentage=100,
                        commercial_licence="x", licence_recorded_at=_now())


@pytest.mark.parametrize("state", ["unknown", "review_required"])
async def test_an_undetermined_licence_cannot_serve(db, state):
    """**Fail safely when the licence authority is missing.**

    `unknown` is `k2-fsa/OmniVoice` — an artefact IRA already fetches, whose
    publisher declares no licence at all. `review_required` is every other model
    configured today. Neither may answer a paying tenant's request, and the
    refusal happens at write time rather than being left to a convention.
    """
    licence = await _evidence(db, classification=state,
                              licence_identifier=None if state == "unknown" else "MIT License")
    with pytest.raises(asyncpg.CheckViolationError, match=f"its licence is {state}"):
        await _register(db, licence_id=licence, status="serving",
                        rollout_percentage=100, commercial_licence="x",
                        licence_recorded_at=_now())


async def test_a_restricted_licence_cannot_serve_either(db):
    """`known_restricted` is a determination, and a negative one. A gate that
    only checked "has somebody looked at this" would let it through."""
    licence = await _evidence(db)
    await _approve(db, licence, classification="known_restricted",
                   commercial_use="prohibited",
                   hosted_saas_redistribution="prohibited")
    with pytest.raises(asyncpg.CheckViolationError, match="its licence is known_restricted"):
        await _register(db, licence_id=licence, status="serving",
                        rollout_percentage=100, commercial_licence="x",
                        licence_recorded_at=_now())


async def test_an_approved_licence_lets_a_model_serve(db):
    """The positive case. Without it the suite would pass against a trigger that
    refused everything, which is a different broken system."""
    licence = await _evidence(db)
    await _approve(db, licence)
    model = await _register(db, licence_id=licence, status="serving",
                            rollout_percentage=100, commercial_licence="Apache",
                            licence_recorded_at=_now())
    assert await db.fetchval(
        "SELECT status FROM model_registry WHERE id = $1", model) == "serving"


# ─────────────────────────────────────────────────────────────────────────────
# Engineering may not make the determination
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("process", [
    "migration", "automation", "system", "script", "collector", "engineering",
    "ira", "bot", "ci", "pipeline", "admin", "operator", "postgres", "unknown",
    "n/a", "tbd", "todo", "none", "anonymous", "placeholder", "agent",
    "assistant", "ira_migrator", "ira_app", "MIGRATION", "  Automation  ",
])
async def test_an_approval_attributed_to_a_process_is_refused(db, process):
    """**`OPEN_DECISIONS.md` D-08, as a constraint.**

    *"Owner action: confirm who verifies licence terms and records them."* Until
    that person exists, nothing may occupy the field on their behalf — and the
    values something automating its way past this table would reach for are
    exactly these. Case and surrounding whitespace are included because
    `Migration` and `migration ` are the same author.
    """
    licence = await _evidence(db)
    with pytest.raises(asyncpg.PostgresError, match="determination_names_a_human"):
        await _approve(db, licence, by=process)


@pytest.mark.parametrize("by", ["", "   ", "ab", "x"])
async def test_an_approval_with_no_real_name_is_refused(db, by):
    """A deny-list alone is defeated by one character. The length floor is what
    stops `-` and `x` from being a verifier."""
    licence = await _evidence(db)
    with pytest.raises(asyncpg.PostgresError,
                       match="determination_names_a_human|determination_is_attributed"):
        await _approve(db, licence, by=by)


async def test_an_approval_with_no_reason_is_refused(db):
    """An approval with no recorded reasoning cannot be re-checked, so it cannot
    be distinguished from a mistake by anyone who did not make it."""
    licence = await _evidence(db)
    with pytest.raises(asyncpg.PostgresError, match="determination_is_attributed"):
        await _approve(db, licence, determination_note="")


@pytest.mark.parametrize("column", [
    "commercial_use", "hosted_saas_redistribution", "derivative_distribution",
])
async def test_an_engineering_state_may_not_answer_the_questions(db, column):
    """**"Do not convert unknown into approved" — including by halves.**

    A collector that set `commercial_use = 'permitted'` while leaving the row
    `review_required` would have made the determination in everything but name,
    and a reader scanning that column would never notice the classification
    disagreed with it. Making the combination unrepresentable is better than
    trusting the collector.
    """
    licence = await _evidence(db)
    with pytest.raises(asyncpg.PostgresError,
                       match="undetermined_rows_answer_nothing"):
        await db.execute(
            f"UPDATE model_licence_register SET {column} = 'permitted' "
            f"WHERE id = $1", licence)


async def test_an_engineering_state_may_not_name_a_classifier(db):
    """The other half of the same rule: `review_required` with a name attached
    reads as though somebody had reviewed it."""
    licence = await _evidence(db)
    with pytest.raises(asyncpg.PostgresError,
                       match="undetermined_rows_answer_nothing"):
        await db.execute(
            "UPDATE model_licence_register SET classified_by = $2, "
            "classified_at = NOW() WHERE id = $1", licence, A_PERSON)


async def test_an_approval_must_answer_the_two_commercial_questions(db):
    """`MODEL_LICENSE_REGISTER.md`: *"'Open weights' and 'permitted for
    commercial SaaS' are different questions, and only the second one matters
    for a paid subscription product."* An approval that leaves either at
    `unknown` has not answered it."""
    licence = await _evidence(db)
    with pytest.raises(asyncpg.PostgresError,
                       match="approval_answers_the_commercial_questions"):
        await _approve(db, licence, hosted_saas_redistribution="unknown")


async def test_a_licence_with_no_evidence_cannot_be_approved(db):
    """**L-03, as a constraint.**

    `k2-fsa/OmniVoice` declares no licence: no identifier, no text. IRA fetches
    it today. This constraint means the only route to approving it is to first
    RECORD terms obtained from the publisher — a real act — rather than to
    change a word in a column because the neighbouring rows are permissive.
    """
    licence = await _evidence(db, classification="unknown",
                              licence_identifier=None)
    with pytest.raises(asyncpg.PostgresError, match="approval_rests_on_evidence"):
        await _approve(db, licence)


async def test_an_approval_must_bind_to_an_immutable_artefact(db):
    """`qwen3:14b` is a moving target: the publisher can push new weights to
    that tag tomorrow. An approval recorded against the tag would silently
    transfer to an artefact nobody read the licence of — the MOD-02
    substitution shape, arriving through the licence door."""
    licence = await _evidence(db, artifact_digest=None)
    with pytest.raises(asyncpg.PostgresError,
                       match="approval_is_bound_to_an_immutable_artefact"):
        await _approve(db, licence)


async def test_evidence_is_frozen_while_a_row_is_approved(db):
    """An approval that can be re-pointed at different evidence approves
    nothing. The withdrawal path is the way out, and it is deliberately not
    silent."""
    licence = await _evidence(db)
    await _approve(db, licence)
    # A savepoint per attempt. The first refusal aborts the enclosing
    # transaction, so without one the second and third assertions would fail
    # with InFailedSQLTransactionError — a green-looking loop that only ever
    # tested its first entry, and a red one that hid which column was wrong.
    for column, value in (("artifact_digest", OTHER_DIGEST),
                          ("licence_identifier", "MIT License"),
                          ("identifier", "someone/else")):
        savepoint = db.transaction()
        await savepoint.start()
        with pytest.raises(asyncpg.CheckViolationError,
                           match="evidence cannot change while the row is approved"):
            await db.execute(
                f"UPDATE model_licence_register SET {column} = $2 WHERE id = $1",
                licence, value)
        await savepoint.rollback()


# ─────────────────────────────────────────────────────────────────────────────
# Withdrawal
# ─────────────────────────────────────────────────────────────────────────────


async def test_withdrawing_an_approval_stops_the_model_serving(db):
    """**The direction that is easy to get backwards.**

    Refusing the withdrawal while a model serves would keep the model serving
    under an approval somebody is actively trying to take back. So a withdrawal
    retires the versions that relied on it — and writes the reason into `notes`,
    because a status that changes underneath an operator with no record is the
    silent-failure class this repository keeps producing.
    """
    licence = await _evidence(db)
    await _approve(db, licence)
    model = await _register(db, licence_id=licence, status="serving",
                            rollout_percentage=100, commercial_licence="Apache",
                            licence_recorded_at=_now())

    await db.execute(
        "UPDATE model_licence_register SET classification = 'known_restricted', "
        "determination_note = 'the use policy conflicts with hosted resale' "
        "WHERE id = $1", licence)

    row = await db.fetchrow(
        "SELECT status, notes FROM model_registry WHERE id = $1", model)
    assert row["status"] == "retired", (
        "withdrawing the licence approval left the model serving under an "
        "approval that has been taken back")
    assert "licence approval withdrawn" in row["notes"], (
        "the model was retired with no record of why, so an operator sees a "
        "model that stopped serving for no visible reason")


async def test_withdrawal_leaves_versions_that_were_not_serving_alone(db):
    """A withdrawal is not a purge. Retiring a registered-but-not-serving
    version would destroy the rollback target of a model that is fine."""
    licence = await _evidence(db)
    await _approve(db, licence)
    idle = await _register(db, licence_id=licence, status="registered")
    await db.execute(
        "UPDATE model_licence_register SET classification = 'review_required', "
        "classified_by = NULL, classified_at = NULL, determination_note = '', "
        "commercial_use = 'unknown', hosted_saas_redistribution = 'unknown' "
        "WHERE id = $1", licence)
    assert await db.fetchval(
        "SELECT status FROM model_registry WHERE id = $1", idle) == "registered"


async def test_a_licence_row_cannot_be_deleted_while_a_model_references_it(db):
    """ON DELETE RESTRICT. Deleting the row would leave a serving model with no
    record of what permitted it, which is worse than the state before 068."""
    licence = await _evidence(db)
    await _approve(db, licence)
    await _register(db, licence_id=licence, status="serving",
                    rollout_percentage=100, commercial_licence="Apache",
                    licence_recorded_at=_now())
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.execute("DELETE FROM model_licence_register WHERE id = $1",
                         licence)


# ─────────────────────────────────────────────────────────────────────────────
# Shape, provenance and privilege
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("column", [
    "source", "identifier", "reference", "licence_identifier",
    "licence_text_sha256", "artifact_digest", "evidence_url",
    "evidence_method", "evidence_retrieved_at", "classification",
    "commercial_use", "hosted_saas_redistribution", "derivative_distribution",
    "attribution_required", "output_restrictions", "classified_by",
    "classified_at", "determination_note",
])
async def test_the_register_carries_every_field_the_document_requires(db, column):
    """`MODEL_LICENSE_REGISTER.md` §"Required per row before a model may serve a
    paying tenant" lists what has to be recorded. A register missing one of them
    cannot answer the question it exists for."""
    assert await db.fetchval(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'model_licence_register' AND column_name = $1",
        column) == 1, f"model_licence_register has no {column} column"


async def test_evidence_must_say_where_it_came_from(db):
    """Provenance is what separates a recorded licence from a remembered one.
    P5-03 shipped three invented commit hashes described as resolved; a licence
    identifier with no URL and no timestamp is the same artefact."""
    with pytest.raises(asyncpg.PostgresError):
        await _evidence(db, evidence_url="")


@pytest.mark.parametrize("method", ["guess", "memory", "", "web-search"])
async def test_an_unrecognised_evidence_method_is_refused(db, method):
    """A source nobody wrote a collector for cannot be recorded into existence.
    The vocabulary is the audit trail: `owner-supplied` means a person typed it,
    and that is visibly different from `ollama-registry-manifest`."""
    with pytest.raises(asyncpg.PostgresError):
        await _evidence(db, evidence_method=method)


@pytest.mark.parametrize("state", ["approved", "ok", "fine", "yes", "",
                                   "KNOWN_APPROVED", "probably_fine"])
async def test_an_invented_classification_is_refused(db, state):
    """Four states, and no fifth. `probably_fine` is the one that would be
    invented, and it would read as an approval to every consumer."""
    with pytest.raises(asyncpg.PostgresError):
        await _evidence(db, classification=state)


@pytest.mark.parametrize("digest", [
    "abc", "sha256:xyz", "sha256:" + "a" * 63, "a" * 64, "a" * 39, "a" * 41,
    # The three that matter: a TAG is not an immutable reference, and every one
    # of these is what somebody would reach for when the digest is inconvenient.
    "14b", "main", "latest",
])
async def test_a_mutable_or_malformed_artefact_reference_is_refused(db, digest):
    """Not cryptography — arithmetic. A truncated or placeholder digest binds an
    approval to nothing while looking like it binds it to something, and a tag
    binds it to whatever the publisher pushes there next."""
    with pytest.raises(asyncpg.PostgresError,
                       match="digest_is_an_immutable_reference"):
        await _evidence(db, artifact_digest=digest)


@pytest.mark.parametrize("digest", ["sha256:" + "a" * 64, "b" * 40])
async def test_both_immutable_reference_shapes_are_accepted(db, digest):
    """An Ollama layer digest and a Hugging Face commit sha.

    Without this, the constraint could tighten to sha256-only and the failure
    would be invisible: every Hugging Face artefact would become permanently
    unapprovable, and the symptom would be a licence that "just cannot be
    recorded" rather than an error anybody could act on.
    """
    assert await _evidence(db, artifact_digest=digest) is not None


async def test_the_same_artefact_reference_cannot_be_registered_twice(db):
    """Two licence rows for one artefact means two answers, and a consumer
    picking whichever it read first."""
    ident = _ident()
    await _evidence(db, identifier=ident, reference="14b")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _evidence(db, identifier=ident, reference="14b")


@pytest.mark.parametrize("privilege", ["INSERT", "UPDATE", "DELETE", "TRUNCATE"])
async def test_the_runtime_role_cannot_write_the_licence_register(db, privilege):
    """**The one that matters most on this table.**

    A process that answers requests and can UPDATE this table can approve its
    own licence. `042` sets ALTER DEFAULT PRIVILEGES granting `ira_app` full DML
    on every new table owned by `ira_schema_owner`, so `068`'s `GRANT SELECT`
    grants nothing by itself — P5-01 shipped exactly that mistake on `067`. The
    explicit REVOKEs are what make this assertion pass, and this assertion is
    what proves they were not forgotten.
    """
    assert await db.fetchval(
        "SELECT has_table_privilege('ira_app', 'model_licence_register', $1)",
        privilege) is False, (
        f"ira_app holds {privilege} on model_licence_register — the process "
        f"that serves requests can record its own licence approval")


async def test_the_runtime_role_can_read_it(db):
    """The gateway has to be able to check. A REVOKE that took SELECT too would
    fail closed, which is safe and useless."""
    assert await db.fetchval(
        "SELECT has_table_privilege('ira_app', 'model_licence_register', "
        "'SELECT')") is True


@pytest.mark.parametrize("function", [
    "model_registry_requires_approved_licence",
    "model_licence_register_withdrawal",
])
async def test_the_gate_functions_pin_their_search_path(db, function):
    """R-45 found twelve functions with an unpinned `search_path`, including two
    security guards. A guard a caller can redirect is not a guard: a session
    that puts its own `model_licence_register` earlier on the path would have
    the trigger read a table it controls."""
    config = await db.fetchval(
        "SELECT proconfig FROM pg_proc WHERE proname = $1", function)
    assert config and any(c.startswith("search_path=") for c in config), (
        f"{function} does not pin its search_path")


@pytest.mark.parametrize("trigger,table", [
    ("model_registry_licence_gate", "model_registry"),
    ("model_licence_register_withdrawal_gate", "model_licence_register"),
])
async def test_the_triggers_exist_and_are_enabled(db, trigger, table):
    """A disabled trigger is invisible in the schema dump everyone reads.

    `tgenabled` is `"char"` and asyncpg returns it as bytes — asserted against
    `b"O"` rather than `"O"` for that reason, which P4 learned the slow way.
    """
    row = await db.fetchrow(
        "SELECT t.tgenabled FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE t.tgname = $1 AND c.relname = $2 AND NOT t.tgisinternal",
        trigger, table)
    assert row is not None, f"{trigger} does not exist on {table}"
    assert row["tgenabled"] in (b"O", "O"), (
        f"{trigger} exists but is disabled, so it enforces nothing")
