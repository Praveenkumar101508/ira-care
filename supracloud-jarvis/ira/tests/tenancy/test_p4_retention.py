"""Phase 4 batch 06 — a retention period that is recorded is now applied.

`026` gave every user `retention_settings` and `core/privacy/store.py` has read
and written them since. **Nothing has ever expired because of them.** Each of
the three settings failed differently:

  * `conversation_retention_days` — read only by the settings page. No
    conversation has ever expired.
  * `memory_retention_days` — becomes a per-row `expires_at` at WRITE time, and
    `store.expire_due_memories`, the one function that acts on `expires_at`,
    **had no callers**. Rows written before the user chose a window carry
    `expires_at IS NULL` and could not have expired even once it ran.
  * `voice_retention_hours` — nothing to expire; no raw audio is stored.

That is worse than having no retention: the user was told their conversations
expire after N days, and they did not. `IMPLEMENTED_VS_PLANNED.md` has carried
`IMPLEMENTED_NOT_FULLY_VERIFIED` against this since Phase 0, which understated
it — the settings were implemented, the enforcement did not exist.

The properties worth reading, in rough order of how much they cost to get
wrong:

  * `auth_events` is reported and NOT deleted, because `061` made it
    append-only for every role including the owner. A retention job that
    dropped the trigger to expire rows would defeat the control by way of the
    feature meant to respect it.
  * an unset window means "keep indefinitely" and is INERT — the interpretation
    that makes a misconfiguration harmless instead of catastrophic.
  * the pass binds a WORKSPACE. All three tables it touches carry Batch 5C's
    workspace policies, so a tenant-bound connection would match zero rows in
    silence and report success. The deletion tests here are what makes that
    substitution fail loudly.
  * data past its window goes, counted rather than assumed.
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

TENANT = uuid.uuid4()
WORKSPACE = uuid.uuid4()
USER = uuid.uuid4()
OTHER_TENANT = uuid.uuid4()
OTHER_WORKSPACE = uuid.uuid4()
OTHER_USER = uuid.uuid4()


@pytest.fixture
async def pool():
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=6)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


@pytest.fixture
async def seeded(pool):
    """Two tenants, each with one user, a workspace and aged content."""
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await _wipe(admin)
        for tenant, workspace, user, slug in (
            (TENANT, WORKSPACE, USER, "p4ret-a"),
            (OTHER_TENANT, OTHER_WORKSPACE, OTHER_USER, "p4ret-b"),
        ):
            await admin.execute(
                "INSERT INTO tenants (id,name,slug,edition) "
                "VALUES ($1,$2,$3,'cloud')", tenant, f"Ret {slug}", slug)
            await admin.execute(
                "INSERT INTO users (id,display_name,auth_provider,status) "
                "VALUES ($1,'R','password','active')", user)
            await admin.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
                "VALUES ($1,$2,'member','active')", tenant, user)
            org = await admin.fetchval(
                "INSERT INTO organisations (tenant_id,name,slug) "
                "VALUES ($1,'O',$2) RETURNING id", tenant, slug)
            await admin.execute(
                # `is_default` is required: 033 refuses a tenant whose only
                # workspace is not the default, deferred to COMMIT.
                "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,"
                " is_default) VALUES ($1,$2,$3,'W',$4,TRUE)",
                workspace, tenant, org, slug)
            await admin.execute(
                # `tenant_id` is spelled out rather than left to a default: a
                # composite FK in 035 refuses a membership whose tenant does not
                # match its workspace's, so the two must be stated together.
                "INSERT INTO workspace_memberships "
                " (tenant_id,workspace_id,user_id,role) "
                "VALUES ($1,$2,$3,'member')", tenant, workspace, user)
        yield admin
    finally:
        await _wipe(admin)
        await admin.close()


async def _wipe(admin):
    for t in (TENANT, OTHER_TENANT):
        await admin.execute("DELETE FROM tenants WHERE id = $1", t)
    for u in (USER, OTHER_USER):
        await admin.execute("DELETE FROM users WHERE id = $1", u)


def _principal(tenant=TENANT, workspace=WORKSPACE, user=USER):
    from core.identity.principal import Principal

    return Principal(user_id=user, tenant_id=tenant, workspace_id=workspace,
                     session_id=uuid.uuid4(), edition="cloud",
                     username="", is_owner=False)


async def _conversation(admin, *, tenant, workspace, age_days: int) -> uuid.UUID:
    # Owned by the tenant's user, as the column default would set it for a
    # conversation that user started (093, ADR-0009). An unowned row is reached
    # by no personal window, which is not what these tests are about.
    owner = USER if tenant == TENANT else OTHER_USER
    cid = await admin.fetchval(
        "INSERT INTO conversations (tenant_id, workspace_id, owner_user_id, session_id, "
        " created_at) "
        "VALUES ($1, $2, $3, 'p4ret', NOW() - ($4 || ' days')::interval) RETURNING id",
        tenant, workspace, owner, str(age_days))
    await admin.execute(
        "INSERT INTO messages (conversation_id, tenant_id, workspace_id, owner_user_id, "
        " role, content, created_at) "
        "VALUES ($1,$2,$3,$4,'user','hello', NOW() - ($5 || ' days')::interval)",
        cid, tenant, workspace, owner, str(age_days))
    return cid


async def _settings(principal, **kw):
    from core.privacy.store import RetentionSettings, save_settings

    await save_settings(principal, RetentionSettings(**kw))


# ─────────────────────────────────────────────────────────────────────────────
# It actually removes things
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_conversation_past_its_window_is_removed(seeded):
    from core.privacy.retention import enforce_for

    admin = seeded
    principal = _principal()
    old = await _conversation(admin, tenant=TENANT, workspace=WORKSPACE, age_days=40)
    new = await _conversation(admin, tenant=TENANT, workspace=WORKSPACE, age_days=2)
    await _settings(principal, conversation_retention_days=30)

    outcome = await enforce_for(principal)
    assert outcome.conversations_deleted == 1
    assert outcome.messages_deleted == 1

    assert await admin.fetchval(
        "SELECT count(*) FROM conversations WHERE id = $1", old) == 0
    assert await admin.fetchval(
        "SELECT count(*) FROM conversations WHERE id = $1", new) == 1, (
        "a conversation inside its retention window was deleted")


async def test_the_messages_are_counted_rather_than_left_to_the_cascade(seeded):
    """The FK cascades, so deleting the conversation would take the messages
    anyway — but then the count is a guess, and a retention report that cannot
    say how many messages went is not evidence of anything."""
    from core.privacy.retention import enforce_for

    admin = seeded
    principal = _principal()
    cid = await _conversation(admin, tenant=TENANT, workspace=WORKSPACE, age_days=40)
    for _ in range(3):
        # With their owner, as every application write gets it (093, ADR-0009):
        # the insert policy refuses a message whose owner is not the caller.
        await admin.execute(
            "INSERT INTO messages (conversation_id,tenant_id,workspace_id,owner_user_id,role,"
            " content,created_at) VALUES ($1,$2,$3,$4,'user','x', NOW() - INTERVAL '40 days')",
            cid, TENANT, WORKSPACE, USER)
    await _settings(principal, conversation_retention_days=30)

    outcome = await enforce_for(principal)
    assert outcome.messages_deleted == 4


async def test_an_old_message_inside_a_live_conversation_survives(seeded):
    """The window is on the CONVERSATION, and the message DELETE selects by the
    conversation rather than by the message's own age.

    Dropping the subselect and matching `messages.created_at` directly would
    look equivalent and would quietly gut the history of every long-running
    conversation, leaving the conversation itself in place to hide it.
    """
    from core.privacy.retention import enforce_for

    admin = seeded
    principal = _principal()
    cid = await _conversation(admin, tenant=TENANT, workspace=WORKSPACE, age_days=2)
    # Owned, like the conversation (093, ADR-0009). An unowned message would be
    # invisible to the pass and survive for that reason alone — the test would
    # then pass even if the DELETE matched `messages.created_at` directly.
    await admin.execute(
        "INSERT INTO messages (conversation_id,tenant_id,workspace_id,owner_user_id,role,"
        " content,created_at) "
        "VALUES ($1,$2,$3,$4,'user','ancient', NOW() - INTERVAL '900 days')",
        cid, TENANT, WORKSPACE, USER)
    await _settings(principal, conversation_retention_days=30)

    outcome = await enforce_for(principal)
    assert outcome.messages_deleted == 0
    assert await admin.fetchval(
        "SELECT count(*) FROM messages WHERE conversation_id = $1", cid) == 2, (
        "a message was expired on its own age rather than its conversation's")


async def test_a_memory_past_its_window_is_removed(seeded):
    from core.privacy.retention import enforce_for

    admin = seeded
    principal = _principal()
    await admin.execute(
        "INSERT INTO memory_embeddings (tenant_id,workspace_id,user_id,source_type,"
        " content,created_at) "
        "VALUES ($1,$2,$3,'note','old', NOW() - INTERVAL '400 days')",
        TENANT, WORKSPACE, str(USER))
    await _settings(principal, memory_retention_days=365)

    outcome = await enforce_for(principal)
    assert outcome.memories_deleted == 1


async def test_a_memory_older_than_the_setting_itself_still_expires(seeded):
    """**The gap the per-row expiry could never close.**

    `memory_retention_days` becomes an `expires_at` at WRITE time. A memory
    stored before the user chose a window carries `expires_at IS NULL`, so
    `expire_due_memories` cannot see it however often it runs — the setting
    would apply only to the future, which is not what the page says it does.
    """
    from core.privacy.retention import enforce_for

    admin = seeded
    principal = _principal()
    mid = await admin.fetchval(
        "INSERT INTO memory_embeddings (tenant_id,workspace_id,user_id,source_type,"
        " content, expires_at, created_at) "
        "VALUES ($1,$2,$3,'note','predates the setting', NULL, "
        "        NOW() - INTERVAL '400 days') RETURNING id",
        TENANT, WORKSPACE, str(USER))
    await _settings(principal, memory_retention_days=365)

    outcome = await enforce_for(principal)
    assert outcome.memories_deleted == 1
    assert await admin.fetchval(
        "SELECT count(*) FROM memory_embeddings WHERE id = $1", mid) == 0


async def test_a_memory_with_an_early_expiry_is_expired_before_its_window(seeded):
    """The other half: `expires_at` may fall EARLIER than the window — a memory
    given a short life of its own, or a user who later shortened the setting.
    Age alone would keep it until the window ran out.

    This is the assertion that `store.expire_due_memories` is called at all. It
    had no callers before this batch.
    """
    from core.privacy.retention import enforce_for

    admin = seeded
    principal = _principal()
    mid = await admin.fetchval(
        "INSERT INTO memory_embeddings (tenant_id,workspace_id,user_id,source_type,"
        " content, expires_at, created_at) "
        "VALUES ($1,$2,$3,'note','short life', NOW() - INTERVAL '1 day', "
        "        NOW() - INTERVAL '2 days') RETURNING id",
        TENANT, WORKSPACE, str(USER))
    await _settings(principal, memory_retention_days=365)

    outcome = await enforce_for(principal)
    assert outcome.memories_expired == 1, (
        "a memory whose own expires_at had passed was left live: the retention "
        "pass is not calling store.expire_due_memories")
    assert outcome.memories_deleted == 0, (
        "the row is 2 days old and the window is 365 — age must not have "
        "reached it")
    assert await admin.fetchval(
        "SELECT deleted_at IS NOT NULL FROM memory_embeddings WHERE id = $1",
        mid) is True


# ─────────────────────────────────────────────────────────────────────────────
# It removes nothing it should not
# ─────────────────────────────────────────────────────────────────────────────


async def test_unset_means_keep_indefinitely_rather_than_delete_everything(seeded):
    """**The interpretation that decides whether a misconfiguration is inert or
    catastrophic.**

    A retention job that read "no window" as "expire immediately" would delete a
    user's entire history the first time somebody left the field blank.
    """
    from core.privacy.retention import enforce_for

    admin = seeded
    principal = _principal()
    old = await _conversation(admin, tenant=TENANT, workspace=WORKSPACE, age_days=9999)
    await _settings(principal, conversation_retention_days=None,
                    memory_retention_days=None)

    outcome = await enforce_for(principal)
    assert outcome.conversations_deleted == 0
    assert await admin.fetchval(
        "SELECT count(*) FROM conversations WHERE id = $1", old) == 1
    assert any("indefinitely" in n for n in outcome.notes)


async def test_the_schema_refuses_a_zero_window_before_the_code_has_to(seeded):
    """`enforce_for` guards `days and days > 0`, which reads like a defence
    against a stored 0. It is the SECOND defence — `026` already constrains the
    column to `IS NULL OR > 0`, so a 0 cannot reach the table at all.

    Asserting the CHECK is what stops the code guard from being the only thing
    standing there. A test that only exercised the guard could not tell the
    first line of defence from the last, and so could not notice the first one
    falling.
    """
    admin = seeded
    for column in ("conversation_retention_days", "memory_retention_days"):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await admin.execute(
                f"INSERT INTO retention_settings (tenant_id,user_id,{column}) "
                f"VALUES ($1,$2,0)", TENANT, USER)


async def test_another_tenants_data_is_untouched(seeded):
    """The predicate only adds AGE. Which rows are reachable is RLS's decision,
    and a retention job that carried its own tenant filter would be one typo
    from expiring somebody else's history."""
    from core.privacy.retention import enforce_for

    admin = seeded
    theirs = await _conversation(admin, tenant=OTHER_TENANT,
                                 workspace=OTHER_WORKSPACE, age_days=9999)
    mine = await _conversation(admin, tenant=TENANT, workspace=WORKSPACE,
                               age_days=9999)
    await _settings(_principal(), conversation_retention_days=1)

    await enforce_for(_principal())

    assert await admin.fetchval(
        "SELECT count(*) FROM conversations WHERE id = $1", theirs) == 1, (
        "a retention pass for one tenant deleted another tenant's conversation")
    assert await admin.fetchval(
        "SELECT count(*) FROM conversations WHERE id = $1", mine) == 0


async def test_a_user_with_no_settings_row_deletes_nothing(seeded):
    """`get_settings` returns the conservative default for a user who has never
    opened the page. Those defaults must not expire anything."""
    from core.privacy.retention import enforce_for

    admin = seeded
    old = await _conversation(admin, tenant=TENANT, workspace=WORKSPACE, age_days=9999)

    outcome = await enforce_for(_principal())
    assert not outcome.anything_removed
    assert await admin.fetchval(
        "SELECT count(*) FROM conversations WHERE id = $1", old) == 1


# ─────────────────────────────────────────────────────────────────────────────
# What it deliberately cannot do
# ─────────────────────────────────────────────────────────────────────────────


async def test_auth_events_are_reported_and_never_deleted(seeded):
    """**The limitation, asserted rather than worked around.**

    `061` made the authentication log append-only for every role including the
    owner, deliberately: "the first thing an intruder does is remove the entry
    that shows them arriving." Per-row expiry is therefore not expressible.

    A retention job that dropped the trigger to expire rows would defeat the
    control by way of the feature meant to respect it. This one counts and says
    so, and removal stays the operator action `061`'s rollback describes.
    """
    from core.privacy.retention import enforce_for

    admin = seeded
    await admin.execute(
        "INSERT INTO auth_events (user_id, tenant_id, event, created_at) "
        "VALUES ($1,$2,'sign_in_succeeded', NOW() - INTERVAL '400 days')",
        USER, TENANT)

    outcome = await enforce_for(_principal(), auth_event_retention_days=365)
    assert outcome.auth_events_past_retention == 1
    assert any("NOT deleted" in n and "operator" in n for n in outcome.notes)

    assert await admin.fetchval(
        "SELECT count(*) FROM auth_events WHERE user_id = $1", USER) == 1, (
        "the retention pass deleted an authentication event. 061 forbids it for "
        "every role, and a job that found a way around that has removed the "
        "record of how accounts were accessed.")


async def test_a_second_workspace_is_reported_as_not_covered(seeded):
    """A pass binds one workspace, so it enforces retention in one workspace.
    Saying nothing about the rest is what turns a partial result into a partial
    result that reads as complete."""
    from core.privacy.retention import enforce_for

    admin = seeded
    second = uuid.uuid4()
    org = await admin.fetchval(
        "SELECT organisation_id FROM workspaces WHERE id = $1", WORKSPACE)
    await admin.execute(
        "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default) "
        "VALUES ($1,$2,$3,'Second','p4ret-a-2',FALSE)", second, TENANT, org)
    await admin.execute(
        "INSERT INTO workspace_memberships (tenant_id,workspace_id,user_id,role) "
        "VALUES ($1,$2,$3,'member')", TENANT, second, USER)

    outcome = await enforce_for(_principal())
    assert outcome.workspaces_not_covered == 1
    assert any("not touched" in n for n in outcome.notes)


async def test_another_workspaces_conversation_survives_this_workspaces_pass(seeded):
    """The other half of the same fact, asserted on data rather than on a
    counter: the bound workspace is the boundary, and a pass cannot reach past
    it however aggressive the window."""
    from core.privacy.retention import enforce_for

    admin = seeded
    second = uuid.uuid4()
    org = await admin.fetchval(
        "SELECT organisation_id FROM workspaces WHERE id = $1", WORKSPACE)
    await admin.execute(
        "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default) "
        "VALUES ($1,$2,$3,'Second','p4ret-a-2',FALSE)", second, TENANT, org)
    await admin.execute(
        "INSERT INTO workspace_memberships (tenant_id,workspace_id,user_id,role) "
        "VALUES ($1,$2,$3,'member')", TENANT, second, USER)
    elsewhere = await _conversation(admin, tenant=TENANT, workspace=second,
                                    age_days=9999)
    here = await _conversation(admin, tenant=TENANT, workspace=WORKSPACE,
                               age_days=9999)
    await _settings(_principal(), conversation_retention_days=1)

    await enforce_for(_principal())

    assert await admin.fetchval(
        "SELECT count(*) FROM conversations WHERE id = $1", here) == 0
    assert await admin.fetchval(
        "SELECT count(*) FROM conversations WHERE id = $1", elsewhere) == 1, (
        "a pass bound to one workspace deleted a conversation in another")


async def test_voice_retention_reports_that_there_is_nothing_to_expire(seeded):
    """`realtime_sessions.audio_stored` is GENERATED ALWAYS AS (FALSE), so no
    raw audio exists. Running a DELETE that matches zero rows forever and
    reporting it as work is how a control comes to look implemented."""
    from core.privacy.retention import enforce_for

    await _settings(_principal(), voice_retention_hours=24)
    outcome = await enforce_for(_principal())
    assert any("no raw audio is stored" in n for n in outcome.notes)


# ─────────────────────────────────────────────────────────────────────────────
# It has a caller
# ─────────────────────────────────────────────────────────────────────────────


def test_the_privacy_centre_can_actually_run_a_pass():
    """**The failure this whole batch is about, guarded against recurring.**

    `store.expire_due_memories` was correct, documented, and had no callers, so
    nothing failed when it never ran. A retention module reachable only from its
    own test suite is the same module with a longer docstring.
    """
    from api.routes import privacy as privacy_routes

    routes = {
        (r.path, method)
        for r in privacy_routes.router.routes
        for method in getattr(r, "methods", ())
    }
    assert ("/privacy/settings/enforce", "POST") in routes, (
        "core.privacy.retention has no route: nothing outside the tests can "
        "make a retention pass happen")


async def test_the_endpoint_reports_what_the_pass_could_not_do(monkeypatch):
    """A handler that returned only the deletion counts would read as a clean
    success while `auth_events` sat past retention and other workspaces went
    untouched. Those two fields are the honest part of the response."""
    from api.routes import privacy as privacy_routes
    from core.privacy.retention import RetentionOutcome

    async def _fake_enforce(principal, **kw):
        return RetentionOutcome(
            conversations_deleted=2, messages_deleted=5,
            memories_expired=1, memories_deleted=3,
            auth_events_past_retention=7, workspaces_not_covered=4,
            notes=("a note",))

    async def _fake_event(principal, kind, detail):
        return None

    monkeypatch.setattr(privacy_routes._retention, "enforce_for", _fake_enforce)
    monkeypatch.setattr(privacy_routes._store, "record_privacy_event", _fake_event)

    # P9-03 turned `privacy._principal` from a function the handler called into
    # a FastAPI dependency, so the principal arrives as an argument and there is
    # nothing left to patch. Handing one in is closer to what the route now
    # promises: it RECEIVES a verified principal rather than going to find one.
    body = await privacy_routes.enforce_retention(principal=_principal())
    assert body["auth_events_past_retention_not_deleted"] == 7, (
        "the endpoint dropped the count of events it may not delete")
    assert body["workspaces_not_covered"] == 4, (
        "the endpoint dropped the count of workspaces the pass did not cover")
    assert body["memories_expired"] == 1 and body["memories_deleted"] == 3
    assert body["notes"] == ["a note"]


async def test_the_generated_column_still_forbids_stored_audio(seeded):
    """The note above is a claim about the schema. This is the claim's evidence
    — without it the note becomes a comment that outlives the thing it
    describes."""
    admin = seeded
    generated = await admin.fetchval(
        "SELECT is_generated FROM information_schema.columns "
        " WHERE table_name = 'realtime_sessions' AND column_name = 'audio_stored'")
    assert generated == "ALWAYS", (
        "realtime_sessions.audio_stored is no longer generated, so raw audio "
        "may now be stored and voice_retention_hours has something to enforce")
