"""Privacy — transparent memory, honest disclosure, and erasure that reports
what it actually did.

Phase 5 makes privacy visible, understandable and controllable, and it settles
the decision Phases 2 and 4 both deferred: how a user can have their data erased
when the audit log is append-only and holds real content (ADR-0004).

  * :mod:`memory`     — every stored memory, explicable and controllable. A
                        memory nobody can explain is one the user cannot judge.
  * :mod:`disclosure` — what IRA may truthfully say about where data is. Derived
                        from the edition, not written into a component, so the
                        cloud interface cannot inherit IRA Private's
                        local-only claims.
  * :mod:`erasure`    — deletion across every store, reporting per store. A
                        partial deletion is a real outcome the user is told
                        about, not an edge case to smooth over.
  * :mod:`store`      — tenant-scoped persistence for all of the above.

The three rules the package exists to hold:

  1. **Never claim more than was done.** No "deleted everywhere", no "removed
     from backups", no green tick when a store could not be reached.
  2. **Off is the default.** Memory asks before storing; voice audio is
     discarded after transcription; a memory is shared with no connector and no
     external provider until the user says otherwise.
  3. **Erasure and evidence are both preserved.** Audit content is redacted
     while the proof that an action happened survives — and where that is
     impossible, the residue is counted and shown rather than hidden.
"""
from __future__ import annotations

from core.privacy.disclosure import (
    LOCAL_ONLY_CLAIMS,
    UNIMPLEMENTED_CLAIMS,
    StorageDisclosure,
    contains_local_only_claim,
    contains_unimplemented_claim,
    deletion_disclosure,
    memory_mode_disclosure,
    storage_disclosure,
    voice_retention_disclosure,
)
from core.privacy.erasure import (
    KNOWN_STORES,
    ErasureOutcome,
    StoreResult,
    default_erasers,
    run_erasure,
)
from core.privacy.memory import (
    NEVER_EXTERNAL,
    SENSITIVITIES,
    MemoryRecord,
    MemoryRejected,
    RetentionSettings,
    approve,
    live_only,
    prepare,
    reject,
    set_expiry,
    soft_delete,
    touch,
)

__all__ = [
    "KNOWN_STORES",
    "LOCAL_ONLY_CLAIMS",
    "NEVER_EXTERNAL",
    "SENSITIVITIES",
    "UNIMPLEMENTED_CLAIMS",
    "ErasureOutcome",
    "MemoryRecord",
    "MemoryRejected",
    "RetentionSettings",
    "StorageDisclosure",
    "StoreResult",
    "approve",
    "contains_local_only_claim",
    "contains_unimplemented_claim",
    "default_erasers",
    "deletion_disclosure",
    "live_only",
    "memory_mode_disclosure",
    "prepare",
    "reject",
    "run_erasure",
    "set_expiry",
    "soft_delete",
    "storage_disclosure",
    "touch",
    "voice_retention_disclosure",
]
