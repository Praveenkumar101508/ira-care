"""Phase 5 batch 02 — the registry service, and the gateway's use of it.

`067` holds the tables; `core/models/registry.py` is the only thing that reads
them, and the gateway is the only thing that acts on the answer. This suite
covers both, against the real schema.

── The failure class this batch is most exposed to ─────────────────────────

"Function exists but has no caller." P4-06 shipped retention whose one consumer
had no callers, and P4-05 found a rotation step that reached two of three
families of protected material. A registry module with a beautiful API that
nothing consults would be the same defect wearing MOD-03's clothes — so the
gateway tests below drive `IRAModelGateway.complete` rather than calling the
registry directly.
"""
from __future__ import annotations

import os
import uuid

import pytest

from tests.tenancy._licence_fixture import approved_licence, drop_licence

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

HASH = "sha256:" + "c" * 64
LICENCE = "test-fixture-not-a-determination"


@pytest.fixture
async def pool():
    """The application pool, as `ira_app`.

    Not the admin DSN. The registry is read by the runtime role in production
    and a superuser fixture would hide a missing SELECT grant — the P4-11 shape,
    where an admin DSN concealed a scheduler reading through the wrong
    connection entirely.
    """
    import utils.db as db_module

    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous = db_module._pool
    db_module._pool = created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


class _Operator:
    """Registration is an operator act, so the fixture performs it as one.

    `ira_app` cannot INSERT here by design (`067` revokes it), which means a
    fixture that seeded through the application pool would fail — and that
    failure is the control working.

    ── Why this COMMITS rather than rolling back ───────────────────────────

    The first version held everything in one open transaction and rolled it
    back, which is the right shape for a schema test and the wrong one here:
    the registry is read through the APPLICATION pool, on a different
    connection, which cannot see an uncommitted row. Three tests failed with
    "a serving model was registered and not found" — the registration was
    invisible, not absent.

    So rows are committed and removed by id at teardown. Tracking the ids
    matters: a blanket `DELETE FROM model_registry` would remove rows another
    test — or another developer's database — legitimately holds.
    """

    def __init__(self, conn):
        self._conn = conn
        self._models: list = []
        self._allowlist: list = []
        self._licences: list = []

    async def execute(self, *args, **kw):
        return await self._conn.execute(*args, **kw)

    async def fetchval(self, *args, **kw):
        return await self._conn.fetchval(*args, **kw)

    async def fetch(self, *args, **kw):
        return await self._conn.fetch(*args, **kw)

    def track_model(self, key):
        self._models.append(key)

    def track_allowlist(self, identifier):
        self._allowlist.append(identifier)

    def track_licence(self, licence_id):
        self._licences.append(licence_id)

    async def cleanup(self):
        for key in self._models:
            await self._conn.execute(
                "DELETE FROM model_registry WHERE model_key = $1", key)
        # Licences come off AFTER the models that reference them: 068's
        # licence_id is ON DELETE RESTRICT, so the other order fails and leaves
        # both rows behind for every later run.
        for licence_id in self._licences:
            await drop_licence(self._conn, licence_id)
        for ident in self._allowlist:
            await self._conn.execute(
                "DELETE FROM model_pull_allowlist WHERE identifier = $1", ident)


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
    return f"p5svc{uuid.uuid4().hex[:10]}"


async def _allowlist(admin, identifier, revision):
    await admin.execute(
        "INSERT INTO model_pull_allowlist (source, identifier, revision) "
        "VALUES ('huggingface', $1, $2) ON CONFLICT DO NOTHING",
        identifier, revision)
    admin.track_allowlist(identifier)


async def _register(admin, key, *, environment="development", serving=True,
                    version=1, revision="9f8e7d6c", licence_id=None):
    """Register a model version, minting a fixture licence approval if it serves.

    068 made `status = 'serving'` require a `model_licence_register` row
    classified `known_approved`, so a serving row now needs `licence_id`.
    That is the migration working: before it, `commercial_licence = 'x'`
    satisfied the schema, and this fixture used a string for exactly that
    reason. The approval it mints names itself a fixture in every column a
    reader sees — see `_licence_fixture.py`.
    """
    if serving and licence_id is None:
        licence_id = await approved_licence(admin, reference=f"{key}:{version}")
        admin.track_licence(licence_id)
    await admin.execute(
        "INSERT INTO model_registry (model_key, version, base_model, revision, "
        " artifact_hash, environment, commercial_licence, licence_recorded_at, "
        " status, rollout_percentage, licence_id) "
        "VALUES ($1,$2,'org/model',$3,$4,$5,$6,$7,$8,$9,$10)",
        key, version, revision, HASH, environment,
        LICENCE if serving else None,
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        if serving else None,
        "serving" if serving else "registered",
        # A serving row must reach somebody — 067's serving_reaches_somebody.
        100 if serving else 0,
        licence_id)
    admin.track_model(key)
    return licence_id


# ─────────────────────────────────────────────────────────────────────────────
# The service
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_serving_model_is_found(pool, admin):
    from core.models import registry

    key = _key()
    await _register(admin, key)
    found = await registry.serving(key)
    assert found is not None, "a serving model was registered and not found"
    assert found.model_key == key
    assert found.artifact_hash == HASH


async def test_a_registered_but_not_serving_model_is_not_found(pool, admin):
    """`registered` means "we know about it". Only `serving` means "it may
    answer" — and the distinction is the whole point of having a status."""
    from core.models import registry

    key = _key()
    await _register(admin, key, serving=False)
    assert await registry.serving(key) is None


async def test_a_model_serving_in_another_environment_is_not_visible(pool, admin):
    """MOD-02's "no separation between development, staging and production
    model registries", enforced rather than described.

    The environment comes from the server. There is no parameter for it, so a
    caller cannot ask the production registry a development question.
    """
    from core.models import registry

    key = _key()
    await _register(admin, key, environment="production")
    assert registry.current_environment() == "development"
    assert await registry.serving(key) is None, (
        "a model registered as serving in production answered a development "
        "lookup")


async def test_the_environment_cannot_be_supplied_by_a_caller():
    """Asserted on the signature, because this is a property of the API rather
    than of a value: an `environment=` parameter is all it takes for a request
    to choose which rules apply to it."""
    import inspect

    from core.models import registry

    for fn in (registry.serving, registry.for_tenant):
        params = set(inspect.signature(fn).parameters)
        assert "environment" not in params, (
            f"{fn.__name__} accepts an environment, so a caller can select "
            f"which registry to be judged against")


# ─────────────────────────────────────────────────────────────────────────────
# The allowlist
# ─────────────────────────────────────────────────────────────────────────────


async def test_an_allowlisted_artefact_at_the_right_revision_is_permitted(pool, admin):
    from core.models import registry

    ident = f"org/{_key()}"
    await admin.execute(
        "INSERT INTO model_pull_allowlist (source, identifier, revision) "
        "VALUES ('huggingface', $1, 'abc123')", ident)
    admin.track_allowlist(ident)
    entry = await registry.require_allowlisted("huggingface", ident, "abc123")
    assert entry.revision == "abc123"


async def test_the_same_artefact_at_a_different_revision_is_refused(pool, admin):
    """**The interesting refusal.**

    The identifier is on the list. A check that asked "is this model
    allowlisted" would pass, and a different artefact would arrive. That is the
    shape a supply-chain substitution takes, and it is why the allowlist is
    keyed on the revision rather than on the name.
    """
    from core.models import registry

    ident = f"org/{_key()}"
    await admin.execute(
        "INSERT INTO model_pull_allowlist (source, identifier, revision) "
        "VALUES ('huggingface', $1, 'abc123')", ident)
    admin.track_allowlist(ident)
    with pytest.raises(registry.ArtefactNotAllowlisted) as exc:
        await registry.require_allowlisted("huggingface", ident, "def456")
    assert "abc123" in str(exc.value), (
        "the refusal does not say which revisions ARE allowed, so an operator "
        "cannot tell a typo from a substitution")


@pytest.mark.parametrize("revision", ["latest", "main", "HEAD", ""])
async def test_an_unpinned_fetch_is_refused_before_the_list_is_even_read(
        pool, revision):
    """MOD-02 verbatim: "it fetches whatever the Hub serves at that moment".

    Refused ahead of the lookup on purpose — an allowlist entry at revision
    'latest' could not exist (067 forbids it), but a caller passing 'latest'
    must get an answer about pinning rather than a confusing "not on the list".
    """
    from core.models import registry

    with pytest.raises(registry.ArtefactNotAllowlisted) as exc:
        await registry.require_allowlisted("huggingface", "org/x", revision)
    assert "pin" in str(exc.value).lower()


async def test_an_unknown_artefact_is_refused(pool):
    from core.models import registry

    with pytest.raises(registry.ArtefactNotAllowlisted):
        await registry.require_allowlisted("huggingface", f"org/{_key()}", "abc123")


# ─────────────────────────────────────────────────────────────────────────────
# The gateway actually consults it
# ─────────────────────────────────────────────────────────────────────────────


def _gateway(*, required: bool, model_key: str = "fast",
             honour: bool = True, calls: list | None = None):
    """A gateway with one local provider that records every invocation.

    `calls` is the load-bearing part. P5-07's claim is that an unapproved model
    produces ZERO provider calls — not a discarded answer — and the only way to
    assert that is to count.

    `honour=False` models a provider that ignores `approved_model` and runs
    something else, which is the substitution the contract exists to prevent.
    """
    from core.interfaces.model_provider import (
        CompletionResult, Locality, ProviderDisclosure, Tier)
    from core.models.gateway import IRAModelGateway

    seen = calls if calls is not None else []
    disclosure = ProviderDisclosure(
        provider_id="local", display_name="Local", locality=Locality.LOCAL,
        retains_prompts=False, region="on-device", trains_on_data=False)

    class _Provider:
        provider_id = "local"

        def disclosure(self):
            return disclosure

        def supports(self, tier):
            return True

        async def complete(self, request):
            seen.append(request)
            approved = request.approved_model
            identity = ""
            if approved is not None:
                identity = (approved.identity if honour
                            else "some-other-model@1:deadbeef")
            return CompletionResult(
                text="hello", model=(approved.model_key if approved else "?"),
                provider_id="local", disclosure=disclosure,
                prompt_tokens=1, completion_tokens=1,
                model_identity=identity)

        async def stream(self, request):  # pragma: no cover - unused
            yield "hello"

    gw = IRAModelGateway(registry_required=required,
                         tier_model_keys={"fast": model_key})
    gw.register(_Provider())
    return gw, Tier, seen


async def _ask(gw, Tier, tenant="tenant-1"):
    from core.interfaces.model_provider import CompletionRequest

    class _P:
        tenant_id = tenant

    return await gw.complete(
        CompletionRequest(messages=[{"role": "user", "content": "hi"}],
                          tier=Tier.FAST),
        principal=_P())


async def test_an_unregistered_model_is_never_invoked(pool):
    """**The defect the owner rejected Phase 5 for.**

    The first version checked `CompletionResult.model` after the call, so an
    unregistered model still ran: the prompt reached it and only the answer was
    thrown away. "IRA does not use unapproved models" was not true — "IRA does
    not return their output" was.

    The assertion is the call count, not the return value.
    """
    from core.models.gateway import RoutingRefusal

    gw, Tier, calls = _gateway(required=True, model_key=_key())
    out = await _ask(gw, Tier)
    assert isinstance(out, RoutingRefusal), (
        f"expected a refusal, got {type(out).__name__}")
    assert calls == [], (
        f"the provider was invoked {len(calls)} time(s) for a model that is "
        f"not registered — the prompt reached a model IRA had not approved")


async def test_a_registered_model_is_invoked_with_its_exact_revision(pool, admin):
    """The positive control, and the version half of MOD-03.

    Without it the test above passes against a gateway that refuses everything;
    without the revision assertion, "approved" would mean a name again.
    """
    from core.interfaces.model_provider import CompletionResult

    key = _key()
    await _register(admin, key, revision="9f8e7d6c")
    await _allowlist(admin, "org/model", "9f8e7d6c")
    gw, Tier, calls = _gateway(required=True, model_key=key)
    out = await _ask(gw, Tier)

    assert isinstance(out, CompletionResult), f"registered model refused: {out}"
    assert len(calls) == 1
    approved = calls[0].approved_model
    assert approved is not None, "the provider was called with no approved model"
    assert approved.model_key == key
    assert approved.revision == "9f8e7d6c", (
        f"the provider was told to run revision {approved.revision!r}")
    assert out.model_identity == approved.identity


async def test_a_provider_that_runs_something_else_is_refused(pool, admin):
    """Selection is not enough on its own. A provider that ignores
    `approved_model` and one that honours it are indistinguishable until it
    reports what it ran."""
    from core.models.gateway import RoutingRefusal

    key = _key()
    await _register(admin, key)
    await _allowlist(admin, "org/model", "9f8e7d6c")
    gw, Tier, calls = _gateway(required=True, model_key=key, honour=False)
    out = await _ask(gw, Tier)

    assert isinstance(out, RoutingRefusal), (
        "a provider ran a different model and its answer was returned")
    assert len(calls) == 1, "the call did happen — this check is about the answer"
    assert "not the one IRA approved" in out.reason


async def test_a_provider_that_reports_nothing_is_refused(pool, admin):
    """Silence is not consent. A provider that forgets to report what it ran is
    treated as a failure, because it is indistinguishable from one that
    substituted."""
    from core.models.gateway import RoutingRefusal

    key = _key()
    await _register(admin, key)
    await _allowlist(admin, "org/model", "9f8e7d6c")
    gw, Tier, _ = _gateway(required=True, model_key=key, honour=True)
    for provider in gw.providers.values():
        original = provider.complete

        async def _silent(request, _o=original):
            from dataclasses import replace as _replace
            return _replace(await _o(request), model_identity="")

        provider.complete = _silent
    out = await _ask(gw, Tier)
    assert isinstance(out, RoutingRefusal), (
        "a provider that reported no model identity was believed")


async def test_a_request_cannot_name_the_model_that_approves_it(pool, admin):
    """The tenancy rule, applied to the registry: authority is derived from the
    server, never accepted from request data. A caller that could set
    `approved_model` could choose the version whose rollout it prefers."""
    from core.interfaces.model_provider import (
        ApprovedModel, CompletionRequest, Tier)
    from core.models.gateway import RoutingRefusal

    key = _key()
    await _register(admin, key)
    await _allowlist(admin, "org/model", "9f8e7d6c")
    gw, _Tier, calls = _gateway(required=True, model_key=key)

    class _P:
        tenant_id = "tenant-1"

    out = await gw.complete(
        CompletionRequest(
            messages=[{"role": "user", "content": "hi"}], tier=Tier.FAST,
            approved_model=ApprovedModel(
                model_key="anything", version=99, base_model="org/x",
                revision="ffffff", artifact_hash="sha256:" + "f" * 64)),
        principal=_P())
    assert isinstance(out, RoutingRefusal)
    assert calls == [], "a caller-supplied approval reached a provider"


async def test_a_model_whose_artefact_is_not_allowlisted_is_never_invoked(
        pool, admin):
    """Registered is not sufficient. MOD-02 and MOD-03 are separate findings,
    and a registered model whose artefact may not be fetched at that revision is
    a supply-chain hole with a licence attached."""
    from core.models.gateway import RoutingRefusal

    key = _key()
    await _register(admin, key, revision="abc999")
    # deliberately NOT allowlisted
    gw, Tier, calls = _gateway(required=True, model_key=key)
    out = await _ask(gw, Tier)
    assert isinstance(out, RoutingRefusal)
    assert calls == [], "an unallowlisted artefact reached a provider"
    assert "allowlist" in out.reason.lower() or "artefact" in out.reason.lower()


async def test_enforcement_off_does_not_consult_the_registry_at_all(pool):
    """Off must mean *not consulted*, not *consulted and ignored*. The second
    pays a query per request for nothing and is one edit away from reading it
    wrong with nobody noticing."""
    import core.models.registry as registry_module
    from core.interfaces.model_provider import CompletionResult

    seen = []
    original = registry_module.resolve_for_request

    async def _counting(model_key, tenant_id):
        seen.append(model_key)
        return await original(model_key, tenant_id)

    registry_module.resolve_for_request = _counting
    try:
        gw, Tier, calls = _gateway(required=False, model_key=_key())
        out = await _ask(gw, Tier)
    finally:
        registry_module.resolve_for_request = original

    assert isinstance(out, CompletionResult), "enforcement off, still refused"
    assert seen == [], f"the registry was consulted {len(seen)} time(s)"
    assert len(calls) == 1


async def test_an_unreadable_registry_refuses_before_invoking_anything(pool):
    """Fail closed, and fail EARLY. Treating an unreadable registry as "allow"
    turns a database blip into an unapproved model answering a paying tenant."""
    import core.models.registry as registry_module
    from core.models.gateway import RoutingRefusal

    async def _broken(model_key, tenant_id):
        raise RuntimeError("registry is down")

    original = registry_module.resolve_for_request
    registry_module.resolve_for_request = _broken
    try:
        gw, Tier, calls = _gateway(required=True, model_key=_key())
        out = await _ask(gw, Tier)
    finally:
        registry_module.resolve_for_request = original

    assert isinstance(out, RoutingRefusal)
    assert calls == [], "an unreadable registry still let a provider run"
    assert "could not be read" in out.reason.lower()


async def test_a_refused_request_is_not_metered_as_a_success(pool):
    recorded = []
    gw, Tier, _ = _gateway(required=True, model_key=_key())
    gw.usage_writer = lambda **kw: recorded.append(kw)
    await _ask(gw, Tier)
    assert "ok" not in [r.get("outcome") for r in recorded]


# ─────────────────────────────────────────────────────────────────────────────
# Rollout and rollback (P5-04, MOD-03)
# ─────────────────────────────────────────────────────────────────────────────


def test_the_rollout_bucket_survives_a_restart():
    """**The defect this exists to prevent.**

    `hash()` on a string is randomised per process unless PYTHONHASHSEED is
    fixed, so `hash(tenant) % 100` puts a tenant in a different bucket after
    every deploy. A tenant flipping between two model versions on each restart
    is the opposite of a controlled rollout, and every individual request looks
    correct while it happens.

    Asserted against a value computed in a SEPARATE interpreter, because the
    same process would agree with itself either way — which is exactly why the
    bug survives review.
    """
    import subprocess
    import sys
    import pathlib

    from core.models import registry

    here = registry.rollout_bucket("qwen3-fast", "tenant-abc")
    root = pathlib.Path(registry.__file__).resolve().parents[3]
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r);"
         "from core.models.registry import rollout_bucket;"
         "print(rollout_bucket('qwen3-fast', 'tenant-abc'))" % str(root)],
        capture_output=True, text=True, timeout=60,
        env={"PYTHONHASHSEED": "random", "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    assert int(out.stdout.strip()) == here, (
        "the rollout bucket differs between processes, so a tenant changes "
        "model version on every restart")


def test_two_models_at_the_same_percentage_do_not_pick_the_same_tenants():
    """Otherwise one unlucky tenant is the canary for everything at once, and
    a bad week for them looks like a platform-wide outage in the metrics."""
    from core.models import registry

    tenants = [f"t{i}" for i in range(400)]
    a = {t for t in tenants if registry.rollout_bucket("model-a", t) < 10}
    b = {t for t in tenants if registry.rollout_bucket("model-b", t) < 10}
    assert a and b, "neither model selected anybody at 10% of 400 tenants"
    overlap = len(a & b) / max(len(a), 1)
    assert overlap < 0.5, (
        f"{overlap:.0%} of model-a's canaries are also model-b's; the bucket "
        f"is not keyed on the model")


async def test_a_full_rollout_reaches_every_tenant(pool, admin):
    from core.models import registry

    key = _key()
    await _register(admin, key)
    await admin.execute(
        "UPDATE model_registry SET rollout_percentage = 100 WHERE model_key = $1",
        key)
    for tenant in ("t1", "t2", "t3", "t4", "t5"):
        got = await registry.for_tenant(key, tenant)
        assert got is not None and got.model_key == key, tenant


async def test_a_partial_rollout_splits_the_tenants(pool, admin):
    """The property that makes a percentage a percentage: some tenants get the
    new version and the rest get the one it replaced — deterministically."""
    from core.models import registry

    key = _key()
    await _register(admin, key, version=1, revision="aaa111")
    old_id = await admin.fetchval(
        "SELECT id FROM model_registry WHERE model_key = $1 AND version = 1", key)
    await admin.execute(
        "UPDATE model_registry SET status = 'retired' WHERE id = $1", old_id)
    await _register(admin, key, version=2, revision="bbb222")
    await admin.execute(
        "UPDATE model_registry SET rollout_percentage = 50, rollback_target = $2 "
        " WHERE model_key = $1 AND version = 2", key, old_id)

    tenants = [f"t{i}" for i in range(200)]
    versions = {}
    for tenant in tenants:
        got = await registry.for_tenant(key, tenant)
        assert got is not None, tenant
        versions[tenant] = got.version

    on_new = sum(1 for v in versions.values() if v == 2)
    assert 60 < on_new < 140, (
        f"{on_new}/200 tenants are on the new version at a 50% rollout; the "
        f"split is not roughly the percentage")

    # And it is stable, which is the whole point.
    for tenant in tenants[:20]:
        again = await registry.for_tenant(key, tenant)
        assert again.version == versions[tenant], (
            f"{tenant} moved between versions on a second read")


async def test_a_partial_rollout_with_no_target_serves_nobody_the_new_model(
        pool, admin):
    """Fail closed. Inventing a fallback — serving the new model to the
    remainder — would make the percentage decorative while looking like it
    worked, which is worse than refusing."""
    from core.models import registry

    key = _key()
    await _register(admin, key)
    await admin.execute(
        "UPDATE model_registry SET rollout_percentage = 1 WHERE model_key = $1",
        key)
    outside = [t for t in (f"t{i}" for i in range(50))
               if registry.rollout_bucket(key, t) >= 1]
    assert outside, "no tenant fell outside a 1% rollout"
    assert await registry.for_tenant(key, outside[0]) is None


async def test_a_rollback_takes_effect_for_every_tenant_at_once(pool, admin):
    """Rolling back is an operator UPDATE — `ira_app` cannot write here. What
    the runtime must guarantee is that the change is seen immediately and by
    everybody, with no cached assignment surviving it."""
    from core.models import registry

    key = _key()
    await _register(admin, key, version=1, revision="aaa111")
    old_id = await admin.fetchval(
        "SELECT id FROM model_registry WHERE model_key = $1 AND version = 1", key)
    await admin.execute(
        "UPDATE model_registry SET status='retired' WHERE id = $1", old_id)
    await _register(admin, key, version=2, revision="bbb222")
    await admin.execute(
        "UPDATE model_registry SET rollout_percentage = 100 WHERE model_key = $1 "
        "  AND version = 2", key)

    for tenant in ("t1", "t2", "t3"):
        assert (await registry.for_tenant(key, tenant)).version == 2

    # The operator rolls back: version 2 stands down, version 1 serves again.
    await admin.execute(
        "UPDATE model_registry SET status='rolled_back', rollback_target = $2 "
        " WHERE model_key = $1 AND version = 2", key, old_id)
    # The percentage goes back with the status. 067 refuses to let it not:
    # a serving row at 0% would serve nobody, silently.
    await admin.execute(
        "UPDATE model_registry SET status='serving', rollout_percentage = 100 "
        " WHERE id = $1", old_id)

    for tenant in ("t1", "t2", "t3"):
        got = await registry.for_tenant(key, tenant)
        assert got is not None and got.version == 1, (
            f"{tenant} is still on version {got.version if got else None} after "
            f"a rollback")


# ─────────────────────────────────────────────────────────────────────────────
# Rollout and rollback, proved through the PROVIDER (P5-07)
# ─────────────────────────────────────────────────────────────────────────────
#
# The tests further up assert what `for_tenant` returns. These assert what the
# provider is asked to RUN, which is the difference the owner rejected Phase 5
# over: a rollout that decides correctly and reaches no provider is a rollout
# that does not exist.


async def _two_versions(admin, key):
    """Version 1 retired, version 2 serving with 1 as its rollback target."""
    await _register(admin, key, version=1, revision="aaa111")
    old = await admin.fetchval(
        "SELECT id FROM model_registry WHERE model_key = $1 AND version = 1", key)
    await admin.execute(
        "UPDATE model_registry SET status='retired' WHERE id = $1", old)
    await _register(admin, key, version=2, revision="bbb222")
    await _allowlist(admin, "org/model", "aaa111")
    await _allowlist(admin, "org/model", "bbb222")
    return old


async def _revision_the_provider_got(key, tenant):
    gw, Tier, calls = _gateway(required=True, model_key=key)
    out = await _ask(gw, Tier, tenant=tenant)
    if not calls:
        return None, out
    return calls[0].approved_model.revision, out


async def test_a_full_rollout_sends_every_tenant_the_new_revision(pool, admin):
    key = _key()
    await _two_versions(admin, key)
    await admin.execute(
        "UPDATE model_registry SET rollout_percentage = 100 "
        " WHERE model_key = $1 AND version = 2", key)

    for tenant in ("t1", "t2", "t3", "t4"):
        revision, out = await _revision_the_provider_got(key, tenant)
        assert revision == "bbb222", f"{tenant} ran {revision!r}: {out}"


async def test_a_partial_rollout_splits_what_the_provider_actually_runs(
        pool, admin):
    """**The rollout, as an observable fact about inference.**

    Not "for_tenant returned version 2" — "the provider was asked to load
    bbb222". Those were the same claim only in a report.
    """
    key = _key()
    old = await _two_versions(admin, key)
    await admin.execute(
        "UPDATE model_registry SET rollout_percentage = 50, rollback_target = $2 "
        " WHERE model_key = $1 AND version = 2", key, old)

    revisions = {}
    for i in range(60):
        tenant = f"tenant-{i}"
        revisions[tenant], _ = await _revision_the_provider_got(key, tenant)

    on_new = sum(1 for r in revisions.values() if r == "bbb222")
    on_old = sum(1 for r in revisions.values() if r == "aaa111")
    assert on_new + on_old == 60, f"some tenants got neither: {set(revisions.values())}"
    assert 15 < on_new < 45, (
        f"{on_new}/60 tenants ran the new revision at a 50% rollout")

    # Stable: the same tenant runs the same weights on the next request.
    for tenant in list(revisions)[:10]:
        again, _ = await _revision_the_provider_got(key, tenant)
        assert again == revisions[tenant], (
            f"{tenant} moved from {revisions[tenant]} to {again} between "
            f"requests")


async def test_a_zero_percent_rollout_sends_everyone_the_old_revision(pool, admin):
    """The 0 boundary. `serving_reaches_somebody` forbids a serving row at 0,
    so 0% is expressed as the new version serving nobody yet — which must mean
    the rollback target answers, not that nothing does."""
    key = _key()
    old = await _two_versions(admin, key)
    await admin.execute(
        "UPDATE model_registry SET rollout_percentage = 1, rollback_target = $2 "
        " WHERE model_key = $1 AND version = 2", key, old)

    from core.models import registry

    outside = [f"t{i}" for i in range(40)
               if registry.rollout_bucket(key, f"t{i}") >= 1]
    assert outside, "no tenant fell outside a 1% rollout"
    for tenant in outside[:5]:
        revision, out = await _revision_the_provider_got(key, tenant)
        assert revision == "aaa111", f"{tenant} ran {revision!r}: {out}"


async def test_a_rollback_changes_the_revision_subsequent_requests_run(
        pool, admin):
    """**MOD-03's rollback, as a serving operation rather than a column.**

    Version 2 serving → every request runs bbb222. The operator rolls back →
    every subsequent request runs aaa111. Nothing is cached, nothing needs a
    restart.
    """
    key = _key()
    old = await _two_versions(admin, key)
    await admin.execute(
        "UPDATE model_registry SET rollout_percentage = 100 "
        " WHERE model_key = $1 AND version = 2", key)

    for tenant in ("t1", "t2", "t3"):
        revision, _ = await _revision_the_provider_got(key, tenant)
        assert revision == "bbb222", tenant

    # The operator rolls back. ira_app cannot do this — it is an operator act.
    await admin.execute(
        "UPDATE model_registry SET status='rolled_back', rollback_target = $2 "
        " WHERE model_key = $1 AND version = 2", key, old)
    await admin.execute(
        "UPDATE model_registry SET status='serving', rollout_percentage = 100 "
        " WHERE id = $1", old)

    for tenant in ("t1", "t2", "t3"):
        revision, out = await _revision_the_provider_got(key, tenant)
        assert revision == "aaa111", (
            f"{tenant} still ran {revision!r} after the rollback: {out}")


async def test_the_active_version_survives_a_restart(pool, admin):
    """No in-process state decides which weights run. Proved by dropping the
    pool and building a new one — the two things a restart clears."""
    import utils.db as db_module

    key = _key()
    await _two_versions(admin, key)
    await admin.execute(
        "UPDATE model_registry SET rollout_percentage = 100 "
        " WHERE model_key = $1 AND version = 2", key)
    before, _ = await _revision_the_provider_got(key, "t1")

    old_pool = db_module._pool
    fresh = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=3)
    db_module._pool = fresh
    try:
        after, out = await _revision_the_provider_got(key, "t1")
    finally:
        db_module._pool = old_pool
        await fresh.close()

    assert before == after == "bbb222", f"{before!r} then {after!r}: {out}"


async def test_a_tenant_cannot_reach_another_environments_registry_entry(
        pool, admin):
    """The registry is platform-scoped, so there is no per-tenant entry to
    steal. What a tenant could otherwise influence is WHICH registry answers —
    and the environment comes from the server, with no parameter for it."""
    from core.models.gateway import RoutingRefusal

    key = _key()
    await _register(admin, key, environment="production")
    await _allowlist(admin, "org/model", "9f8e7d6c")
    gw, Tier, calls = _gateway(required=True, model_key=key)
    out = await _ask(gw, Tier)
    assert isinstance(out, RoutingRefusal), (
        "a model serving only in production answered a development request")
    assert calls == []
