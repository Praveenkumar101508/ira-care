"""P12-04 finding I — consent was recorded by nothing and checked by nothing.

`037` gave `voice_profiles` four columns in Phase 3B:

    consent_granted_at   consent_reference   revoked_at   revocation_reason

and until this batch **no Python read or wrote any of them**. Measured, not
assumed:

    grep -rn "consent_granted_at\\|consent_reference" --include=*.py .   -> 0 outside tests
    grep -n  "consent\\|revoked_at" voice/biometrics.py                  -> 0

So a voiceprint enrolled with no recorded consent authenticated normally, and
revoking consent had no effect on `_load_owner_profile` — the only path that
decides whether a voiceprint can identify somebody.

Severity, stated rather than implied: enrolment is owner-gated (`is_owner`,
403 otherwise) and this is a single-owner subsystem, so this is a
consent-enforcement gap and not a cross-tenant disclosure. P12-01 classified
`voice/biometrics.py` NOT_APPLICABLE for the workspace audit for the same
reason. That bound is why the batch is small; it is not a reason the gap was
acceptable.
"""
from __future__ import annotations

import base64
import datetime
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

#: An EXPLICIT KEK. `save_owner_profile` seals the template before it writes,
#: so these tests cannot exercise the real enrolment path without one.
_KEK = base64.b64encode(b"P" * 32).decode()
os.environ.setdefault("IRA_CONNECTOR_ENCRYPTION_KEY", _KEK)
os.environ.setdefault("IRA_CONNECTOR_ENCRYPTION_KEY_ID", "kek-p1204")

for _k, _v in {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-test",
    "REDIS_PASSWORD": "ci-test",
}.items():
    os.environ.setdefault(_k, _v)


@pytest.fixture
async def pool():
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=2)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


@pytest.fixture
async def admin():
    """A superuser connection, and a `voice_profiles` emptied around each test.

    Whole-table, not `WHERE owner_name = …`, for the reason
    `test_p4_voiceprint_encryption`'s fixture records: the paths under test
    select *the newest row visible to the owner*, so one row left behind by any
    other source is the row they read. Scoping the cleanup to this suite's own
    names would make that leftover invisible to the cleanup and decisive for the
    test.
    """
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute("DELETE FROM voice_profiles")
        yield conn
    finally:
        await conn.execute("DELETE FROM voice_profiles")
        await conn.close()


@pytest.fixture
def owner():
    """The `user_id` the policy checks against — derived, never stored."""
    from core.identity.db_context import voiceprint_owner_id

    return voiceprint_owner_id()


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


async def _profile(admin, owner, *, consent, revoked=None):
    """Insert a `voice_profiles` row in one of the four consent states.

    Seeded as a SUPERUSER on purpose: a fixture has to be able to create the
    very row the policy under test is about to filter, and `037`'s `WITH CHECK`
    would otherwise force the fixture to bind the same context the code under
    test binds — which would make the test pass by construction.
    """
    await admin.execute(
        "INSERT INTO voice_profiles (owner_name, user_id, embedding, "
        "                            consent_granted_at, consent_reference, "
        "                            revoked_at, revocation_reason) "
        "VALUES ($1, $2, '', $3, $4, $5, $6)",
        "p1204-fixture",
        owner,
        _now() if consent else None,
        "policy-v1" if consent else None,
        revoked,
        None if revoked is None else "withdrawn by the subject")


# ── The state machine the read path now consults ────────────────────────────


async def test_no_profile_is_not_the_same_as_no_consent(pool, admin, owner):
    from voice.biometrics import voice_consent_state

    assert await voice_consent_state() == "no_profile"


async def test_a_profile_without_recorded_consent_is_refused(pool, admin, owner):
    """The state finding I left reachable: enrolled, never consented."""
    await _profile(admin, owner, consent=None)
    from voice.biometrics import voice_consent_state

    assert await voice_consent_state() == "missing"


async def test_a_revoked_profile_is_refused(pool, admin, owner):
    await _profile(admin, owner, consent=True, revoked=_now())
    from voice.biometrics import voice_consent_state

    assert await voice_consent_state() == "revoked"


async def test_a_consented_profile_is_granted(pool, admin, owner):
    """The control. Without it, refusing everything would look like a fix."""
    await _profile(admin, owner, consent=True)
    from voice.biometrics import voice_consent_state

    assert await voice_consent_state() == "granted"


# ── Enforcement on the path that actually authenticates ─────────────────────


async def test_verification_refuses_a_revoked_voiceprint(pool, admin, owner, monkeypatch):
    """The whole finding, at the function that decides identity.

    The template is deliberately left INTACT and loadable: revocation is not
    erasure, and the refusal must come from consent rather than from the
    template being absent.
    """
    from voice import biometrics

    await _profile(admin, owner, consent=True, revoked=_now())

    async def compute_embedding(_audio):
        return [1.0, 0.0, 0.0, 0.0]

    async def load_owner_profile():
        return [1.0, 0.0, 0.0, 0.0]        # a perfect match, if it were used

    monkeypatch.setattr(biometrics, "compute_embedding", compute_embedding,
                        raising=False)
    monkeypatch.setattr(biometrics, "_load_owner_profile", load_owner_profile,
                        raising=False)

    ok = await biometrics.is_owner_authenticated(b"\0" * 32_000,
                                                 session_id="p1204-revoked")
    assert ok is False, (
        "a revoked voiceprint authenticated. The template matches perfectly, "
        "which is the point: the refusal has to come from consent."
    )


async def test_the_refusal_is_distinguishable_from_a_missing_model(pool, admin,
                                                                   owner,
                                                                   monkeypatch):
    """`is_owner_authenticated` returns False for BOTH, by design.

    It is best-effort: a missing model degrades voice to public access rather
    than crashing. So a consent refusal returning the same False down the same
    channel is indistinguishable from an outage — this phase's recurring
    failure mode, arriving in a new place. The audit row is what separates
    them.
    """
    from voice import biometrics

    await _profile(admin, owner, consent=True, revoked=_now())

    async def compute_embedding(_audio):
        return [1.0, 0.0, 0.0, 0.0]

    monkeypatch.setattr(biometrics, "compute_embedding", compute_embedding,
                        raising=False)

    session = f"p1204-{uuid.uuid4().hex[:8]}"
    await biometrics.is_owner_authenticated(b"\0" * 32_000, session_id=session)

    # Read as a superuser, deliberately. `_record_biometric_attempt` now
    # ATTRIBUTES the row to the subject, so `subject_or_tenant_select` hides it
    # from an unbound reader — which is the same predicate that made every row
    # this function used to write invisible to everybody. Reading it here
    # without a bound context would fail for the opposite reason to the one the
    # test is about.
    row = await admin.fetchrow(
        "SELECT result, failure_reason, user_id FROM biometric_audit "
        " WHERE session_id = $1 ORDER BY created_at DESC LIMIT 1", session)

    assert row is not None, (
        "the refused attempt left no audit row, so an operator cannot tell a "
        "revocation from a model outage"
    )
    assert row["result"] is False
    assert row["failure_reason"] == "revoked_enrolment", (
        f"the audit row says {row['failure_reason']!r}. A consent refusal and "
        f"a missing model must not be the same record."
    )
    assert row["user_id"] == owner, (
        "the audit row is unattributed. `subject_or_tenant_select` predicates "
        "on tenant_id OR user_id, so a NULL/NULL row is readable by nobody — "
        "including the person whose voice was refused."
    )


# ── Collection and revocation ───────────────────────────────────────────────


async def test_enrolment_requires_a_consent_reference(pool, admin, owner):
    """Keyword-only and required, so an unconsented enrolment is unexpressible."""
    from voice.biometrics import save_owner_profile

    with pytest.raises(TypeError):
        await save_owner_profile([[1.0, 0.0]])          # no consent_reference

    assert await save_owner_profile([[1.0, 0.0]], consent_reference="   ") is False, (
        "a blank consent reference was accepted; it records nothing"
    )


async def test_revocation_counts_the_rows_it_changed(pool, admin, owner):
    """A revocation matching nothing must not report success."""
    from voice.biometrics import revoke_voice_consent, voice_consent_state

    assert await revoke_voice_consent("no profile yet") is False

    await _profile(admin, owner, consent=True)
    assert await voice_consent_state() == "granted"

    assert await revoke_voice_consent("withdrawn by the subject") is True
    assert await voice_consent_state() == "revoked"

    # A second revocation changes nothing, and says so rather than repeating
    # a success the database did not perform.
    assert await revoke_voice_consent("again") is False


async def test_revocation_does_not_destroy_the_template(pool, admin, owner):
    """Revocation and erasure are different requests.

    Conflating them would make "stop using this" indistinguishable from
    "destroy the evidence", and erasure has its own path with its own
    reporting.
    """
    from voice.biometrics import revoke_voice_consent

    await _profile(admin, owner, consent=True)
    assert await revoke_voice_consent("withdrawn") is True

    row = await admin.fetchrow(
        "SELECT deletion_status, revocation_reason FROM voice_profiles "
        " WHERE user_id = $1", owner)

    assert row["deletion_status"] == "live", "revocation deleted the profile"
    assert row["revocation_reason"] == "withdrawn"


# ── FINDING J — the binding the whole subsystem was missing ─────────────────


async def test_enrolment_writes_a_row_at_all(pool, admin, owner):
    """The finding, at its simplest: enrolment had never once succeeded.

    `037` put `voice_profiles` under FORCE RLS with
    `WITH CHECK (user_id = app.user_id)` in Phase 3B. `save_owner_profile` bound
    nothing and wrote no `user_id`, so every INSERT since has been refused by the
    policy, caught by the function's own `except`, logged as an ordinary save
    failure and returned as `False`.

    Nothing noticed because nothing had ever called this function against a real
    database — the P4 encryption suite seeds `voice_profiles` as a SUPERUSER,
    which bypasses RLS entirely, so the one suite that touches this table most
    could not see the policy at all.
    """
    from voice.biometrics import save_owner_profile

    assert await save_owner_profile([[1.0, 0.0, 0.0]],
                                    consent_reference="policy-v1") is True, (
        "enrolment returned False. It returns False from inside an `except`, so "
        "the reason is in the log rather than the assertion: before P12-04 it "
        "was `new row violates row-level security policy for table "
        "\"voice_profiles\"`."
    )

    row = await admin.fetchrow(
        "SELECT user_id, consent_granted_at, consent_reference, migration_status "
        "  FROM voice_profiles")
    assert row is not None, "save_owner_profile reported success and wrote nothing"
    assert row["user_id"] == owner, (
        "the row carries no owner, so `037`'s policy hides it from the very "
        "person it belongs to — which is how a write path can succeed and a "
        "read path still find nothing"
    )
    assert row["consent_granted_at"] is not None
    assert row["consent_reference"] == "policy-v1"
    assert row["migration_status"] == "ENCRYPTED"


async def test_what_is_enrolled_can_then_be_read_back(pool, admin, owner):
    """Write path and read path must agree about which row they mean.

    Separately correct halves are not enough: `save_owner_profile` selected rows
    by `owner_name` and `_load_owner_profile` selected the newest row overall, so
    even with the binding fixed a renamed owner would enrol into one row and
    verify against another. Both now scope the same way — RLS on `user_id`,
    newest live row — and this is the test that says so.
    """
    from voice import biometrics

    template = [0.6, 0.8, 0.0]
    assert await biometrics.save_owner_profile(
        [template], consent_reference="policy-v1") is True

    biometrics.invalidate_profile_cache()
    loaded = await biometrics._load_owner_profile()

    assert loaded is not None, (
        "the template that was just enrolled did not load. Under FORCE RLS an "
        "unbound read matches nothing and returns None, which this function's "
        "caller reports as 'no owner profile enrolled'."
    )
    assert len(loaded) == len(template)
    assert await biometrics.voice_consent_state() == "granted"


async def test_an_unbound_reader_still_sees_nothing(pool, admin, owner):
    """The negative control — proof the policy is what was doing the hiding.

    Without this, `test_enrolment_writes_a_row_at_all` passing could mean the
    binding works OR that RLS quietly stopped applying. They are the same green.
    """
    from voice.biometrics import save_owner_profile

    assert await save_owner_profile([[1.0, 0.0, 0.0]],
                                    consent_reference="policy-v1") is True

    # The same pool, the same role, the same table — only the context differs.
    async with pool.acquire() as conn:
        unbound = await conn.fetchval("SELECT count(*) FROM voice_profiles")

    assert unbound == 0, (
        f"an unbound connection read {unbound} voiceprint row(s). Either FORCE "
        f"ROW LEVEL SECURITY is no longer on this table or `user_private` has "
        f"been widened — and both make the binding this batch added pointless."
    )
    assert await admin.fetchval("SELECT count(*) FROM voice_profiles") == 1


async def test_enrolment_leaves_the_ownership_contract_satisfied(pool, admin, owner):
    """`037`'s own readiness view must not be knocked over by a new enrolment.

    `voice_migration_status.contract_ready` is false while any row has a NULL
    `user_id` or an unencrypted template, and `scripts/phase4/encrypt_voiceprints.py`
    uses it as a gate before `063` drops the plaintext column. An enrolment path
    that wrote rows without `user_id` would hold that gate shut forever — which
    is the state the repository was in, invisibly, because no enrolment ever
    completed.
    """
    from voice.biometrics import save_owner_profile

    assert await save_owner_profile([[1.0, 0.0, 0.0]],
                                    consent_reference="policy-v1") is True

    status = await admin.fetchrow("SELECT * FROM voice_migration_status")
    assert status["total_rows"] == 1
    assert status["unresolved_owner"] == 0
    assert status["missing_consent"] == 0
    assert status["still_plaintext"] == 0
    assert status["contract_ready"] is True


async def test_the_voiceprint_connection_refuses_in_cloud(pool, monkeypatch):
    """It derives an identity from a deployment-wide string. Cloud must not.

    `is_owner()` fails closed in cloud for exactly this reason and says so at
    length: `ira_admin_username` is one string for the whole deployment, a
    username is per-tenant, and treating the two as the same thing grants one
    tenant's `alice` authority over another's. A connection that binds
    `private_user_id(ira_admin_username)` carries that same defect into RLS,
    where it would be a template that opens for the wrong body.
    """
    import core.editions as editions
    from core.identity.db_context import (MissingTenantContext,
                                          voiceprint_connection)

    monkeypatch.setattr(editions, "current_edition",
                        lambda: editions.EDITION_CLOUD, raising=False)

    with pytest.raises(MissingTenantContext) as caught:
        async with voiceprint_connection("p1204 cloud probe"):
            pass

    assert "cloud" in str(caught.value).lower()
    assert "p1204 cloud probe" in str(caught.value), (
        "the refusal does not name its caller, so an operator has to go and "
        "find which subsystem reached it"
    )


def test_both_enrolment_branches_write_the_owner():
    """A GREP, declared as one, for a branch execution cannot reach yet.

    `save_owner_profile` has two INSERTs: one for the schema before `063` drops
    `voice_profiles.embedding` (NOT NULL, so it must be named) and one for
    after. `exists()` picks between them, and on every database this suite can
    run against the column is still there — so the post-`063` statement is
    unreachable and no test above covers it.

    That asymmetry is not hypothetical: an adversarial plant that removed
    `user_id` from the post-`063` INSERT left all fourteen tests green, and only
    the same plant against the legacy branch turned them red. A branch that
    activates on the day a migration runs, having never executed, is the shape
    this phase keeps finding.

    So this reads the source, and says so rather than pretending to be an
    execution test. It is worth exactly what a grep is worth — it would not
    notice a wrong VALUES order — and it is strictly better than the nothing
    that covers that branch today.
    """
    import inspect
    import re

    from voice import biometrics

    body = inspect.getsource(biometrics.save_owner_profile)
    inserts = re.findall(r"INSERT INTO voice_profiles \(([^)]*)", body)

    assert len(inserts) == 2, (
        f"expected the pre-063 and post-063 INSERTs, found {len(inserts)}. If a "
        f"branch was added or removed, this test needs to know."
    )
    for columns in inserts:
        assert "user_id" in columns, (
            f"an enrolment INSERT does not write user_id: ({columns.strip()}). "
            f"`037`'s WITH CHECK compares it to app.user_id, so this statement "
            f"is refused by the policy — into an `except` that logs and returns "
            f"False."
        )
