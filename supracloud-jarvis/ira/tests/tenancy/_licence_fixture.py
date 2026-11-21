"""An approved licence row, for tests that need a model to actually serve.

Phase 6's `068` makes `model_registry.status = 'serving'` require a
`model_licence_register` row classified `known_approved`. That is the point of
the migration, and it means every Phase 5 test that put a model into service now
needs one — twenty-three of them failed the moment `068` applied, which is the
gate working rather than a regression.

── Why a shared helper rather than a copy in each file ────────────────────

Three call sites need this: the registry schema suite, the registry service
suite, and the staging end-to-end script. Three copies of "insert an approval"
is three places for the fixture text to drift, and the fixture text is the thing
that has to stay unmistakably a fixture.

── This is a fixture, and it says so in every column that a human reads ───

An approval is a legal determination, and Phase 6 exists to stop engineering
making one. A test that needed to write `classified_by = 'Jane Smith'` would be
committing a fabricated verifier to the repository, and
`MODEL_LICENCE_DETERMINATIONS_REQUIRED.md` L-04 is precisely the open question
of who that person is.

So the values below name themselves. `068`'s `determination_names_a_human`
rejects the obvious process names (`migration`, `automation`, `test`, …), which
is deliberate — the constraint has to reject them, or a migration could seed an
approval. What it cannot do is distinguish a real person from a plausible one,
and nothing can. What the repository CAN do is make the fixture greppable and
assert on it: `test_p6_truth_pass.py` requires that every committed approval in
the tree is one of these, and that the strings say what they are.

`test_p5_model_registry_schema.py` already applies the same rule to `067`'s
`commercial_licence` column, and found nothing because the field was empty. This
keeps that test honest now that rows exist.
"""
from __future__ import annotations

import uuid

#: The verifier name every test approval carries. Long enough to satisfy the
#: length floor, and impossible to mistake for a person in a grep of the tree.
FIXTURE_VERIFIER = "TEST FIXTURE — not a licence determination"

#: The reason. `determination_is_attributed` requires a non-empty note, and an
#: empty-looking one would be the first thing a reader trusted.
FIXTURE_NOTE = (
    "Test fixture only. No licence text was read and no determination was "
    "made. See docs/cloud/MODEL_LICENCE_DETERMINATIONS_REQUIRED.md.")

#: The free-text field `067` carries. Kept consistent with the pre-068 fixtures.
FIXTURE_LICENCE = "test-fixture-not-a-determination"

_DIGEST = "sha256:" + "f" * 64


async def approved_licence(conn, *, identifier: str | None = None,
                           reference: str = "test", digest: str = _DIGEST) -> str:
    """Insert an approved licence row and return its id.

    Two statements rather than one: the INSERT lands in a state engineering is
    allowed to write (`review_required`, answering nothing), and the UPDATE
    performs the approval. That mirrors how a real approval happens, and it means
    a change to `undetermined_rows_answer_nothing` breaks here rather than
    silently letting fixtures take a shortcut the product does not have.
    """
    identifier = identifier or f"fixture/{uuid.uuid4().hex[:12]}"
    licence_id = await conn.fetchval(
        "INSERT INTO model_licence_register "
        "  (source, identifier, reference, licence_identifier, artifact_digest, "
        "   evidence_url, evidence_method, evidence_retrieved_at, classification) "
        "VALUES ('ollama', $1, $2, 'test-fixture-licence', $3, "
        "        'https://example.invalid/fixture', 'owner-supplied', NOW(), "
        "        'review_required') "
        "RETURNING id", identifier, reference, digest)
    await conn.execute(
        "UPDATE model_licence_register SET classification = 'known_approved', "
        "  classified_by = $2, classified_at = NOW(), determination_note = $3, "
        "  commercial_use = 'permitted', hosted_saas_redistribution = 'permitted' "
        "WHERE id = $1", licence_id, FIXTURE_VERIFIER, FIXTURE_NOTE)
    return licence_id


async def drop_licence(conn, licence_id) -> None:
    """Remove a fixture approval.

    Ordered after the models that reference it: `licence_id` is ON DELETE
    RESTRICT, so a teardown that ran this first would fail and leave both rows
    behind.
    """
    await conn.execute("DELETE FROM model_licence_register WHERE id = $1",
                       licence_id)
