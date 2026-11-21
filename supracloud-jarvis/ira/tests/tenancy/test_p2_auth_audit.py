"""Phase 2 batch 11 — the authentication audit trail.

Phase 2 built ten batches of authentication and recorded almost none of it.
`access_audit_log` covers authorisation — what a principal was allowed to reach
— and says nothing about how they came to BE that principal. `061` and
`core/identity/auth_audit.py` answer the questions an incident actually asks:
when did this account last sign in, did the second factor ever fail, was a
recovery code spent, was the password reset, did somebody link a provider.

Four properties, and each is the whole point of a different half of the design:

  * the log is APPEND-ONLY, refused three independent ways (no DELETE policy,
    no DELETE grant, a trigger that raises)
  * a person reads their OWN history and nobody else's — not the tenant's
  * NOTHING readable is stored: no credential material, and no address or IP in
    the clear either
  * recording NEVER changes an authentication outcome

The last one is the inversion of the rule the rest of Phase 2 follows, and the
tests below hold it in both directions: a logging failure must not refuse a
login, AND a login must not be silently unlogged.
"""
from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

for _k, _v in {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-test",
    "REDIS_PASSWORD": "ci-test",
    "VLLM_API_KEY": "ci-test",
}.items():
    os.environ.setdefault(_k, _v)

DOMAIN = "p2audit.example"
PASSWORD = "a-perfectly-adequate-passphrase"
TENANT = uuid.uuid4()
ALICE = uuid.uuid4()
BOB = uuid.uuid4()


def email(name: str) -> str:
    return f"{name}@{DOMAIN}"


@pytest.fixture
async def pool():
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=8)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


@pytest.fixture
async def seeded(monkeypatch, pool):
    """One tenant, two members, both able to sign in."""
    from api.middleware.auth import hash_password
    from core.editions import EDITION_CLOUD

    for target in ("core.editions.current_edition",
                   "core.identity.resolver.current_edition"):
        monkeypatch.setattr(target, lambda: EDITION_CLOUD)

    stored = hash_password(PASSWORD)
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await _wipe(admin)
        run = uuid.uuid4().hex[:10]
        await admin.execute(
            "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
            TENANT, f"Audit {run}", f"audit-{run}")
        for user, label in ((ALICE, "alice"), (BOB, "bob")):
            await admin.execute(
                "INSERT INTO users (id,email,display_name,auth_provider,status,"
                " email_verified_at) "
                "VALUES ($1,$2,$3,'password','active',NOW())",
                user, email(label), label)
            await admin.execute(
                "INSERT INTO user_credentials (user_id,password_hash) "
                "VALUES ($1,$2)", user, stored)
            await admin.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
                "VALUES ($1,$2,'member','active')", TENANT, user)
        yield admin
    finally:
        await _wipe(admin)
        await admin.close()


async def _wipe(admin):
    # `auth_events` first and by hand: the FKs are ON DELETE SET NULL rather
    # than CASCADE, so deleting the users and the tenant would leave the rows
    # behind — de-attributed, and therefore invisible to the next run's
    # assertions while still being counted by anything that counts rows.
    # TRUNCATE rather than DELETE, because 061's trigger refuses DELETE and is
    # supposed to: this is the deliberate operator action its HINT describes.
    await admin.execute("TRUNCATE auth_events")
    await admin.execute("DELETE FROM tenants WHERE id = $1", TENANT)
    # Migration 096: a password reset raises the account's token version (R08),
    # and a user with a counter cannot be deleted (ON DELETE RESTRICT).
    await admin.execute("DELETE FROM auth_token_versions WHERE user_id IN "
                        "(SELECT id FROM users WHERE normalised_email LIKE $1)", f"%@{DOMAIN}")
    await admin.execute("DELETE FROM users WHERE normalised_email LIKE $1",
                        f"%@{DOMAIN}")


async def _principal(name: str):
    from core.identity.resolver import resolve_principal

    return await resolve_principal(email(name), edition="cloud")


async def _events(admin, **where) -> list[dict]:
    """Every event, read as the ADMIN so the SELECT policy is not the thing
    under test. The policy gets its own tests below."""
    clauses, args = [], []
    for i, (col, val) in enumerate(where.items(), 1):
        clauses.append(f"{col} = ${i}")
        args.append(val)
    sql = "SELECT * FROM auth_events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return [dict(r) for r in await admin.fetch(sql + " ORDER BY created_at", *args)]


# ─────────────────────────────────────────────────────────────────────────────
# Recording — the events that must exist
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_successful_sign_in_is_recorded(seeded):
    from core.identity.cloud_login import authenticate_cloud_user

    admin = seeded
    await authenticate_cloud_user(email("alice"), PASSWORD, tenant_id=TENANT)

    events = [e["event"] for e in await _events(admin, user_id=ALICE)]
    assert "sign_in_succeeded" in events, (
        f"a successful sign-in left no record; got {events}. 'When did this "
        f"account last sign in' is the first question asked after an incident.")


async def test_a_refused_sign_in_is_recorded_without_naming_the_account(seeded):
    """The event that matters most, and the one with the sharpest constraint.

    A failed-sign-in log has to record that somebody tried, and must not become
    a list of which addresses are registered. So the row carries the HMAC of the
    attempted address and NOT the `user_id` — even here, where the account
    plainly exists.
    """
    from core.identity.cloud_login import CloudLoginRefused, authenticate_cloud_user

    admin = seeded
    with pytest.raises(CloudLoginRefused):
        await authenticate_cloud_user(email("alice"), "not-the-password",
                                      tenant_id=TENANT)

    refusals = await _events(admin, event="sign_in_refused")
    assert refusals, "a refused sign-in left no record at all"
    assert all(r["user_id"] is None for r in refusals), (
        "a refused sign-in named the account it was for. That turns the audit "
        "table into an oracle for which addresses are registered — the exact "
        "thing `authenticate_cloud_user` refuses to tell the caller.")
    assert all(r["subject_hash"] for r in refusals), (
        "the refusal recorded no subject at all, so 'the same mailbox, twice' "
        "is unanswerable and the row cannot support a rate limit or an alert")


async def test_the_attempted_address_is_never_stored_in_the_clear(seeded):
    from core.identity.cloud_login import CloudLoginRefused, authenticate_cloud_user

    admin = seeded
    with pytest.raises(CloudLoginRefused):
        await authenticate_cloud_user(email("nobody"), PASSWORD, tenant_id=TENANT)

    rows = await _events(admin)
    blob = " ".join(
        f"{r['subject_hash'] or ''} {r['ip_hash'] or ''} {r['detail']} "
        f"{r['user_agent']}" for r in rows)
    assert DOMAIN not in blob, (
        "an attempted address reached the audit table in the clear. A "
        "failed-sign-in log full of addresses is two things at once: a "
        "personal-data store nobody remembers to erase, and a list of every "
        "address somebody probed.")
    assert rows and any(r["subject_hash"] for r in rows)


async def test_the_subject_hash_is_stable_and_keyed(seeded):
    """Stable, so 'the same mailbox again' is answerable; keyed, so the hash is
    not the address to anybody holding a list of plausible ones."""
    import hashlib

    from core.identity.cloud_login import CloudLoginRefused, authenticate_cloud_user

    admin = seeded
    for _ in range(2):
        with pytest.raises(CloudLoginRefused):
            await authenticate_cloud_user(email("nobody"), PASSWORD,
                                          tenant_id=TENANT)

    hashes = {r["subject_hash"] for r in await _events(admin,
                                                       event="sign_in_refused")}
    assert len(hashes) == 1, (
        f"two attempts on the same mailbox produced {len(hashes)} distinct "
        f"hashes, so counting attempts per address is impossible")

    unkeyed = hashlib.sha256(email("nobody").encode()).hexdigest()
    assert unkeyed not in hashes, (
        "the subject hash is an UNKEYED digest of the address. The whole "
        "plausible-address space is a table somebody precomputes once, and an "
        "unkeyed hash of an identifier IS the identifier to whoever built it.")


async def test_a_password_reset_records_both_halves(seeded):
    """The request and the completion, and the completion carries the session
    count — because `_end_every_session` is fail-soft by design and the count is
    the only durable evidence that the second half happened."""
    from core.identity.password_reset import (
        complete_password_reset, request_password_reset,
    )

    admin = seeded
    issued = await request_password_reset(email("alice"))
    assert issued.token
    await complete_password_reset(issued.token, "another-adequate-passphrase")

    events = {e["event"]: e for e in await _events(admin, user_id=ALICE)}
    assert "password_reset_requested" in events
    assert "password_reset_completed" in events
    assert "session(s) ended" in events["password_reset_completed"]["detail"], (
        "the completion event does not say how many sessions were ended. "
        "Revoking them is the half of a reset people forget, and it is "
        "fail-soft — so an event that omits the count cannot tell a reset that "
        "signed the attacker out from one that did not.")


async def test_a_reset_for_an_unknown_address_records_nothing(seeded):
    """The asymmetry that keeps the audit table from becoming the enumeration
    oracle `request_password_reset` refuses to be.

    One row per probed address is the same oracle, moved from the response into
    a store the operator reads.
    """
    from core.identity.password_reset import request_password_reset

    admin = seeded
    outcome = await request_password_reset(email("nobody"))
    assert outcome.issued is False

    assert await _events(admin, event="password_reset_requested") == [], (
        "a reset request for an address with no account was recorded. An "
        "attacker who can trigger requests now writes one row per address they "
        "probe, and the audit table answers the question the response refuses "
        "to.")


async def test_revoking_every_session_is_recorded_with_its_count(seeded):
    from core.identity.sessions import record_session, revoke_all_sessions

    admin = seeded
    alice = await _principal("alice")
    await record_session(alice, refresh_jti="jti-audit-1")
    await revoke_all_sessions(alice)

    rows = await _events(admin, event="sessions_revoked_all", user_id=ALICE)
    assert rows, "signing out everywhere left no record"
    assert "1 session(s)" in rows[0]["detail"]


async def test_issuing_recovery_codes_records_the_generation_not_the_codes(seeded):
    from security.mfa_recovery import generate_recovery_codes

    admin = seeded
    alice = await _principal("alice")
    issued = await generate_recovery_codes(alice, count=3)

    rows = await _events(admin, event="recovery_codes_issued", user_id=ALICE)
    assert rows, (
        "issuing a new set of recovery codes left no record. Issuing a set "
        "INVALIDATES the owner's, so an attacker who reached the settings page "
        "hands themselves a fresh way past the second factor and takes the "
        "owner's away.")
    assert f"generation {issued.generation}" in rows[0]["detail"]
    for code in issued.codes:
        assert code not in rows[0]["detail"], (
            "a recovery code reached the audit table. An audit trail carrying "
            "credential material turns a log reader into an authenticator.")


# ─────────────────────────────────────────────────────────────────────────────
# Append-only
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_application_holds_no_delete_privilege(seeded, pool):
    """Three independent refusals, and this one is the OUTERMOST: `ira_app`
    holds no DELETE privilege, so the statement dies before RLS or the trigger
    is consulted.

    ── Why this asserts the SQLSTATE and not merely "it raised" ─────────────

    The first version caught `asyncpg.PostgresError`, and a planted defect
    walked straight past it: granting DELETE and adding a DELETE policy still
    left the trigger raising, so the test went green while two of the three
    refusals were gone. A test that cannot tell the first line of defence from
    the last cannot notice the first one falling.

    `InsufficientPrivilegeError` is the grant refusing. The trigger's refusal is
    a `RaiseError` and is asserted separately below.
    """
    admin = seeded
    await admin.execute(
        "INSERT INTO auth_events (user_id, event) VALUES ($1, 'sign_in_succeeded')",
        ALICE)

    from core.identity.db_context import identity_bootstrap_connection

    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with identity_bootstrap_connection(ALICE) as conn:
            await conn.execute("DELETE FROM auth_events")

    assert await _events(admin, user_id=ALICE), (
        "the event was deleted. The first thing an intruder does is remove the "
        "entry that shows them arriving.")


async def test_no_delete_policy_exists_at_all(seeded):
    """The second refusal, and the only one that is an ABSENCE.

    Under FORCE RLS a table with no DELETE policy denies every delete, whatever
    grants exist. An absence is the hardest thing to notice going missing —
    nothing fails, a statement simply starts succeeding — so it is asserted
    directly rather than inferred from a delete that raised for some reason.
    """
    admin = seeded
    assert await admin.fetchval(
        "SELECT count(*) FROM pg_policy "
        " WHERE polrelid = 'public.auth_events'::regclass AND polcmd = 'd'") == 0, (
        "auth_events has a DELETE policy")
    assert await admin.fetchval(
        "SELECT has_table_privilege('ira_app','auth_events','DELETE')") is False, (
        "ira_app holds DELETE on auth_events")
    assert await admin.fetchval(
        "SELECT has_table_privilege('ira_worker','auth_events','SELECT')") is False, (
        "ira_worker can read auth_events — a job that can read this can "
        "reconstruct who signed in from where")


async def test_the_owner_cannot_delete_an_event_either(seeded):
    """The trigger, proved as the role a migration runs as.

    The grant check above is refused before the trigger is reached, so on its
    own it proves nothing about the trigger — and the grant is the defence a
    provisioning script hands straight back (R-47).
    """
    admin = seeded
    await admin.execute(
        "INSERT INTO auth_events (user_id, event) VALUES ($1, 'mfa_failed')", ALICE)

    with pytest.raises(asyncpg.PostgresError, match="not deletable"):
        await admin.execute("DELETE FROM auth_events WHERE user_id = $1", ALICE)


async def test_what_happened_cannot_be_rewritten(seeded):
    admin = seeded
    await admin.execute(
        "INSERT INTO auth_events (user_id, event) VALUES ($1, 'mfa_failed')", ALICE)

    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await admin.execute(
            "UPDATE auth_events SET event = 'mfa_succeeded' WHERE user_id = $1",
            ALICE)


async def test_attribution_cannot_be_reassigned_to_somebody_else(seeded):
    """Worse than deletion, and refused separately for that reason: a deleted
    row is visibly missing, and a reassigned one reads as the truth."""
    admin = seeded
    await admin.execute(
        "INSERT INTO auth_events (user_id, event) VALUES ($1, 'sign_in_succeeded')",
        ALICE)

    with pytest.raises(asyncpg.PostgresError, match="cleared, not reassigned"):
        await admin.execute(
            "UPDATE auth_events SET user_id = $1 WHERE user_id = $2", BOB, ALICE)


async def test_attribution_may_be_cleared(seeded):
    """The ONE permitted mutation. Erasure depends on it, so a trigger that
    refused everything would make the table unerasable rather than safe."""
    admin = seeded
    await admin.execute(
        "INSERT INTO auth_events (user_id, event) VALUES ($1, 'sign_in_succeeded')",
        ALICE)
    await admin.execute(
        "UPDATE auth_events SET user_id = NULL WHERE user_id = $1", ALICE)

    assert await _events(admin, user_id=ALICE) == []
    assert await _events(admin, event="sign_in_succeeded"), (
        "clearing the attribution took the row. What happened and when is the "
        "evidence; who it was about is the personal data.")


# ─────────────────────────────────────────────────────────────────────────────
# Reading — the caller's own history and nobody else's
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_person_reads_their_own_history(seeded):
    from core.identity import auth_audit

    admin = seeded
    await admin.execute(
        "INSERT INTO auth_events (user_id, tenant_id, event) "
        "VALUES ($1, $2, 'sign_in_succeeded')", ALICE, TENANT)

    history = await auth_audit.history(await _principal("alice"))
    assert [h["event"] for h in history] == ["sign_in_succeeded"]


async def test_a_colleague_cannot_read_it(seeded):
    """The rule `057` drew for `user_sessions`, held at the audit table.

    "When did my colleague last sign in, and from where" is a movement log, and
    it is no more a tenant administrator's to read because it lives in an audit
    table. Bob is a member of the same tenant, which is what makes this the
    interesting case rather than cross-tenant isolation restated.
    """
    from core.identity import auth_audit

    admin = seeded
    await admin.execute(
        "INSERT INTO auth_events (user_id, tenant_id, event) "
        "VALUES ($1, $2, 'sign_in_succeeded')", ALICE, TENANT)

    assert await auth_audit.history(await _principal("bob")) == [], (
        "a member of the same tenant read another member's authentication "
        "history. A tenant predicate alone is what `060` had to remove from "
        "two other tables in this same batch.")


async def test_an_unattributed_event_is_readable_by_nobody(seeded):
    """The failed-sign-in rows exist for the operator and for an incident. A
    tenant able to browse them could enumerate which addresses were tried."""
    from core.identity import auth_audit

    admin = seeded
    await admin.execute(
        "INSERT INTO auth_events (tenant_id, event) VALUES ($1, 'sign_in_refused')",
        TENANT)

    for name in ("alice", "bob"):
        assert await auth_audit.history(await _principal(name)) == [], (
            f"{name} read an unattributed sign-in refusal through the "
            f"application")


async def test_the_history_never_hands_out_the_stable_identifiers(seeded):
    """Keyed, so they are not addresses — but they are stable, and a list of
    them is a list of "the same place, again", which no API needs to return."""
    from core.identity import auth_audit

    admin = seeded
    await admin.execute(
        "INSERT INTO auth_events (user_id, event, subject_hash, ip_hash) "
        "VALUES ($1, 'sign_in_succeeded', $2, $3)", ALICE, "a" * 64, "b" * 64)

    history = await auth_audit.history(await _principal("alice"))
    assert history
    for entry in history:
        assert "subject_hash" not in entry and "ip_hash" not in entry
        assert "a" * 64 not in str(entry) and "b" * 64 not in str(entry)


# ─────────────────────────────────────────────────────────────────────────────
# Recording never changes an authentication outcome
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def unwritable_log(seeded):
    """Make the audit table genuinely unwritable for `ira_app`, and put it back.

    Deliberately NOT `monkeypatch.setattr(auth_audit, "record", explode)`. That
    patches away the very function whose contract is "never raises", so it can
    only ever prove that a caller handles a contract violation — a thing no
    caller is written to do and no caller should be. Here the DATABASE refuses
    the write, which is the failure that actually happens, and the refusal
    passes through `record`'s own handler exactly as it would in production.
    """
    admin = seeded
    await admin.execute("REVOKE INSERT ON auth_events FROM ira_app")
    try:
        yield admin
    finally:
        await admin.execute("GRANT INSERT ON auth_events TO ira_app")


async def test_a_broken_audit_table_does_not_refuse_a_login(unwritable_log):
    """The inversion of the rule the rest of Phase 2 follows, and deliberate.

    A logging outage that refused logins turns a bookkeeping problem into a
    total outage, and hands an attacker who can break the log a way to lock
    everybody out. The log is evidence, not a control.
    """
    from core.identity.cloud_login import authenticate_cloud_user

    outcome = await authenticate_cloud_user(email("alice"), PASSWORD,
                                            tenant_id=TENANT)
    assert outcome.principal.user_id == ALICE, (
        "a failure to write the audit trail refused a valid sign-in. That "
        "converts a logging outage into a total outage.")


async def test_a_broken_audit_table_does_not_refuse_a_password_reset(unwritable_log):
    """The same property at a second call site, because it is a property of the
    CALLERS as much as of `record` — a caller that wrapped the write in
    something of its own would break it for that path alone."""
    from core.identity.password_reset import request_password_reset

    outcome = await request_password_reset(email("alice"))
    assert outcome.issued and outcome.token


async def test_a_broken_audit_table_does_not_refuse_a_session_revocation(
        unwritable_log):
    from core.identity.sessions import record_session, revoke_all_sessions

    alice = await _principal("alice")
    await record_session(alice, refresh_jti="jti-audit-broken")
    assert await revoke_all_sessions(alice) == 1, (
        "a failure to write the audit trail stopped a session being revoked. "
        "The revocation is the security control; the log is the evidence.")


async def test_record_swallows_a_real_store_failure(unwritable_log):
    """`record` itself, with a real refusal underneath it."""
    from core.identity import auth_audit

    await auth_audit.record(auth_audit.SIGN_IN_SUCCEEDED, user_id=ALICE)


async def test_an_unknown_event_name_is_refused_before_it_reaches_the_table(seeded):
    admin = seeded
    from core.identity import auth_audit

    await auth_audit.record("whatever_i_felt_like", user_id=ALICE)
    assert await _events(admin, user_id=ALICE) == []


# ─────────────────────────────────────────────────────────────────────────────
# Erasure
# ─────────────────────────────────────────────────────────────────────────────


async def test_an_erasure_de_attributes_the_history(seeded):
    from core.privacy.erasure import erase_audit

    admin = seeded
    await admin.execute(
        "INSERT INTO auth_events (user_id, tenant_id, event, subject_hash, "
        " ip_hash, user_agent) "
        "VALUES ($1, $2, 'sign_in_succeeded', $3, $4, 'Firefox')",
        ALICE, TENANT, "c" * 64, "d" * 64)

    result = await erase_audit(await _principal("alice"), scope="account")
    assert result.ok, result.error

    assert await _events(admin, user_id=ALICE) == [], (
        "the authentication log still names the erased account")
    rows = await _events(admin, event="sign_in_succeeded")
    assert len(rows) == 1, "the redaction took the row instead of clearing it"
    assert rows[0]["subject_hash"] is None and rows[0]["ip_hash"] is None
    assert rows[0]["user_agent"] == ""


async def test_the_erasure_does_not_touch_anybody_elses_history(seeded):
    from core.privacy.erasure import erase_audit

    admin = seeded
    await admin.execute(
        "INSERT INTO auth_events (user_id, tenant_id, event) "
        "VALUES ($1, $2, 'sign_in_succeeded')", BOB, TENANT)

    await erase_audit(await _principal("alice"), scope="account")

    assert await _events(admin, user_id=BOB), (
        "erasing one account cleared a colleague's authentication history")


async def test_the_redaction_widening_does_not_expose_an_attributed_row(seeded):
    """`auth_event_redaction_select` exists so an erasure can update a row out
    of its own visibility. It must not become a way to READ somebody's history.

    Bob binds the redaction GUC to himself, correctly — and must still see only
    his own rows. The widening reaches ALREADY-unattributed rows and nothing
    else, and this is the assertion that says so rather than the comment.
    """
    admin = seeded
    await admin.execute(
        "INSERT INTO auth_events (user_id, event) VALUES ($1, 'sign_in_succeeded')",
        ALICE)

    from core.identity.db_context import identity_bootstrap_connection

    async with identity_bootstrap_connection(BOB) as conn:
        await conn.execute(
            "SELECT set_config('app.audit_redaction', $1, true)", str(BOB))
        rows = await conn.fetch("SELECT user_id FROM auth_events")

    assert rows == [], (
        "binding the redaction GUC exposed another account's attributed "
        "authentication history. The widening is for rows that carry no "
        "attribution at all.")


async def test_the_redaction_guc_must_name_the_account_it_is_bound_to(seeded):
    """The narrowing that stops it being a generic flag: the GUC has to MATCH
    `app.user_id`, so a caller cannot declare a redaction of somebody else."""
    admin = seeded
    await admin.execute(
        "INSERT INTO auth_events (event) VALUES ('sign_in_refused')")

    from core.identity.db_context import identity_bootstrap_connection

    async with identity_bootstrap_connection(BOB) as conn:
        await conn.execute(
            "SELECT set_config('app.audit_redaction', $1, true)", str(ALICE))
        mismatched = await conn.fetch("SELECT id FROM auth_events")
        await conn.execute(
            "SELECT set_config('app.audit_redaction', $1, true)", str(BOB))
        matched = await conn.fetch("SELECT id FROM auth_events")

    assert mismatched == [], (
        "a connection bound to Bob declared a redaction of Alice and was "
        "granted the widened read")
    assert len(matched) == 1, (
        "the widening did not apply even when the GUC matched the bound "
        "account, so `redact_for` cannot work and the policy is dead weight")


async def test_the_redaction_is_counted_in_the_erasure_result(seeded):
    """Silently-zero is what a wrongly-scoped statement looks like."""
    from core.privacy.erasure import erase_audit

    admin = seeded
    for _ in range(3):
        await admin.execute(
            "INSERT INTO auth_events (user_id, tenant_id, event) "
            "VALUES ($1, $2, 'sign_in_succeeded')", ALICE, TENANT)

    result = await erase_audit(await _principal("alice"), scope="account")
    assert result.redacted >= 3, (
        f"erase_audit reported {result.redacted} redactions for three "
        f"authentication events plus everything else it clears")


# ─────────────────────────────────────────────────────────────────────────────
# Schema shape
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_raw_address_is_not_representable(seeded):
    admin = seeded
    with pytest.raises(asyncpg.PostgresError, match="hashes_are_hashes"):
        await admin.execute(
            "INSERT INTO auth_events (event, subject_hash) "
            "VALUES ('sign_in_refused', $1)", email("alice"))


async def test_a_raw_ip_is_not_representable(seeded):
    admin = seeded
    with pytest.raises(asyncpg.PostgresError, match="hashes_are_hashes"):
        await admin.execute(
            "INSERT INTO auth_events (event, ip_hash) "
            "VALUES ('sign_in_refused', '203.0.113.9')")


async def test_the_detail_column_is_bounded(seeded):
    """An unbounded column on an audit table is where credential material
    eventually lands, because somebody logs "the request" to debug something."""
    admin = seeded
    with pytest.raises(asyncpg.PostgresError):
        await admin.execute(
            "INSERT INTO auth_events (event, detail) VALUES ('mfa_failed', $1)",
            "x" * 201)


async def test_deleting_a_tenant_does_not_delete_the_record_of_its_sign_ins(seeded):
    """ON DELETE SET NULL, never CASCADE. An organisation being wound up is
    precisely when somebody asks what happened in it."""
    admin = seeded
    await admin.execute(
        "INSERT INTO auth_events (user_id, tenant_id, event) "
        "VALUES ($1, $2, 'sign_in_succeeded')", ALICE, TENANT)

    await admin.execute("DELETE FROM tenant_memberships WHERE tenant_id = $1",
                        TENANT)
    await admin.execute("DELETE FROM tenants WHERE id = $1", TENANT)

    rows = await _events(admin, event="sign_in_succeeded")
    assert len(rows) == 1, (
        "deleting the tenant deleted the record of how its accounts were "
        "accessed")
    assert rows[0]["tenant_id"] is None
    assert rows[0]["user_id"] == ALICE, (
        "the tenant deletion also cleared the SUBJECT. Only the tenant "
        "attribution should have gone.")


async def test_deleting_a_user_does_not_delete_their_sign_in_record(seeded):
    admin = seeded
    await admin.execute(
        "INSERT INTO auth_events (user_id, event) VALUES ($1, 'sign_in_succeeded')",
        ALICE)

    await admin.execute("DELETE FROM tenant_memberships WHERE user_id = $1", ALICE)
    await admin.execute("DELETE FROM user_credentials WHERE user_id = $1", ALICE)
    await admin.execute("DELETE FROM auth_token_versions WHERE user_id = $1", ALICE)
    await admin.execute("DELETE FROM users WHERE id = $1", ALICE)

    rows = await _events(admin, event="sign_in_succeeded")
    assert len(rows) == 1 and rows[0]["user_id"] is None, (
        "deleting the account deleted its authentication history rather than "
        "de-attributing it")


async def test_the_event_vocabulary_matches_the_check_constraint(seeded):
    """A typo at a call site must be an ImportError, not a silently-dropped
    event — `record` swallows database failures by design, so a constraint
    violation would lose the event rather than surface it."""
    admin = seeded
    from core.identity import auth_audit

    for event in sorted(auth_audit.EVENTS):
        await admin.execute(
            "INSERT INTO auth_events (event) VALUES ($1)", event)

    stored = {r["event"] for r in await _events(admin)}
    assert stored == set(auth_audit.EVENTS), (
        f"the Python vocabulary and the CHECK disagree: "
        f"{sorted(set(auth_audit.EVENTS) ^ stored)}")
