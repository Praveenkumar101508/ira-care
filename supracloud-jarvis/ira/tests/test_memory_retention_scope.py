"""Every reserved memory namespace has a retention decision, made on purpose.

Age-based retention excludes namespaces (`RETENTION_EXEMPT_NAMESPACES`) rather
than listing personal kinds, because personal `source_type` is free-form and an
allowlist would let every new personal kind escape retention silently. The cost
of that choice is the opposite failure: a new corpus added to the registry is
aged out like personal memory unless somebody decides otherwise — which is how
the Technical Library came to be deleted by the weekly purge.

`tests/test_delegated_access.py` already forces every namespace into
`memory/namespaces.py`. These tests make adding one there fail until it is
classified, and pin the exempt set to the owner's decision: widening or
narrowing it means editing this file, where a reviewer will see it.
"""
from __future__ import annotations

from memory.namespaces import (
    MEETING_NAMESPACE,
    RESERVED_NAMESPACES,
    RETENTION_EXEMPT_NAMESPACES,
    SELF_KNOWLEDGE_NAMESPACE,
    SYSTEM_NAMESPACE,
    TECH_LIBRARY_NAMESPACE,
    VAULT_NAMESPACE,
)

#: Reference corpora rebuilt by their own ingest: no age-based pass deletes them.
EXEMPT = {TECH_LIBRARY_NAMESPACE, SELF_KNOWLEDGE_NAMESPACE}
#: Rows that age out: personal memory, and meeting transcripts — other people's
#: speech, with no per-meeting delete, so retention is what removes them.
RETAINED = {VAULT_NAMESPACE, SYSTEM_NAMESPACE, MEETING_NAMESPACE}


def test_every_reserved_namespace_has_a_retention_decision():
    assert not EXEMPT & RETAINED
    undecided = RESERVED_NAMESPACES - EXEMPT - RETAINED
    assert not undecided, (
        f"memory namespace(s) {sorted(undecided)} have no retention decision, so "
        f"age-based retention deletes them like personal memory. Decide, then add "
        f"each to RETENTION_EXEMPT_NAMESPACES (and EXEMPT here) or to RETAINED.")
    assert EXEMPT | RETAINED == RESERVED_NAMESPACES, (
        "a classified namespace is no longer in the registry — remove it here")


def test_retention_exempts_exactly_the_reference_corpora():
    assert RETENTION_EXEMPT_NAMESPACES == EXEMPT
