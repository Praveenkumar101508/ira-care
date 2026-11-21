"""Governance — the User Constitution and the Permission Broker.

IRA's primary differentiator, and the phase that makes the promise concrete:

    IRA knows only what the user allows, acts only within the user's rules and
    proves every important action.

The flow, and which module owns each step:

    ActionRequest                      core.interfaces.permission  (Phase 1)
        ↓
    tool policy lookup                 policies.py     deployment baseline + ceiling
        ↓
    risk classification                broker.py       reuses the EXISTING engines
        ↓
    User Constitution                  constitution.py deterministic, never prompted
        ↓
    permission grants                  grants.py       purpose-bound, expiring
        ↓
    PermissionDecision + ticket        broker.py       ticket only when permitted
        ↓
    Shadow World / Action Guard        gateway.py      the EXISTING safety stack
        ↓
    Trust Ledger + Proof Card          gateway.py      the EXISTING audit trail

Two things to know before changing anything here:

**This is not a second safety system.** Simulation, blast-radius grading, the
protected-path denylist, one-shot approval tokens, the hash-chained ledger and
Proof Cards all already existed and all still do the work. What Phase 4 adds is
the layer above them that asks whose rules apply and whether anyone actually
decided this was allowed.

**Execution requires a ticket.** `gateway.execute` takes an `ExecutionTicket`
that only `broker.evaluate` can mint. There is no `ticket=None` path and no flag
that skips it. That is what turns "tool calls should be checked" into "tool
calls cannot run unchecked" — see `ticket.py` for why the ticket is
authenticated rather than a plain dataclass.

**And what that sentence does NOT say (P9-05, finding F9-6).** It is a property
of *this function*, not yet of *this product*. Nothing in production calls
`gateway.execute`. The shipped execution surfaces — `/actions`, `/notes`,
`/calendar`, assist scheduling — go through `actions/gated_execution.py`, which
applies simulation, the owner gate, one-shot approval, the Trust Ledger and
Proof Cards, and never consults the broker, the Constitution or the grants. A
tenant's rules therefore constrain `/governance/preview` and nothing that
executes.

That path now refuses outright in the cloud edition (P9-04) rather than relying
on each caller passing `is_owner=`, which is False there — so no cloud action
executes ungoverned. Routing those surfaces through broker → ticket → execute is
the remaining work, and it is recorded in
`docs/cloud/evidence/PHASE_9_EXECUTION_PLAN.md` rather than implied by the
paragraph above. A module docstring that describes the architecture as if it
were the wiring is how a gap survives a reading.
"""
from __future__ import annotations

from core.governance.broker import (
    BrokerDecision,
    IRAPermissionBroker,
    build_broker,
    classify_risk,
)
from core.governance.constitution import ConstitutionVerdict, evaluate, summarise
from core.governance.effects import (
    clamp_to_ceiling,
    higher_risk,
    is_permissive,
    stricter_of,
)
from core.governance.gateway import ExecutionOutcome, ExecutionRefused, execute
from core.governance.grants import ANY_PURPOSE, PermissionGrant, find_covering
from core.governance.policies import (
    SEED_POLICIES,
    ToolPolicy,
    all_policies,
    never_automatic,
    policy_for,
)
from core.governance.rules import (
    STARTER_RULES,
    ConstitutionRule,
    InvalidRule,
    parse_rule,
)
from core.governance.ticket import (
    ExecutionTicket,
    InvalidTicket,
    TicketError,
    TicketMismatch,
)

__all__ = [
    "ANY_PURPOSE",
    "SEED_POLICIES",
    "STARTER_RULES",
    "BrokerDecision",
    "ConstitutionRule",
    "ConstitutionVerdict",
    "ExecutionOutcome",
    "ExecutionRefused",
    "ExecutionTicket",
    "IRAPermissionBroker",
    "InvalidRule",
    "InvalidTicket",
    "PermissionGrant",
    "TicketError",
    "TicketMismatch",
    "ToolPolicy",
    "all_policies",
    "build_broker",
    "clamp_to_ceiling",
    "classify_risk",
    "evaluate",
    "execute",
    "find_covering",
    "higher_risk",
    "is_permissive",
    "never_automatic",
    "parse_rule",
    "policy_for",
    "stricter_of",
    "summarise",
]
