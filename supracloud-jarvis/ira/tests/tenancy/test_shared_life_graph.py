"""A shared life-graph entity keeps colleagues' links and its author's words apart.

The life graph keeps one entity per name per workspace, so when Alice and Bob
both name "Mum" they share one row, and it is Alice's if she named it first
(ADR-0010). Measured on 20 October
(`docs/testing/evidence/everything-personal-c-2026-10-20/`), two effects
followed:

1. Alice's "delete everything" deleted Bob's edges to her entity, through the
   `edges` -> `entities` ON DELETE CASCADE.
2. Bob naming her entity overwrote her description with his.

ADR-0011 option C, which changes code only:

- **Erasure** keeps an entity of hers that someone else's edge, or an edge with
  no author, still points to. Its description, embedding and author are
  cleared, so it names no one and is counted as kept.
- **Writers** change an entity's description and embedding, or an edge's
  weight, only when the writer is its author.

Real PostgreSQL, real RLS, as `ira_app`, through the real writers. Only the
embedding model is replaced, by a fixed vector.
"""
from __future__ import annotations

import dataclasses
import os
import sys
import uuid

import pytest

for _k, _v in {
    "IRA_SECRET_KEY": "ci-test-secret-key-for-jwt",
    "IRA_ADMIN_PASSWORD": "ci-test-password",
    "POSTGRES_PASSWORD": "ci-test",
    "REDIS_PASSWORD": "ci-test",
    "VLLM_API_KEY": "ci-test",
}.items():
    os.environ.setdefault(_k, _v)

# `core.privacy.retention` reaches `memory.store`, which imports
# `SentenceTransformer` by name; conftest stubs the package without it.
_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    setattr(_st, "SentenceTransformer", object)
    setattr(_st, "CrossEncoder", object)

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.getenv("IRA_TEST_DATABASE_DSN", "")
ADMIN_DSN = os.getenv("IRA_TEST_ADMIN_DSN", "")
if not DSN or not ADMIN_DSN:
    pytest.skip("IRA_TEST_DATABASE_DSN / IRA_TEST_ADMIN_DSN not set",
                allow_module_level=True)

TENANT = uuid.uuid4()
WORKSPACE = uuid.uuid4()
SLUG = f"slg-{TENANT.hex[:8]}"
PEOPLE = {"alice": uuid.uuid4(), "bob": uuid.uuid4()}


@pytest.fixture
async def pool(monkeypatch):
    import memory.life_graph as life_graph
    import utils.db as db_module

    async def _embed(text: str) -> list[float]:
        return [0.5] * 1024

    monkeypatch.setattr(life_graph, "embed_one", _embed)
    monkeypatch.setenv("IRA_LIFE_GRAPH", "true")
    created = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=4)
    previous, db_module._pool = db_module._pool, created
    try:
        yield created
    finally:
        db_module._pool = previous
        await created.close()


@pytest.fixture
async def admin(pool):
    """Alice and Bob, members of one workspace. Yields the admin connection."""
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await _wipe(conn)
        await conn.execute(
            "INSERT INTO tenants (id,name,slug,edition) VALUES ($1,$2,$3,'cloud')",
            TENANT, f"Shared life graph {SLUG}", SLUG)
        org = await conn.fetchval(
            "INSERT INTO organisations (tenant_id,name,slug) VALUES ($1,'O',$2) RETURNING id",
            TENANT, SLUG)
        await conn.execute(
            "INSERT INTO workspaces (id,tenant_id,organisation_id,name,slug,is_default) "
            "VALUES ($1,$2,$3,'W',$4,TRUE)", WORKSPACE, TENANT, org, SLUG)
        for who, uid in PEOPLE.items():
            await conn.execute(
                "INSERT INTO users (id,email,display_name,auth_provider,external_subject,status) "
                "VALUES ($1,NULL,'R','password',$2,'active')", uid, f"{who}-{SLUG}")
            await conn.execute(
                "INSERT INTO tenant_memberships (tenant_id,user_id,role,status) "
                "VALUES ($1,$2,'member','active')", TENANT, uid)
            await conn.execute(
                "INSERT INTO workspace_memberships (tenant_id,workspace_id,user_id,role) "
                "VALUES ($1,$2,$3,'member')", TENANT, WORKSPACE, uid)
        yield conn
    finally:
        await _wipe(conn)
        await conn.close()


async def _wipe(conn):
    # `edges` and `entities` hold their tenant ON DELETE RESTRICT; children first.
    for table in ("edges", "entities"):
        await conn.execute(f'DELETE FROM "{table}" WHERE tenant_id = $1', TENANT)  # noqa: S608
    await conn.execute("DELETE FROM tenants WHERE id = $1", TENANT)
    for uid in PEOPLE.values():
        await conn.execute("DELETE FROM users WHERE id = $1", uid)


def _person(who: str):
    from core.identity.principal import Principal

    return Principal(user_id=PEOPLE[who], tenant_id=TENANT, workspace_id=WORKSPACE,
                     session_id=uuid.uuid4(), edition="cloud",
                     username=f"{who}-{SLUG}", is_owner=False)


def _job():
    from core.identity.background import background_principal

    job = background_principal(TENANT, job="shared-life-graph", edition="cloud")
    return dataclasses.replace(job, workspace_id=WORKSPACE)


async def _entity(admin, entity_id: str):
    return await admin.fetchrow(
        "SELECT created_by, description, embedding IS NOT NULL AS embedded "
        "FROM entities WHERE id = $1", uuid.UUID(entity_id))


async def _edge(admin, edge_id: str):
    return await admin.fetchrow(
        "SELECT created_by, weight FROM edges WHERE id = $1", uuid.UUID(edge_id))


@pytest.fixture
async def graph(admin):
    """Alice's, Bob's and a background job's entities and edges, written by the
    real writers. Yields the admin connection and {name: id}."""
    from memory.life_graph import add_edge, upsert_entity

    alice, bob, job = _person("alice"), _person("bob"), _job()
    ids: dict[str, str | None] = {}
    # Alice's: "Mum", linked by Bob; "Gym", linked by a job; "Bank", linked
    # only by her; "Diary", linked by nobody.
    ids["mum"] = await upsert_entity(alice, "person", f"Mum {SLUG}", "Alice's words about Mum")
    ids["gym"] = await upsert_entity(alice, "place", f"Gym {SLUG}", "Alice's gym")
    ids["bank"] = await upsert_entity(alice, "org", f"Bank {SLUG}", "Alice's bank")
    ids["diary"] = await upsert_entity(alice, "topic", f"Diary {SLUG}", "Alice's diary")
    ids["alice_edge"] = await add_edge(alice, ids["mum"], ids["bank"], "banks_with")
    # Bob's: "Dad", and his edge to Alice's "Mum".
    ids["dad"] = await upsert_entity(bob, "person", f"Dad {SLUG}", "Bob's words about Dad")
    ids["bob_edge"] = await add_edge(bob, ids["dad"], ids["mum"], "married_to")
    # A background job's: an entity and an edge with no author, to Alice's "Gym".
    ids["fitness"] = await upsert_entity(job, "topic", f"Fitness {SLUG}", "a job's topic")
    ids["job_edge"] = await add_edge(job, ids["fitness"], ids["gym"], "about")
    assert all(ids.values()), ids
    yield admin, ids


# ── Erasure ──────────────────────────────────────────────────────────────────


async def test_erasing_keeps_a_colleagues_edge_to_the_persons_entity(graph):
    from core.privacy.erasure import erase_postgres

    admin, ids = graph
    result = await erase_postgres(_person("alice"), scope="everything")

    assert result.ok, result.error
    assert await _edge(admin, ids["bob_edge"]) is not None, (
        "Alice's erasure deleted Bob's edge to an entity she created")
    assert (await _entity(admin, ids["dad"]))["description"] == "Bob's words about Dad"


async def test_the_kept_entity_holds_none_of_the_persons_words(graph):
    from core.privacy.erasure import erase_postgres

    admin, ids = graph
    await erase_postgres(_person("alice"), scope="everything")

    mum = await _entity(admin, ids["mum"])
    assert mum is not None
    assert (mum["created_by"], mum["description"], mum["embedded"]) == (None, None, False), (
        "the entity Alice's erasure kept for Bob's edge still holds her words or her name")


async def test_erasing_keeps_an_edge_with_no_author(graph):
    """ADR-0010 Q4 keeps a row nobody can be attributed to. Deleting the entity
    it points to would take it through the cascade."""
    from core.privacy.erasure import erase_postgres

    admin, ids = graph
    await erase_postgres(_person("alice"), scope="everything")

    assert await _edge(admin, ids["job_edge"]) is not None, (
        "Alice's erasure deleted an edge with no author, through the cascade")
    gym = await _entity(admin, ids["gym"])
    assert gym is not None and gym["created_by"] is None and gym["description"] is None


async def test_erasing_deletes_what_only_the_person_links_to(graph):
    from core.privacy.erasure import erase_postgres

    admin, ids = graph
    await erase_postgres(_person("alice"), scope="everything")

    assert await _edge(admin, ids["alice_edge"]) is None
    assert await _entity(admin, ids["bank"]) is None, (
        "an entity only Alice's own edge pointed to survived her erasure")
    assert await _entity(admin, ids["diary"]) is None


async def test_the_note_says_which_kept_rows_were_the_persons(graph):
    from core.privacy.erasure import erase_postgres

    _, _ = graph
    result = await erase_postgres(_person("alice"), scope="everything")

    # Kept: Mum and Gym (released) and the job's Fitness; the job's edge.
    assert "Kept 4 row(s) that record no author or owner" in result.note, result.note
    assert "edges 1, entities 3" in result.note, result.note
    assert ("You created 2 of them. They are kept because other people's rows "
            "point to them, with your description and authorship removed") in result.note, (
        result.note)
    assert result.note.endswith("(ADR-0011): entities 2."), result.note


def test_the_note_reads_right_for_one_released_row():
    from core.privacy.erasure import kept_note

    note = kept_note({"entities": 1}, {"entities": 1})
    assert note.endswith("You created 1 of them. It is kept because other people's rows "
                         "point to it, with your description and authorship removed "
                         "(ADR-0011): entities 1."), note
    assert kept_note({"edges": 1}) == kept_note({"edges": 1}, {}), "nothing released, no clause"


async def test_the_persons_own_edges_do_not_hold_an_entity_back(graph):
    """Erasure deletes the person's edges before their entities, so their own
    edges are gone by the time the release runs. The release must not depend on
    that order: run first, it still releases only what other people's edges, or
    edges with no author, point to."""
    from core.identity.db_context import workspace_connection
    from core.privacy.erasure import release_shared

    admin, ids = graph
    alice = _person("alice")
    async with workspace_connection(alice) as conn:
        released = await release_shared(conn, "entities", alice)

    assert released == 2, "Mum (Bob's edge) and Gym (the job's edge), and nothing else"
    bank = await _entity(admin, ids["bank"])
    assert (bank["created_by"], bank["description"]) == (PEOPLE["alice"], "Alice's bank"), (
        "Alice's own edge held her entity back")


async def test_a_colleagues_erasure_leaves_the_persons_entity_alone(graph):
    """Bob's edge points to Alice's entity; his erasure deletes his edge only."""
    from core.privacy.erasure import erase_postgres

    admin, ids = graph
    result = await erase_postgres(_person("bob"), scope="everything")

    assert result.ok, result.error
    assert await _edge(admin, ids["bob_edge"]) is None
    assert await _entity(admin, ids["dad"]) is None
    mum = await _entity(admin, ids["mum"])
    assert (mum["created_by"], mum["description"], mum["embedded"]) == (
        PEOPLE["alice"], "Alice's words about Mum", True)


# ── Writers ──────────────────────────────────────────────────────────────────


async def test_a_colleague_naming_the_entity_leaves_its_description(graph):
    from memory.life_graph import upsert_entity

    admin, ids = graph
    again = await upsert_entity(_person("bob"), "person", f"Mum {SLUG}", "Bob's words about Mum")

    assert again == ids["mum"], "Bob naming Mum must still give him its id to link to"
    mum = await _entity(admin, ids["mum"])
    assert (mum["created_by"], mum["description"]) == (
        PEOPLE["alice"], "Alice's words about Mum"), "Bob's words landed in Alice's row"


async def test_a_job_naming_the_entity_leaves_its_description(graph):
    from memory.life_graph import upsert_entity

    admin, ids = graph
    again = await upsert_entity(_job(), "person", f"Mum {SLUG}", "a job's words")

    assert again == ids["mum"]
    assert (await _entity(admin, ids["mum"]))["description"] == "Alice's words about Mum"


async def test_the_author_still_updates_the_description(graph):
    from memory.life_graph import upsert_entity

    admin, ids = graph
    again = await upsert_entity(_person("alice"), "person", f"Mum {SLUG}", "Alice, later")

    assert again == ids["mum"]
    assert (await _entity(admin, ids["mum"]))["description"] == "Alice, later"


async def test_a_colleague_reasserting_the_edge_leaves_its_weight(graph):
    from memory.life_graph import add_edge

    admin, ids = graph
    again = await add_edge(_person("bob"), ids["mum"], ids["bank"], "banks_with", weight=0.1)

    assert again == ids["alice_edge"]
    assert (await _edge(admin, ids["alice_edge"]))["weight"] == pytest.approx(1.0), (
        "Bob's assertion changed the weight of Alice's edge")


async def test_the_author_still_updates_the_weight(graph):
    from memory.life_graph import add_edge

    admin, ids = graph
    again = await add_edge(_person("alice"), ids["mum"], ids["bank"], "banks_with", weight=0.25)

    assert again == ids["alice_edge"]
    assert (await _edge(admin, ids["alice_edge"]))["weight"] == pytest.approx(0.25)
