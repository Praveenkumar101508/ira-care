"""Phase 4 acceptance — the exit criteria, checked without trusting the batches.

`PHASE_4_EXECUTION_PLAN.md` §1 lists seven exit criteria. Each batch asserted its
own work and each was green, which is exactly the situation in which a phase can
still be unfinished: every batch checked what it built, and nothing checked the
phase.

So this file is deliberately NOT a summary of the batch suites. It re-derives
each criterion from the live schema and the tree, and it is written to fail if
any batch were reverted — that is its only real job, and the one a "✅ all green"
table cannot do.

── The two invariants §8 singles out ───────────────────────────────────────

**A tenant deletion must never cryptographically destroy a person's second
factor.** This is why D-17 could not be decided by inaction and why
`user_mfa_keys` is keyed per (tenant, user) rather than per tenant. The test
below builds a user who belongs to two tenants, deletes one, and requires the
other enrolment to still be openable. A per-tenant key would fail it.

**No key material in PostgreSQL, and none in a log.** Asserted structurally:
no column anywhere holds an unwrapped key, and the wrapped ones are `bytea`
under a named `kek_id`.

── Vacuity ─────────────────────────────────────────────────────────────────

Criterion 1 reads "no `USER_PRIVATE` secret column remains `PLAINTEXT` in a
fresh chain". On a fresh chain both tables are EMPTY, so a row count satisfies
it trivially and proves nothing — the P2-05 lesson, where a count taken through
RLS counted what the role happened to see.

What is asserted instead is that the schema CANNOT hold the bad state: an
`ENCRYPTED` row without a key is refused, and a shredded row that kept its key
is refused. Those hold on an empty table and on a full one.
"""
from __future__ import annotations

import base64 as _b64
import json
import os
import secrets as _stdlib_secrets
import uuid
from pathlib import Path

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

REPO = Path(__file__).resolve().parents[4]
IRA = REPO / "supracloud-jarvis/ira"
DOCS = REPO / "docs/cloud"

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)


@pytest.fixture
async def admin():
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        yield conn
    finally:
        await conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Criterion 1 — no USER_PRIVATE secret can be half-encrypted
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("constraint,table", [
    ("totp_secrets_encrypted_is_complete", "totp_secrets"),
    ("totp_secrets_has_a_secret", "totp_secrets"),
    ("voice_profiles_encrypted_is_complete", "voice_profiles"),
    ("voice_profiles_shredded_holds_no_key", "voice_profiles"),
])
async def test_the_schema_forbids_a_secret_that_cannot_be_opened(
        admin, constraint, table):
    """**Asserted as a constraint, not as a row count.**

    On a fresh chain both tables are empty, so "no row is PLAINTEXT" is true and
    vacuous. These CHECKs hold whether the table has zero rows or a million, and
    they are what makes an unopenable secret unrepresentable rather than merely
    absent today.
    """
    found = await admin.fetchval(
        "SELECT count(*) FROM pg_constraint "
        " WHERE conrelid = $1::regclass AND conname = $2 AND contype = 'c'",
        table, constraint)
    assert found == 1, (
        f"{constraint} is gone from {table}. Without it the table can hold a "
        f"row marked ENCRYPTED with no key — a secret nobody can open and "
        f"nothing reports as lost.")


async def test_a_keyless_encrypted_voiceprint_is_actually_refused(admin):
    """The constraint existing is one claim; it being ENFORCED is another. A
    constraint added `NOT VALID`, or on a column nothing writes, satisfies the
    catalogue query above and stops nothing."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await admin.execute(
            "INSERT INTO voice_profiles (owner_name, embedding, migration_status,"
            " template_ciphertext, encryption_key_version, encrypted_at) "
            "VALUES ($1, 'p', 'ENCRYPTED', '\\xaa'::bytea, 1, NOW())",
            f"acceptance-{uuid.uuid4().hex[:8]}")


# ─────────────────────────────────────────────────────────────────────────────
# Criterion 2 — the plaintext columns go, but only at the contract stage
# ─────────────────────────────────────────────────────────────────────────────


def test_the_contract_stage_is_held_outside_the_chain():
    """`contract/063` drops the plaintext columns, and it must NOT be a chain
    migration. The workers need those columns to read from; a chain that
    dropped them would destroy the plaintext before anything had encrypted it.

    This is the ordering the whole encryption phase depends on, so it is
    asserted rather than left to the directory looking right.
    """
    contract = REPO / "supracloud-jarvis/postgres/contract/063_contract_stage.sql"
    assert contract.exists(), "the contract stage is missing"
    chain = list((REPO / "supracloud-jarvis/postgres").glob("063_*.sql"))
    assert not chain, (
        f"the contract stage has entered the migration chain as {chain}. It "
        f"drops the plaintext columns the encryption workers read from.")


async def test_the_plaintext_columns_are_still_present_before_the_contract_stage(
        admin):
    """The other half. A test that only checked the contract stage exists would
    pass on a database where somebody had already dropped the columns by hand,
    which is the state in which the workers silently have nothing to migrate."""
    for table, column in (("totp_secrets", "secret"),
                          ("voice_profiles", "embedding")):
        present = await admin.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            " WHERE table_name = $1 AND column_name = $2", table, column)
        assert present == 1, (
            f"{table}.{column} is gone from a database that has only applied "
            f"the chain. Either the contract stage ran here, or a migration "
            f"took it — and the encryption workers now have no source.")


# ─────────────────────────────────────────────────────────────────────────────
# Criterion 3 — production refuses the development key provider
# ─────────────────────────────────────────────────────────────────────────────


def test_production_refuses_to_start_on_the_development_key_provider():
    """ADR-0012. The abstraction is not the control — the refusal is."""
    from core.crypto import key_provider

    assert hasattr(key_provider, "is_development_provider")
    assert key_provider.is_development_provider(key_provider.ENVIRONMENT), (
        "the environment provider is no longer classed as a development one, "
        "so production would accept env-supplied key material")


def test_the_environment_validator_still_checks_the_provider():
    """P4-04 added this as a numbered rule in `validate_for_environment`. A rule
    that stops being called is a rule that stops applying, and nothing else in
    the system would notice."""
    from core.runtime import environment

    source = Path(environment.__file__).read_text(encoding="utf-8")
    assert "key_provider" in source, (
        "core/runtime/environment.py no longer references the key provider — "
        "production can start on the development one again")


# ─────────────────────────────────────────────────────────────────────────────
# Criterion 4 — rotation reaches EVERY purpose, not only connectors
# ─────────────────────────────────────────────────────────────────────────────


async def test_every_table_holding_a_wrapped_key_is_named_by_rotation(admin):
    """**The guard that is about the next table, not this one.**

    P4-05 found that a KEK rotation would have left every voiceprint wrapped
    under a KEK that no longer matched — unrecoverable once the contract stage
    drops the plaintext. The defect was not that voice was forgotten; it was
    that nothing connected "this table holds KEK-wrapped material" to "rotation
    must reach it".

    So the question is put to the live schema.
    """
    from tests.tenancy.test_p4_rotation_reaches_everything import ROTATED

    tables = {r["table_name"] for r in await admin.fetch(
        "SELECT table_name FROM information_schema.columns "
        " WHERE column_name = 'kek_id' AND table_schema = 'public'")}
    unreached = tables - set(ROTATED)
    assert not unreached, (
        f"{sorted(unreached)} hold KEK-wrapped material and are not named in "
        f"ROTATED. A key rotation will leave them wrapped under a KEK that no "
        f"longer matches, and report success.")


# ─────────────────────────────────────────────────────────────────────────────
# Criterion 5 — retention is enforced by something that RUNS
# ─────────────────────────────────────────────────────────────────────────────


def test_retention_enforcement_is_reachable_from_outside_its_own_tests():
    """The criterion says "enforced by something that runs, not only recorded as
    a setting". A module with no caller satisfies neither half — and that is
    precisely what `store.expire_due_memories` was for three phases."""
    from api.routes import privacy as privacy_routes

    assert (IRA / "core/privacy/retention.py").exists()
    routes = {(r.path, m) for r in privacy_routes.router.routes
              for m in getattr(r, "methods", ())}
    assert ("/privacy/settings/enforce", "POST") in routes, (
        "nothing outside the tests can cause a retention pass")


def test_the_expiry_that_had_no_callers_now_has_one():
    """Naming the specific regression. `expire_due_memories` was correct,
    documented, and dead; P4-06 gave it a caller, and a later refactor that
    quietly dropped the call would restore the original defect exactly."""
    source = (IRA / "core/privacy/retention.py").read_text(encoding="utf-8")
    assert "expire_due_memories" in source


# ─────────────────────────────────────────────────────────────────────────────
# Criterion 6 and 7 — the documents
# ─────────────────────────────────────────────────────────────────────────────


def test_no_control_document_still_says_the_secrets_are_plaintext():
    """Criterion 6. The phase encrypted both populations; a document still
    saying otherwise is a claim the code contradicts, and it is the version a
    reader quotes."""
    offenders = []
    for name in ("IMPLEMENTED_VS_PLANNED.md", "SECURITY_CONTROL_MATRIX.md"):
        flat = " ".join((DOCS / name).read_text(encoding="utf-8").split())
        if "Not encrypted. Not one row." in flat:
            offenders.append(name)
    assert not offenders, (
        f"{offenders} still claim the secrets are unencrypted. P2-05 and P4-02 "
        f"encrypted them, and their workers and tests are in the tree.")


def test_the_closure_report_exists():
    """Criterion 7. A phase that cannot say what it did and did not do has not
    closed; it has stopped."""
    report = DOCS / "evidence/PHASE_4_CLOSURE_REPORT.md"
    assert report.exists(), f"{report} is missing"
    text = report.read_text(encoding="utf-8")
    assert "Not claimed" in text or "not claimed" in text, (
        "the closure report has no non-claims section. Every closure report in "
        "this repository carries one, because the boundary of what was proved "
        "is the part a reader cannot reconstruct.")


# ─────────────────────────────────────────────────────────────────────────────
# §8 — the two invariants this phase must not weaken
# ─────────────────────────────────────────────────────────────────────────────


async def test_deleting_one_tenant_does_not_destroy_a_users_second_factor(admin):
    """**The reason D-17 could not be decided by inaction.**

    MFA keys are per (tenant, user). A user who belongs to two tenants holds two
    keys, and deleting one tenant takes one key and one enrolment. A key scoped
    to the TENANT — the shape this design rejected — would mean an unrelated
    tenant's deletion cryptographically destroying somebody's second factor and
    locking them out of an account that had nothing to do with it.

    Built rather than asserted from the FK, because the FK is only half the
    story: the other half is that the enrolment in the surviving tenant is still
    complete afterwards.
    """
    keep, drop = uuid.uuid4(), uuid.uuid4()
    user = uuid.uuid4()
    tag = uuid.uuid4().hex[:8]
    try:
        for tenant, slug in ((keep, f"acc-keep-{tag}"), (drop, f"acc-drop-{tag}")):
            await admin.execute(
                "INSERT INTO tenants (id,name,slug,edition) "
                "VALUES ($1,$2,$3,'cloud')", tenant, slug, slug)
        await admin.execute(
            "INSERT INTO users (id,display_name,auth_provider,status) "
            "VALUES ($1,'Acc','password','active')", user)
        for tenant in (keep, drop):
            await admin.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
                "VALUES ($1,$2,'member','active')", tenant, user)
            await admin.execute(
                "INSERT INTO user_mfa_keys (tenant_id,user_id,key_version,"
                " wrapped_dek,wrap_nonce,kek_id) "
                "VALUES ($1,$2,1,'\\xab'::bytea,'\\xcd'::bytea,'kek-acc')",
                tenant, user)
            await admin.execute(
                "INSERT INTO totp_secrets (tenant_id,user_id,username,"
                " secret_ciphertext,secret_nonce,encryption_key_version,"
                " encrypted_at,migration_status,enabled) "
                "VALUES ($1,$2,$3,'\\xcd'::bytea,'\\xef'::bytea,1,NOW(),"
                "        'ENCRYPTED',TRUE)",
                tenant, user, f"{tag}-{tenant}")

        await admin.execute("DELETE FROM tenants WHERE id = $1", drop)

        surviving_key = await admin.fetchval(
            "SELECT count(*) FROM user_mfa_keys "
            " WHERE tenant_id = $1 AND user_id = $2 AND destroyed_at IS NULL",
            keep, user)
        assert surviving_key == 1, (
            "deleting one tenant destroyed this user's MFA key in ANOTHER "
            "tenant. The key is no longer per-user — a tenant deletion now "
            "locks people out of unrelated accounts.")

        surviving_enrolment = await admin.fetchval(
            "SELECT count(*) FROM totp_secrets "
            " WHERE tenant_id = $1 AND user_id = $2 "
            "   AND secret_ciphertext IS NOT NULL AND migration_status='ENCRYPTED'",
            keep, user)
        assert surviving_enrolment == 1, (
            "the surviving tenant's TOTP enrolment lost its ciphertext")

        assert await admin.fetchval(
            "SELECT count(*) FROM user_mfa_keys WHERE tenant_id = $1",
            drop) == 0, "the deleted tenant's key outlived the tenant"
    finally:
        for tenant in (keep, drop):
            await admin.execute("DELETE FROM tenants WHERE id = $1", tenant)
        await admin.execute("DELETE FROM users WHERE id = $1", user)


async def test_no_column_holds_an_unwrapped_key(admin):
    """§8's second invariant, structurally. Every key at rest is wrapped under a
    named `kek_id`; a column called `key`, `dek` or `secret_key` in the clear
    would be the thing envelope encryption exists to avoid."""
    suspects = await admin.fetch(
        "SELECT table_name, column_name, data_type "
        " FROM information_schema.columns WHERE table_schema = 'public' "
        "   AND (column_name IN ('dek','data_key','plaintext_key','kek') "
        "        OR column_name LIKE '%unwrapped%')")
    assert suspects == [], (
        f"columns that look like unwrapped key material: "
        f"{[(r['table_name'], r['column_name']) for r in suspects]}")

    for table in ("user_mfa_keys", "voice_profiles", "tenant_data_keys"):
        kek = await admin.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            " WHERE table_name = $1 AND column_name = 'kek_id'", table)
        assert kek == 1, (
            f"{table} no longer records which KEK wrapped its keys. Without it "
            f"a rotation cannot tell a stale wrapping from a current one.")


async def test_a_user_can_hold_a_second_factor_in_each_of_their_tenants(admin):
    """**The defect P4-08 found, and the reason it mattered.**

    `036` closed R-26 — a GLOBAL unique on `totp_secrets.username` — by moving
    the key to a surrogate and adding `totp_secrets_user_key ON (user_id)`. Its
    comment says why: "one enrolment per user". That was the model then.

    `035` gave a user many memberships, `055` made invitations the way that
    happens, and `cloud_login.py` raises `TenantSelectionRequired` rather than
    refusing, precisely so a member of several organisations can sign in.
    Nothing revisited the index. So R-26 was closed by replacing a global unique
    with a narrower global unique that outlived its assumption, and the measured
    consequence was: a user with two memberships could enrol a second factor in
    one tenant, was NOT asked for one in the other, and could not turn it on
    there.

    `065` rescopes the index. This asserts the enrolment is possible; the login
    behaviour that follows from it is asserted below.
    """
    a, b, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tag = uuid.uuid4().hex[:8]
    try:
        for tenant, slug in ((a, f"mfa-a-{tag}"), (b, f"mfa-b-{tag}")):
            await admin.execute(
                "INSERT INTO tenants (id,name,slug,edition) "
                "VALUES ($1,$2,$3,'cloud')", tenant, slug, slug)
        await admin.execute(
            "INSERT INTO users (id,display_name,auth_provider,status) "
            "VALUES ($1,'M','password','active')", user)
        for tenant in (a, b):
            await admin.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
                "VALUES ($1,$2,'member','active')", tenant, user)
            await admin.execute(
                "INSERT INTO totp_secrets (tenant_id,user_id,username,secret,"
                " migration_status,enabled) "
                "VALUES ($1,$2,$3,'JBSWY3DPEHPK3PXP','PLAINTEXT',TRUE)",
                tenant, user, f"mfa-{tag}")

        held = await admin.fetchval(
            "SELECT count(*) FROM totp_secrets "
            " WHERE user_id = $1 AND revoked_at IS NULL", user)
        assert held == 2, (
            "this user cannot hold a second factor in both their tenants. In "
            "the tenant without one they are not challenged and cannot enrol.")
    finally:
        for tenant in (a, b):
            await admin.execute("DELETE FROM tenants WHERE id = $1", tenant)
        await admin.execute("DELETE FROM users WHERE id = $1", user)


async def test_one_enrolment_per_user_per_tenant_is_still_enforced(admin):
    """Rescoping a unique is one edit away from removing it. The constraint got
    WIDER, not absent: a second live enrolment in the SAME tenant is still
    refused, because two active seeds for one account in one tenant is an
    ambiguity the login path has no way to resolve."""
    tenant, user = uuid.uuid4(), uuid.uuid4()
    tag = uuid.uuid4().hex[:8]
    try:
        await admin.execute(
            "INSERT INTO tenants (id,name,slug,edition) "
            "VALUES ($1,$2,$3,'cloud')", tenant, f"dup-{tag}", f"dup-{tag}")
        await admin.execute(
            "INSERT INTO users (id,display_name,auth_provider,status) "
            "VALUES ($1,'D','password','active')", user)
        await admin.execute(
            "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
            "VALUES ($1,$2,'member','active')", tenant, user)
        await admin.execute(
            "INSERT INTO totp_secrets (tenant_id,user_id,username,secret,"
            " migration_status,enabled) VALUES ($1,$2,$3,'JBSWY3DPEHPK3PXP',"
            "'PLAINTEXT',TRUE)", tenant, user, f"dup-{tag}")
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await admin.execute(
                "INSERT INTO totp_secrets (tenant_id,user_id,username,secret,"
                " migration_status,enabled) VALUES ($1,$2,$3,'JBSWY3DPEHPK3PXP',"
                "'PLAINTEXT',TRUE)", tenant, user, f"dup2-{tag}")
    finally:
        await admin.execute("DELETE FROM tenants WHERE id = $1", tenant)
        await admin.execute("DELETE FROM users WHERE id = $1", user)


async def test_the_global_enrolment_index_is_gone(admin):
    """Named directly. `065` drops `totp_secrets_user_key`, and a database that
    still has it has not had the fix however new its other objects look."""
    stale = await admin.fetchval(
        "SELECT count(*) FROM pg_indexes "
        " WHERE tablename = 'totp_secrets' AND indexname = 'totp_secrets_user_key'")
    assert stale == 0, (
        "totp_secrets_user_key is back. It permits one second factor per user "
        "ACROSS ALL TENANTS, so a user with two memberships is unprotected in "
        "one of them and cannot enrol there.")


# ─────────────────────────────────────────────────────────────────────────────
# Criterion 4, second half — crypto-shredding is PERMANENT
# ─────────────────────────────────────────────────────────────────────────────
#
# The rotation half is asserted above. The shredding half was asserted only as a
# constraint ("a shredded row cannot keep its key") and as isolation ("shredding
# one leaves another openable"). Neither of those tries to OPEN the shredded row,
# and "the ciphertext is permanently unopenable" is the property crypto-shredding
# actually claims.
#
# The MFA population already has it — `test_a_destroyed_key_makes_the_seed_
# permanently_unreadable` opens after the shred and requires a refusal. The voice
# population did not, and it is the population where it matters more: the
# plaintext column still EXISTS until the contract stage runs, so a shredded row
# sits beside a readable copy of the very thing that was supposed to be
# destroyed.


VOICE_TEMPLATE = "[0.11, 0.22, 0.33, 0.44]"

#: The KEK these tests seal under. Generated, never written down — a literal
#: 32-byte key in a test file is what Gitleaks blocks and what P4-04 had to fix
#: once already. Passed explicitly rather than via `setdefault`, so another
#: module's key cannot win and make a failure here look like a crypto bug.
_ACCEPTANCE_KEK = _b64.b64encode(_stdlib_secrets.token_bytes(32)).decode()
VOICE_ENV = {"IRA_CONNECTOR_ENCRYPTION_KEY": _ACCEPTANCE_KEK,
             "IRA_CONNECTOR_ENCRYPTION_KEY_ID": "kek-acceptance"}
os.environ.setdefault("IRA_CONNECTOR_ENCRYPTION_KEY", _ACCEPTANCE_KEK)
os.environ.setdefault("IRA_CONNECTOR_ENCRYPTION_KEY_ID", "kek-acceptance")


async def _shredded_voice_row(admin, suffix: str = ""):
    """A sealed enrolment, then crypto-shredded. Returns its id."""
    from security.voice_crypto import seal_template

    owner = f"acceptance-shred{suffix}-{uuid.uuid4().hex[:8]}"
    row_id = await admin.fetchval(
        "INSERT INTO voice_profiles (owner_name, embedding, enrollment_version) "
        "VALUES ($1, $2, 1) RETURNING id", owner, VOICE_TEMPLATE)
    sealed = seal_template(row_id, 1, VOICE_TEMPLATE, env=VOICE_ENV)
    await admin.execute(
        "UPDATE voice_profiles SET template_ciphertext=$2, template_nonce=$3,"
        " wrapped_dek=$4, wrap_nonce=$5, kek_id=$6, encryption_key_version=1,"
        " encrypted_at=NOW(), migration_status='ENCRYPTED' WHERE id=$1",
        row_id, sealed["template_ciphertext"], sealed["template_nonce"],
        sealed["wrapped_dek"], sealed["wrap_nonce"], sealed["kek_id"])
    # The shred itself: the key goes, the ciphertext stays. That is the whole
    # mechanism — the data is destroyed by destroying the only key to it.
    await admin.execute(
        "UPDATE voice_profiles SET wrapped_dek=NULL, wrap_nonce=NULL,"
        " deletion_status='crypto_shredded' WHERE id=$1", row_id)
    return row_id


async def test_a_crypto_shredded_voiceprint_cannot_be_opened(admin):
    """**The property crypto-shredding claims, asserted as an outcome.**

    The existing tests check that the schema refuses a shredded row which kept
    its key, and that shredding one enrolment leaves another readable. Neither
    tries to open the shredded row, so neither would notice a `use_template`
    that had grown a path returning the template anyway.
    """
    from security.voice_crypto import VoiceKeyUnavailable, use_template

    row_id = await _shredded_voice_row(admin)
    full = dict(await admin.fetchrow(
        "SELECT * FROM voice_profiles WHERE id=$1", row_id))
    try:
        with pytest.raises(VoiceKeyUnavailable):
            await use_template(full, json.loads, env=VOICE_ENV)
    finally:
        await admin.execute("DELETE FROM voice_profiles WHERE id=$1", row_id)


async def test_a_shredded_voiceprint_does_not_fall_back_to_the_plaintext(
        admin, monkeypatch):
    """**The one that matters before the contract stage runs.**

    `voice_profiles.embedding` still exists — `contract/063` drops it, and no
    deployment has run that. So a crypto-shredded row sits next to a plaintext
    copy of the template it was supposed to destroy, and a read path that fell
    back would authenticate against it. The shred would then be a column update
    and nothing else.

    The existing fallback test corrupts the wrapped key, which fails inside the
    DECRYPT. A shred sets it to NULL, which fails at the completeness check —
    a different branch, and the one a `deletion_status` shortcut would land in.
    """
    import utils.db as db_module
    import voice.biometrics as bio

    row_id = await _shredded_voice_row(admin)
    plaintext_present = await admin.fetchval(
        "SELECT embedding IS NOT NULL FROM voice_profiles WHERE id=$1", row_id)
    assert plaintext_present, (
        "this test is vacuous without a plaintext template beside the shredded "
        "ciphertext — that is the situation it exists to check")

    pool = await asyncpg.create_pool(dsn=ADMIN_DSN, min_size=1, max_size=2)
    previous = db_module._pool
    db_module._pool = pool
    monkeypatch.setattr(bio, "_owner_embedding_cache", None, raising=False)
    try:
        loaded = await bio._load_owner_profile()
    finally:
        db_module._pool = previous
        await pool.close()
        await admin.execute("DELETE FROM voice_profiles WHERE id=$1", row_id)

    assert loaded is None, (
        "a crypto-shredded enrolment was loaded from its plaintext column. The "
        "shred destroyed the key and nothing else: verification still succeeds "
        "against the template the user asked to have destroyed.")


async def test_a_rotation_does_not_resurrect_a_shredded_enrolment(admin):
    """P4-05 asserts a shredded row is "skipped rather than counted as a
    failure" — a statement about the COUNT. This asserts the consequence: after
    a rotation has actually run, the row is still unopenable.

    **The rotation must genuinely run, and that is asserted separately.** The
    first version of this test wrapped the rewrap in `except Exception: pass`,
    reasoning that a rotation which could not run did not weaken the conclusion.
    It did: a planted defect that made the rewrap include shredded rows walked
    straight past, because the rewrap never executed and the row was untouched
    for the wrong reason. A live enrolment alongside is the witness that work
    happened.
    """
    from security.voice_crypto import (
        VoiceKeyUnavailable, rewrap_tenant_voice_keys, seal_template,
        use_template,
    )
    from tests.tenancy.test_p4_rotation_reaches_everything import _principal

    new_env = {"IRA_CONNECTOR_ENCRYPTION_KEY":
                   _b64.b64encode(_stdlib_secrets.token_bytes(32)).decode(),
               "IRA_CONNECTOR_ENCRYPTION_KEY_ID": "kek-acceptance-2"}

    shredded = await _shredded_voice_row(admin, "-dead")
    live_owner = f"acceptance-live-{uuid.uuid4().hex[:8]}"
    live = await admin.fetchval(
        "INSERT INTO voice_profiles (owner_name, embedding, enrollment_version) "
        "VALUES ($1, '', 1) RETURNING id", live_owner)
    sealed = seal_template(live, 1, VOICE_TEMPLATE, env=VOICE_ENV)
    await admin.execute(
        "UPDATE voice_profiles SET template_ciphertext=$2, template_nonce=$3,"
        " wrapped_dek=$4, wrap_nonce=$5, kek_id=$6, encryption_key_version=1,"
        " encrypted_at=NOW(), migration_status='ENCRYPTED' WHERE id=$1",
        live, sealed["template_ciphertext"], sealed["template_nonce"],
        sealed["wrapped_dek"], sealed["wrap_nonce"], sealed["kek_id"])

    import utils.db as db_module

    pool = await asyncpg.create_pool(dsn=ADMIN_DSN, min_size=1, max_size=3)
    previous = db_module._pool
    db_module._pool = pool
    try:
        principal = await _principal(admin)
        done, failures = await rewrap_tenant_voice_keys(
            principal, old_env=VOICE_ENV, new_env=new_env)

        # The witness. Without it the assertions below hold trivially on a
        # rotation that never touched anything.
        assert done >= 1, (
            f"the rotation re-wrapped nothing ({done} done, {failures} failed), "
            f"so this test proves nothing about what it does to a shredded row")
        opened = await use_template(
            dict(await admin.fetchrow(
                "SELECT * FROM voice_profiles WHERE id=$1", live)),
            json.loads, env=new_env)
        assert opened == json.loads(VOICE_TEMPLATE), (
            "the live enrolment did not survive the rotation")

        still = dict(await admin.fetchrow(
            "SELECT * FROM voice_profiles WHERE id=$1", shredded))
        assert still["wrapped_dek"] is None, (
            "a rotation gave a crypto-shredded enrolment a wrapped key back — "
            "it has undone a destruction the user asked for")
        assert str(shredded) not in failures, (
            "a shredded row was reported as a rotation FAILURE. It has no key "
            "by design, and flagging it makes every rotation after a shred look "
            "broken")
        # NOTE: the line above cannot fail while `rewrap_voice_key` keeps its
        # `if row["wrapped_dek"] is None: return False` guard — a shredded row
        # returns early and is never appended to `failures`. It is kept as a
        # regression check on the CONTRACT, and the guard itself is tested
        # directly below, because an assertion that cannot fire is not evidence
        # of the thing it names.
        with pytest.raises(VoiceKeyUnavailable):
            await use_template(still, json.loads, env=new_env)
    finally:
        db_module._pool = previous
        await pool.close()
        await admin.execute(
            "DELETE FROM voice_profiles WHERE id = ANY($1::uuid[])",
            [shredded, live])


async def test_the_rewrap_declines_a_shredded_row_rather_than_erroring(admin):
    """**The guard that makes the assertion above unfalsifiable, tested directly.**

    `rewrap_voice_key` returns `False` for a row with no wrapped key rather than
    raising, so a shredded enrolment never reaches the failure list. That is
    correct — a row destroyed on purpose is not a rotation failure, and flagging
    it would make every rotation after a crypto-shred look broken.

    It also means the `not in failures` assertion above can never fire. Two
    planted defects proved that: routing shredded rows INTO the rewrap changed
    nothing, because this early return catches them. So the mechanism is asserted
    here, on the function, where a defect in it can actually be seen.
    """
    from security.voice_crypto import rewrap_voice_key

    row_id = await _shredded_voice_row(admin, "-decline")
    try:
        row = dict(await admin.fetchrow(
            "SELECT id, enrollment_version, wrapped_dek, wrap_nonce, kek_id "
            "  FROM voice_profiles WHERE id=$1", row_id))
        assert row["wrapped_dek"] is None

        result = await rewrap_voice_key(
            None, row, old_env=VOICE_ENV, new_env=VOICE_ENV)
        assert result is False, (
            "rewrap_voice_key did not decline a shredded row. Either it raised "
            "— making every rotation after a crypto-shred report a failure — or "
            "it re-wrapped something, which for a row with no key means it "
            "minted one.")

        assert await admin.fetchval(
            "SELECT wrapped_dek IS NULL FROM voice_profiles WHERE id=$1",
            row_id) is True, "declining still wrote a key"
    finally:
        await admin.execute("DELETE FROM voice_profiles WHERE id=$1", row_id)
