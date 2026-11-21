"""Phase 5 — erasing audit content without destroying the audit.

This is the decision Phases 2 and 4 both deferred, and it is the hardest thing
in the phase, so the tests are about the exact trade being made.

The ledger records `resource` and `purpose` — a file path, an email address,
"email Sarah about the divorce". Pretending the audit log holds no personal
data would be false, so erasure has to reach it. But the chain exists to make
tampering evident, and under chain v1 the record body is *inside* the hash
pre-image: blanking it makes the chain read as tampered, and there is no way to
tell that apart from an actual attack.

Chain v2 hashes the body's DIGEST instead. After redaction the body is gone and
every link still verifies. What is lost is the ability to show what a record
said — not the ability to prove nothing was inserted, removed or reordered.

The four properties, in order of how badly they would matter if broken:

  1. v1 records verify EXACTLY as before (no history was rewritten).
  2. A redacted v2 chain still verifies, and still detects tampering.
  3. The content is actually gone.
  4. A redacted v1 record is REFUSED, not quietly accepted as verified.

(4) is the one that would be easiest to get wrong in the convenient direction.
"""
from __future__ import annotations

import pytest

from trust.ledger import (
    GENESIS,
    REDACTION_TOMBSTONE,
    body_digest,
    compute_hash,
    compute_hash_v2,
    is_redacted,
    verify_chain,
)

SECRET = "sarah@example.com"


def _chain(version: int, n: int = 4) -> list[dict]:
    """A chain of `n` records in the given version, carrying real content."""
    records: list[dict] = []
    prev = GENESIS
    for i in range(1, n + 1):
        body = {
            "seq": i,
            "user_id": "owner",
            "action_id": f"action-{i}",
            "task_id": None,
            "kind": "action",
            "record": {"resource": SECRET, "purpose": "email Sarah about the invoice"},
            "prev_hash": prev,
        }
        if version == 2:
            digest = body_digest(body)
            h = compute_hash_v2(prev, body)
            records.append({**body, "hash": h, "body_digest": digest})
        else:
            h = compute_hash(prev, body)
            records.append({**body, "hash": h})
        prev = h
    return records


def _redact(records: list[dict]) -> list[dict]:
    return [{**r, "record": dict(REDACTION_TOMBSTONE)} for r in records]


# ── 1. Nothing about v1 changed ──────────────────────────────────────────────


def test_a_v1_chain_verifies_exactly_as_before():
    """Existing records must not have been invalidated by adding v2.

    If this fails, the migration rewrote history — destroying the property the
    chain exists to provide.
    """
    assert verify_chain(_chain(1)) == 4


def test_a_v1_chain_still_detects_tampering():
    records = _chain(1)
    records[2]["record"] = {"resource": "somewhere else"}
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_chain(records)


def test_v1_and_v2_produce_different_hashes():
    """Sanity: the two constructions are genuinely different, so a test that
    passes under one is not accidentally passing under the other."""
    body = {"seq": 1, "user_id": "o", "action_id": "a", "task_id": None,
            "kind": "action", "record": {"x": 1}, "prev_hash": GENESIS}
    assert compute_hash(GENESIS, body) != compute_hash_v2(GENESIS, body)


# ── 2. v2 survives redaction, and still catches tampering ────────────────────


def test_a_v2_chain_verifies():
    assert verify_chain(_chain(2)) == 4


def test_a_redacted_v2_chain_still_verifies():
    """The whole point. Erasure and tamper-evidence are not in conflict here."""
    assert verify_chain(_redact(_chain(2))) == 4


def test_a_redacted_v2_chain_still_verifies_against_an_external_anchor():
    """The anchor is what makes tampering evident at all — an erasure that
    invalidated it would silently remove the guarantee."""
    original = _chain(2)
    head = original[-1]["hash"]
    assert verify_chain(_redact(original), expected_head=head) == 4


def test_tampering_is_detected_after_redaction():
    redacted = _redact(_chain(2))
    redacted[2]["hash"] = "f" * 64
    with pytest.raises(ValueError):
        verify_chain(redacted)


def test_swapping_a_redacted_records_digest_is_detected():
    """Once the body is gone the digest is the only thing pinning the record.
    Replacing it must break the chain, or redaction would be a hole."""
    redacted = _redact(_chain(2))
    redacted[1]["body_digest"] = "a" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_chain(redacted)


def test_removing_a_redacted_record_is_detected():
    redacted = _redact(_chain(2))
    del redacted[2]
    with pytest.raises(ValueError, match="broken prev_hash link"):
        verify_chain(redacted)


def test_reordering_redacted_records_is_detected():
    redacted = _redact(_chain(2))
    redacted[1], redacted[2] = redacted[2], redacted[1]
    with pytest.raises(ValueError):
        verify_chain(redacted)


def test_editing_a_v2_record_while_keeping_its_digest_is_detected():
    """The digest must be checked against the body while the body is present —
    otherwise an attacker could edit content and keep the old digest."""
    records = _chain(2)
    records[1]["record"] = {"resource": "attacker-controlled"}
    with pytest.raises(ValueError, match="body digest mismatch"):
        verify_chain(records)


# ── 3. The content is really gone ────────────────────────────────────────────


def test_redaction_removes_the_content():
    redacted = _redact(_chain(2))
    blob = str(redacted)
    assert SECRET not in blob
    assert "invoice" not in blob
    assert all(is_redacted(r["record"]) for r in redacted)


def test_redaction_keeps_the_facts_that_are_not_content():
    """What survives is deliberately chosen: it must remain provable that an
    action of a given kind happened at a given point."""
    original = _chain(2)
    redacted = _redact(original)
    for before, after in zip(original, redacted):
        assert after["seq"] == before["seq"]
        assert after["kind"] == before["kind"]
        assert after["action_id"] == before["action_id"]
        assert after["hash"] == before["hash"]
        assert after["body_digest"] == before["body_digest"]


def test_a_tombstone_is_distinguishable_from_an_empty_record():
    """"Erased on request" and "there was nothing here" are different facts."""
    assert is_redacted(dict(REDACTION_TOMBSTONE))
    assert not is_redacted({})
    assert not is_redacted(None)
    assert not is_redacted({"resource": "x"})


# ── 4. A redacted v1 record is refused, never quietly accepted ───────────────


def test_a_redacted_v1_record_is_refused():
    """The convenient failure would be to accept it and report "verified".

    A v1 record's content is inside its hash, so once erased its integrity
    genuinely cannot be established. Saying so is the honest answer; a green
    tick would be a false claim about evidence.
    """
    records = _chain(1)
    records[1]["record"] = dict(REDACTION_TOMBSTONE)
    with pytest.raises(ValueError, match="no body_digest"):
        verify_chain(records)


def test_the_refusal_explains_why_rather_than_saying_hash_mismatch():
    """An operator seeing this needs to know it is a design limit, not an
    attack — otherwise a routine erasure looks like a breach."""
    records = _chain(1)
    records[0]["record"] = dict(REDACTION_TOMBSTONE)
    with pytest.raises(ValueError) as exc:
        verify_chain(records)
    message = str(exc.value)
    assert "v1" in message and "cannot be verified once erased" in message


# ── Mixed chains: the realistic state of a migrated deployment ───────────────


def test_a_chain_that_starts_v1_and_continues_v2_verifies():
    """What a real deployment looks like after the migration: old records keep
    their construction, new ones use the redactable one."""
    records = _chain(1, n=2)
    prev = records[-1]["hash"]
    for i in (3, 4):
        body = {"seq": i, "user_id": "owner", "action_id": f"action-{i}",
                "task_id": None, "kind": "action",
                "record": {"resource": SECRET, "purpose": "later action"},
                "prev_hash": prev}
        digest = body_digest(body)
        h = compute_hash_v2(prev, body)
        records.append({**body, "hash": h, "body_digest": digest})
        prev = h

    assert verify_chain(records) == 4

    # The v2 half can be erased; the v1 half cannot, and the chain still holds.
    for record in records:
        if record.get("body_digest"):
            record["record"] = dict(REDACTION_TOMBSTONE)
    assert verify_chain(records) == 4
    assert sum(1 for r in records if is_redacted(r["record"])) == 2
    # And the residue is visible rather than hidden: two records still hold
    # content that cannot be erased without breaking the proof.
    assert sum(1 for r in records if not r.get("body_digest")) == 2
