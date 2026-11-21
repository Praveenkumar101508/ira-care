"""Phase 5 — never claim more than was done.

Two of the brief's rules are really the same instruction, and it is the one most
easily broken by a well-meaning `try/except`:

    Do not claim data deletion before all relevant stores are handled.
    Do not claim immediate backup deletion unless implemented.

So the tests here are mostly about the *unhappy* paths: a store that could not
be reached, an audit record that could not be erased, a backup that was never
touched. In each case the question is not "did it fail gracefully" but "does the
user get told the truth".
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

# `test_every_capability_the_disclosure_promises_has_a_route` builds the real
# application, and `Settings` refuses to construct without these. Placeholders,
# set the way every other suite in this repository does — this file asserts
# about wording and routing, never about a credential.
for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD",
           "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

from core.privacy.disclosure import (
    LOCAL_ONLY_CLAIMS,
    UNIMPLEMENTED_CLAIMS,
    claimed_capabilities,
    contains_local_only_claim,
    contains_unimplemented_claim,
    deletion_disclosure,
    memory_mode_disclosure,
    storage_disclosure,
    voice_retention_disclosure,
)
from core.privacy.erasure import (
    KNOWN_STORES,
    StoreResult,
    missing_store,
    run_erasure,
)


def ok(store: str, **kw) -> StoreResult:
    return StoreResult(store=store, ok=True, **kw)


def bad(store: str, **kw) -> StoreResult:
    return StoreResult(store=store, ok=False, error="unreachable", **kw)


def erasers(**by_name):
    async def _make(result):
        return result

    return {name: (lambda r=r: _make(r)) for name, r in by_name.items()}


# ── Status is only 'completed' when everything succeeded ─────────────────────


async def test_all_stores_succeeding_is_completed():
    outcome = await run_erasure(
        scope="everything",
        erasers=erasers(**{s: ok(s, deleted=1) for s in KNOWN_STORES}),
    )
    assert outcome.status == "completed"
    assert outcome.complete is True
    assert outcome.stores_reached == outcome.stores_total


async def test_one_unreachable_store_makes_the_whole_thing_partial():
    """The failure this suite exists for. A deletion that cleared four stores
    out of five is not a deletion."""
    results = {s: ok(s) for s in KNOWN_STORES}
    results["redis"] = bad("redis")
    outcome = await run_erasure(scope="everything", erasers=erasers(**results))

    assert outcome.status == "partial"
    assert outcome.complete is False
    assert "incomplete" in outcome.public_dict()["summary"]


async def test_every_store_failing_is_failed_not_partial():
    outcome = await run_erasure(
        scope="everything", erasers=erasers(**{s: bad(s) for s in KNOWN_STORES})
    )
    assert outcome.status == "failed"


async def test_a_store_with_no_eraser_is_reported_missing_not_skipped():
    """An absent store in a report reads as "nothing to do there", which is
    exactly the wrong inference."""
    outcome = await run_erasure(
        scope="everything",
        erasers=erasers(**{s: ok(s) for s in KNOWN_STORES if s != "object_storage"}),
    )
    assert outcome.status == "partial"
    entry = next(r for r in outcome.results if r.store == "object_storage")
    assert entry.ok is False
    assert "no result" in entry.error


async def test_an_eraser_that_raises_does_not_abort_the_others():
    """Letting the first failure propagate would leave the user unable to tell
    which stores were cleared."""
    async def explode():
        raise RuntimeError("connection reset")

    supplied = {s: (lambda r=ok(s): _wrap(r)) for s in KNOWN_STORES if s != "redis"}
    supplied["redis"] = explode

    outcome = await run_erasure(scope="everything", erasers=supplied)

    assert outcome.status == "partial"
    assert outcome.stores_reached == len(KNOWN_STORES) - 1
    redis = next(r for r in outcome.results if r.store == "redis")
    assert "RuntimeError" in redis.error


async def _wrap(result):
    return result


async def test_every_known_store_appears_in_the_report():
    """A store that is silently absent is a store nobody checked."""
    outcome = await run_erasure(
        scope="everything", erasers=erasers(**{s: ok(s) for s in KNOWN_STORES})
    )
    assert {r.store for r in outcome.results} == set(KNOWN_STORES)
    assert len(KNOWN_STORES) >= 5, "the store list has shrunk — is one unchecked?"


# ── Backups are never claimed as deleted ─────────────────────────────────────


async def test_the_summary_never_claims_backups_were_deleted():
    outcome = await run_erasure(
        scope="everything", erasers=erasers(**{s: ok(s) for s in KNOWN_STORES})
    )
    summary = outcome.public_dict()["summary"]
    assert contains_unimplemented_claim(summary) == []
    assert "Backups are not edited" in summary


def test_the_deletion_summary_mentions_backup_residue_even_on_success():
    summary = deletion_disclosure(
        stores_reached=5, stores_total=5, unredactable_records=0
    )
    assert "backup" in summary.lower()
    assert "age out" in summary


def test_the_summary_can_omit_backup_wording_only_when_told_to():
    summary = deletion_disclosure(
        stores_reached=5, stores_total=5, unredactable_records=0,
        backup_residue=False,
    )
    assert "backup" not in summary.lower()


async def test_a_backup_residue_date_is_always_reported():
    outcome = await run_erasure(
        scope="everything", erasers=erasers(**{s: ok(s) for s in KNOWN_STORES})
    )
    assert outcome.backup_residue_until > outcome.finished_at


# ── Unerasable audit records are surfaced, not hidden ────────────────────────


async def test_unerasable_audit_records_are_counted_and_explained():
    results = {s: ok(s) for s in KNOWN_STORES}
    results["audit"] = ok("audit", redacted=10, unerasable=3)
    outcome = await run_erasure(scope="account", erasers=erasers(**results))

    assert outcome.unerasable_records == 3
    summary = outcome.public_dict()["summary"]
    assert "3 older audit record" in summary
    assert "unverifiable" in summary


def test_an_unknown_residue_is_not_reported_as_zero():
    """"We could not determine it" and "there is none" are different facts, and
    only one of them is reassuring."""
    summary = deletion_disclosure(
        stores_reached=5, stores_total=5, unredactable_records=-1
    )
    assert "could not be determined" in summary
    assert "unknown rather than zero" in summary


def test_no_residue_produces_no_scary_sentence():
    summary = deletion_disclosure(
        stores_reached=5, stores_total=5, unredactable_records=0
    )
    assert "audit record" not in summary


# ── Truthful wording per edition ─────────────────────────────────────────────


def test_the_vocabulary_lists_are_not_empty():
    """Without this, every scan below would pass vacuously."""
    assert len(LOCAL_ONLY_CLAIMS) >= 5
    assert len(UNIMPLEMENTED_CLAIMS) >= 5


def test_the_private_edition_may_say_data_stays_on_the_machine():
    disclosure = storage_disclosure("private")
    assert disclosure.local_processing is True
    assert "this machine" in disclosure.detail


def test_the_cloud_edition_never_makes_a_local_only_claim():
    """The rule that matters. IRA Private's reassuring wording becomes a lie in
    a hosted product, told on the screen where the user decides how much to
    trust it."""
    disclosure = storage_disclosure("cloud")
    assert disclosure.local_processing is False
    for text in (disclosure.headline, disclosure.detail):
        assert contains_local_only_claim(text) == [], text


def test_no_disclosure_in_any_edition_makes_an_unimplemented_claim():
    for edition in ("private", "cloud"):
        disclosure = storage_disclosure(edition)
        for text in (disclosure.headline, disclosure.detail):
            assert contains_unimplemented_claim(text) == [], text


def test_the_claim_detector_actually_detects():
    """A scanner that never fires would make the tests above meaningless."""
    assert contains_local_only_claim("Your data never leaves your device.")
    assert contains_unimplemented_claim("Everything is end-to-end encrypted.")
    assert contains_local_only_claim("Stored in your workspace.") == []


@pytest.mark.parametrize("mode", ["off", "ask", "automatic"])
def test_every_memory_mode_has_honest_wording(mode):
    text = memory_mode_disclosure(mode)
    assert text and contains_unimplemented_claim(text) == []


def test_voice_retention_wording_matches_the_setting():
    assert "discarded as soon as" in voice_retention_disclosure(0)
    assert "24 hour" in voice_retention_disclosure(24)


def test_zero_voice_retention_is_the_wording_for_the_default():
    """RetentionSettings defaults to 0 hours, and the sentence a user reads must
    match what the system actually does."""
    from core.privacy.memory import RetentionSettings

    assert RetentionSettings().voice_retention_hours == 0
    assert "discarded" in voice_retention_disclosure(
        RetentionSettings().voice_retention_hours
    )


# ── The object-storage eraser is honest about not existing ───────────────────


async def test_object_storage_reports_unreached_rather_than_done():
    """A green tick for a store with no backend is exactly the failure this
    module is written to avoid — and it would silently become wrong the day a
    backend is added."""
    from core.privacy.erasure import erase_object_storage

    result = await erase_object_storage(object(), scope="everything")
    assert result.ok is False
    assert "no object-storage backend" in result.error


# ── P12-05 finding K — a promise must point at something ─────────────────────


def test_every_capability_the_disclosure_promises_has_a_route():
    """The guard the deny-list could not be.

    `UNIMPLEMENTED_CLAIMS` forbids nine substrings. The cloud disclosure said
    "You can export or delete your data at any time" while no export existed
    anywhere in the codebase — and passed, because that sentence contains none
    of the nine. A deny-list can only forbid wordings somebody already thought
    of; a promise made in new words is invisible to it.

    So this asks the other question, against the REAL application rather than
    against a list: for every capability the text claims, is there a route that
    provides it?
    """
    from core.privacy.disclosure import CAPABILITY_CLAIMS

    from app.factory import create_app

    # The REAL application, built the way every other route test in this
    # repository builds it. Asserting against a hand-written list of paths would
    # make this test agree with a copy of the router rather than with the app —
    # and the whole point is that the disclosure and the routing table are
    # checked against each other.
    # P16-R1b: FastAPI 0.141 stopped flattening included routers into
    # `app.routes`, so this returned 79 of 300 paths and reported that
    # promised capabilities had no route. `route_paths` descends the
    # `_IncludedRouter` wrappers and applies their prefixes.
    from tests.route_introspection import route_paths

    paths = set(route_paths(create_app()))

    checked = 0
    for edition in ("cloud", "private"):
        disclosure = storage_disclosure(edition)
        for capability in claimed_capabilities(disclosure.detail):
            route = CAPABILITY_CLAIMS[capability]
            assert route in paths, (
                f"the {edition} disclosure tells the user they can "
                f"{capability!r} their data, and {route} is not a registered "
                f"route. Either build it or stop saying so — this is the exact "
                f"defect P12-05 found, where the text promised an export that "
                f"did not exist in any form."
            )
            checked += 1

    assert checked >= 2, (
        f"only {checked} capability claim(s) were checked, so this test is "
        f"close to vacuous. If the disclosure text genuinely stopped promising "
        f"anything, delete this test deliberately rather than letting it pass "
        f"on nothing."
    )
