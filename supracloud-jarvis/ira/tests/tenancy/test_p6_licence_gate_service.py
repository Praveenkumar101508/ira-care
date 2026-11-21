"""Phase 6 batch 03 — the licence gate on the READ path.

`068` refuses the write that would put an unlicensed model into service. This is
the other half: the request path refuses to run one, and readiness refuses to
come up if the database is already in that state.

── Why three mechanisms and not one ───────────────────────────────────────

They fail differently, and only the first is visible in a schema:

    068's trigger              refuses the WRITE
    require_approved_licence   refuses the REQUEST
    check_model_licences       refuses to COME UP

A trigger speaks for the moment of writing. It cannot speak for a row written
before it existed, for a row written while it was disabled — `ALTER TABLE …
DISABLE TRIGGER` changes one character in `pg_trigger` and nothing a schema diff
shows — or for a database somebody edited by hand. And an approval is a legal
position, so it can be *withdrawn*: the state a model is allowed to be in is not
fixed at the moment it was registered.

Every test below therefore does its damage with the trigger disabled, which is
the only way to reach the state the read path exists to catch. That is done
inside a transaction that is always rolled back, and the trigger is a schema
object rather than data — so `test_the_trigger_is_enabled_again_afterwards`
exists as the belt-and-braces check that no test leaked a disabled guard into
the shared development database.
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

from tests.tenancy._licence_fixture import (  # noqa: E402
    FIXTURE_NOTE, FIXTURE_VERIFIER, approved_licence, drop_licence)

HASH = "sha256:" + "a" * 64
DIGEST = "sha256:" + "d" * 64


@pytest.fixture
async def pool():
    """The application pool — the connection `registry` actually uses.

    Set on `utils.db` directly rather than through `init_pool`, because the
    module reads a global and a test that patched a wrapper would be testing the
    wrapper. Restored afterwards so a later suite gets whatever it expected.
    """
    import utils.db as db_module

    previous = getattr(db_module, "_pool", None)
    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


class _Operator:
    """Commits, and cleans up exactly what it made.

    Commits because the registry is read through the APPLICATION pool on a
    different connection, which cannot see an uncommitted row — the same
    finding P5-02 recorded. Cleanup is by key rather than by truncation, for
    the same reason it was there: a blanket delete removes rows another test or
    another developer's database legitimately holds.
    """

    def __init__(self, conn):
        self._conn = conn
        self._models: list = []
        self._licences: list = []

    async def execute(self, *a, **kw):
        return await self._conn.execute(*a, **kw)

    async def fetchval(self, *a, **kw):
        return await self._conn.fetchval(*a, **kw)

    async def cleanup(self):
        for key in self._models:
            await self._conn.execute(
                "DELETE FROM model_registry WHERE model_key = $1", key)
        for licence_id in self._licences:
            await drop_licence(self._conn, licence_id)


@pytest.fixture
async def admin():
    conn = await asyncpg.connect(ADMIN_DSN)
    operator = _Operator(conn)
    try:
        yield operator
    finally:
        await operator.cleanup()
        await conn.close()


def _key() -> str:
    return f"p6svc{uuid.uuid4().hex[:10]}"


async def _allowlist(admin, identifier, revision):
    await admin.execute(
        "INSERT INTO model_pull_allowlist (source, identifier, revision) "
        "VALUES ('huggingface', $1, $2) ON CONFLICT DO NOTHING",
        identifier, revision)


async def _evidence_row(admin, classification="review_required",
                        licence_identifier="Apache License"):
    """A licence row in a state ENGINEERING may write."""
    licence_id = await admin.fetchval(
        "INSERT INTO model_licence_register "
        "  (source, identifier, reference, licence_identifier, artifact_digest, "
        "   evidence_url, evidence_method, evidence_retrieved_at, classification) "
        "VALUES ('ollama', $1, 'ref', $2, $3, 'https://example.invalid/x', "
        "        'owner-supplied', NOW(), $4) RETURNING id",
        f"p6svc/{uuid.uuid4().hex[:12]}", licence_identifier, DIGEST,
        classification)
    admin._licences.append(licence_id)
    return licence_id


async def _serve(admin, key, licence_id, *, revision="9f8e7d6c",
                 base="org/p6model", disable_trigger=False):
    """Register a version as SERVING against `licence_id`.

    `disable_trigger` reaches the state the read path exists to catch, and there
    is no other way to reach it: `068` refuses the write. Re-enabled in the same
    call rather than in a fixture teardown, because a teardown that did not run
    would leave the shared development database with its licence gate off.
    """
    await _allowlist(admin, base, revision)
    if disable_trigger:
        await admin.execute(
            "ALTER TABLE model_registry DISABLE TRIGGER model_registry_licence_gate")
    try:
        await admin.execute(
            "INSERT INTO model_registry (model_key, version, base_model, "
            " revision, artifact_hash, environment, commercial_licence, "
            " licence_recorded_at, status, rollout_percentage, licence_id) "
            "VALUES ($1,1,$2,$3,$4,'development','fixture',$5,'serving',100,$6)",
            key, base, revision, HASH, dt.datetime.now(dt.timezone.utc),
            licence_id)
    finally:
        if disable_trigger:
            await admin.execute(
                "ALTER TABLE model_registry ENABLE TRIGGER "
                "model_registry_licence_gate")
    admin._models.append(key)


# ─────────────────────────────────────────────────────────────────────────────
# The read path refuses
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["unknown", "review_required",
                                   "known_restricted"])
async def test_a_request_is_refused_when_the_licence_is_not_approved(
        pool, admin, state):
    """**MOD-01 at the point of use.**

    All three non-approved states refuse, and they refuse for visibly different
    reasons — `unknown` means nobody can read the licence because none exists,
    `review_required` means nobody has, and `known_restricted` means somebody
    did and the answer was no. A gate that collapsed them into "not allowed"
    would send an operator looking in the wrong place.
    """
    from core.models import registry

    key = _key()
    if state == "known_restricted":
        licence_id = await _evidence_row(admin)
        await admin.execute(
            "UPDATE model_licence_register SET classification = 'known_restricted', "
            "  classified_by = $2, classified_at = NOW(), determination_note = $3 "
            "WHERE id = $1", licence_id, FIXTURE_VERIFIER, FIXTURE_NOTE)
    else:
        licence_id = await _evidence_row(
            admin, classification=state,
            licence_identifier=None if state == "unknown" else "Apache License")

    await _serve(admin, key, licence_id, disable_trigger=True)

    with pytest.raises(registry.ModelLicenceNotApproved) as exc:
        await registry.resolve_for_request(key, "tenant-1")
    assert state in str(exc.value), (
        f"the refusal does not say which state it found: {exc.value}")


async def test_a_request_is_refused_when_there_is_no_licence_row_at_all(
        pool, admin):
    """Every `model_registry` row written before `068` is in this state, and
    `067`'s `serving_is_licensed` was satisfied by any string — so `NULL`
    licence_id with a plausible `commercial_licence` is exactly what a database
    migrated from Phase 5 looks like."""
    from core.models import registry

    key = _key()
    await _serve(admin, key, None, disable_trigger=True)
    with pytest.raises(registry.ModelLicenceNotApproved,
                       match="no licence register row"):
        await registry.resolve_for_request(key, "tenant-1")


async def test_an_approved_licence_resolves_and_carries_its_identifier(
        pool, admin):
    """The positive case, without which every assertion above would pass
    against a gate that refused everything.

    It also pins that `ApprovedModel` carries the PUBLISHED identifier — the
    evidence — so the object that reaches a provider records what it ran under.
    """
    from core.models import registry

    key = _key()
    licence_id = await approved_licence(
        admin._conn, reference=f"{key}:1", digest=DIGEST)
    admin._licences.append(licence_id)
    await admin.execute(
        "UPDATE model_licence_register SET licence_identifier = 'Apache License' "
        "WHERE id = $1 AND classification <> 'known_approved'", licence_id)

    await _serve(admin, key, licence_id)
    approved = await registry.resolve_for_request(key, "tenant-1")
    assert approved.model_key == key
    assert approved.revision == "9f8e7d6c"
    assert approved.licence_identifier, (
        "ApprovedModel carries no licence identifier, so nothing downstream can "
        "record what licence the request ran under")


async def test_an_unreadable_licence_register_is_a_refusal(pool, admin,
                                                           monkeypatch):
    """**"I could not check" is not "nothing to worry about".**

    P5-02 made the same call for the registry itself. A control that treats an
    error as a pass switches itself off under exactly the conditions it exists
    for — a database under load, a permission change, a schema drift.
    """
    from core.models import registry

    key = _key()
    licence_id = await approved_licence(
        admin._conn, reference=f"{key}:1", digest=DIGEST)
    admin._licences.append(licence_id)
    await _serve(admin, key, licence_id)

    async def boom(_licence_id):
        raise RuntimeError("relation \"model_licence_register\" does not exist")

    monkeypatch.setattr(registry, "licence_for", boom)
    with pytest.raises(registry.ModelLicenceNotApproved, match="could not be read"):
        await registry.resolve_for_request(key, "tenant-1")


async def test_the_licence_check_happens_before_the_model_is_handed_over(
        pool, admin):
    """**Order, asserted on the source.**

    P5-07's rejection was that the gateway called a provider and then checked.
    The same mistake is available here: resolve the model, build the
    `ApprovedModel`, hand it back, and check the licence afterwards. The outcome
    would look identical in a test that only inspected the exception — and an
    unlicensed model would already have been dispatched.
    """
    import inspect

    from core.models import registry

    source = inspect.getsource(registry.resolve_for_request)
    licence_at = source.index("require_approved_licence(")
    build_at = source.index("return ApprovedModel(")
    assert licence_at < build_at, (
        "resolve_for_request builds the ApprovedModel before it checks the "
        "licence, so an unlicensed model is handed to a caller and refused "
        "afterwards")


async def test_the_trigger_is_enabled_again_afterwards(admin):
    """Belt and braces on this file's own method.

    Every test above disables `model_registry_licence_gate` to create a state
    `068` refuses to create. A trigger is a schema object: if one of those
    `finally` blocks ever stops running, the shared development database is left
    with its licence gate off and every later suite passes while enforcing
    nothing.
    """
    enabled = await admin.fetchval(
        "SELECT tgenabled FROM pg_trigger WHERE tgname = "
        "'model_registry_licence_gate' AND NOT tgisinternal")
    assert enabled in (b"O", "O"), (
        f"model_registry_licence_gate is {enabled!r} rather than enabled — a "
        f"test in this file disabled it and did not put it back")


# ─────────────────────────────────────────────────────────────────────────────
# Readiness
# ─────────────────────────────────────────────────────────────────────────────


async def test_readiness_fails_when_a_model_serves_without_an_approved_licence(
        pool, admin):
    """The mechanism that catches what the other two structurally cannot: a
    database already in the bad state when the process starts."""
    from core.runtime import readiness

    key = _key()
    licence_id = await _evidence_row(admin)
    await _serve(admin, key, licence_id, disable_trigger=True)

    result = await readiness.check_model_licences(required=True)
    assert result.status is readiness.CheckStatus.FAILED, (
        "readiness came up green with a model serving on an unread licence")
    assert result.blocks_readiness


async def test_readiness_passes_in_the_current_state(pool, admin):
    """Nothing is serving, because every licence is `review_required` or
    `unknown` — which is MOD-01 blocking, working. Readiness must not report
    that as a failure: a blocked phase is not a broken instance."""
    from core.runtime import readiness

    result = await readiness.check_model_licences(required=True)
    assert result.status is readiness.CheckStatus.OK, (
        f"readiness reports {result.detail!r} while nothing is serving")


async def test_readiness_passes_without_the_tables(pool, admin, monkeypatch):
    """A developer who has not migrated has no `model_registry`, and nothing can
    be serving without it. `check_model_artefacts` is development-optional for
    this reason; this check is required everywhere instead, and is written so
    the un-migrated case is a genuine pass rather than a suppressed error.

    What it must never do is pass because it could not tell — which is the case
    above it.
    """
    from core.runtime import readiness

    class _Conn:
        async def fetchval(self, *a, **kw):
            return False        # to_regclass(...) IS NOT NULL → false

        async def fetch(self, *a, **kw):  # pragma: no cover
            raise AssertionError(
                "the probe queried model_registry after finding it absent")

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    import utils.db as db_module
    monkeypatch.setattr(db_module, "acquire", lambda *a, **kw: _Acquire())

    result = await readiness.check_model_licences(required=True)
    assert result.status is readiness.CheckStatus.OK


async def test_readiness_gathers_the_licence_check(monkeypatch):
    """A check that exists and is never called is the P4-06 class: a check
    missing from readiness produces no error anywhere — readiness simply stops
    looking at licences.

    **This used to read the source of the `gather` call, and P13-04 broke it**
    by moving the probe list into `_evaluate_uncached` when it added
    single-flight. The property never changed; only the line the assertion was
    pointing at did.

    It is now asserted by RUNNING readiness with every probe replaced, and
    checking that `model_licences` is in the report. That is strictly stronger
    — a check could appear in the source list and still be dropped before the
    report — and it cannot be broken by moving code, which is the failure this
    test just demonstrated.
    """
    from core.runtime import readiness
    from core.runtime.readiness import CheckResult, CheckStatus

    seen: list[str] = []

    for name in ("postgres", "redis", "migrations", "database_role",
                 "key_provider", "model_artefacts", "model_licences",
                 "model_gateway"):
        async def _check(required: bool = True, _name=name):
            seen.append(_name)
            return CheckResult(_name, CheckStatus.OK, 1, required)

        monkeypatch.setattr(readiness, f"check_{name}", _check)

    readiness.reset_readiness_cache()
    try:
        report = await readiness.evaluate_readiness(use_cache=False)
    finally:
        readiness.reset_readiness_cache()

    assert "model_licences" in seen, (
        "check_model_licences is defined and never called, so /readyz never "
        "runs it")
    assert "model_licences" in [c.name for c in report.checks], (
        "the licence check ran but its result never reached the report")
