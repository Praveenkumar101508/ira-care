"""IRA Cloud Companion — the multi-tenant SaaS edition.

**Not implemented.** Phase 2 builds cloud identity and tenant isolation; nothing
here serves traffic yet.

This package exists in Phase 1 for one reason: to make the boundary real and
testable before there is code to constrain. The architecture test asserts that
nothing under `editions/cloud/` imports `editions/private/` or any Private-only
module (Armory, Forge, NeuroKernel, architect, host commands). Establishing that
now means the first cloud module is written inside a boundary that already
exists, rather than having one retrofitted around it later.

Cloud composition rules, from ADR-0001 and the edition defaults table:

  * Identity comes from the cloud authentication system — never from
    `Settings.ira_admin_username`. `resolve_principal` raises for cloud rather
    than falling back to the private single-owner mapping.
  * External egress (browser agent) is off until it routes through the
    Permission Broker and produces an Action Receipt.
  * Privacy mode is `local_only` until the Phase 8 model gateway can disclose
    which provider handled a request.
  * Owner-grade tooling is absent from the import graph, not merely flagged off.
"""
from __future__ import annotations

from core.editions import EDITION_CLOUD, EditionName

EDITION: EditionName = EDITION_CLOUD

#: Set true by the phase that first serves cloud traffic. Kept explicit so no
#: one can conclude from "the package exists" that the edition is usable.
IMPLEMENTED: bool = False


__all__ = ["EDITION", "IMPLEMENTED"]
