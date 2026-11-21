"""IRA Core — the intelligence and governance both editions share.

ADR-0001 (docs/cloud-transformation/adr/ADR-0001-TWO-EDITIONS.md) decided two
editions over one governed core. This package is that core.

Boundary rule, enforced by tests/architecture/test_edition_boundaries.py:

    core/ and core/interfaces/ MUST NOT import from editions/.

Core defines the seams; editions supply the implementations. An import in the
other direction would let a deployment assumption (a local filesystem, a single
owner, an Ollama install) leak back into code the cloud edition also runs.
"""
from core.editions import (
    EDITION_CLOUD,
    EDITION_PRIVATE,
    EditionName,
    current_edition,
    edition_default,
    is_cloud,
    is_private,
)

__all__ = [
    "EditionName",
    "EDITION_PRIVATE",
    "EDITION_CLOUD",
    "current_edition",
    "edition_default",
    "is_private",
    "is_cloud",
]
