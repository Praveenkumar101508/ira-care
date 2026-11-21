"""Edition-specific composition.

    editions/private/  — IRA Private. Local-first, single owner, local models,
                         direct filesystem executors, owner-grade tooling.
    editions/cloud/    — IRA Cloud Companion. Multi-tenant SaaS (Phase 2 onward).

Phase 1 does NOT move existing modules into these packages. Moving hundreds of
files before the seams are proven would produce an unreviewable diff and risk
breaking a working product; ADR-0001 defers physical movement deliberately.
What lives here now is the composition layer: which implementations each edition
selects, and the boundary that keeps them apart.

The boundary that matters, enforced by tests/architecture/test_edition_boundaries.py:

    editions/cloud/ MUST NOT import editions/private/ (or the private-only
    executors it composes).

Cloud must not be able to reach a filesystem executor, the Armory, or Forge —
not because a flag is off, but because the code is not in its import graph.
"""

__all__: list[str] = []
