"""Tool policies — the deployment's baseline, and the ceiling nothing exceeds.

Mirrors the seed block in `postgres/025_governance.sql`. As in Phase 3, the
table is the durable record and these constants are what the process actually
reads; `tests/governance/test_policy_matches_migration.py` asserts the two
agree, field by field, so the pair cannot drift into a state where which answer
is true depends on the code path.

── Why a policy is a ceiling and not a default ──────────────────────────────

`default_effect` is the answer when nothing else applies. `max_effect` is the
most permissive answer that is EVER possible for that action, whatever a user's
Constitution says and whatever they have granted.

Those are different jobs and conflating them is how safety rails get removed by
configuration. "email.send has max_effect=require_confirmation" means no rule,
no grant and no accumulated session state can make sending silent. A user who
writes "always allow email" gets confirmation anyway — and the decision record
says why, so it does not look like a malfunction.

── Unknown actions ──────────────────────────────────────────────────────────

There is no policy for an action not listed here, and `policy_for` returns None
rather than inventing a permissive default. The broker turns that into a denial.
An action nobody has classified is precisely the one nobody has thought about.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Optional

from core.interfaces.permission import Effect, RiskLevel


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """What this build permits for one `domain.action`."""

    domain: str
    action: str
    max_effect: Effect
    default_effect: Effect
    risk: RiskLevel
    side_effecting: bool
    #: Shadow World action kind to simulate under. None means no simulator
    #: exists — which, for a side-effecting action, is a reason to require
    #: confirmation rather than to skip simulation.
    shadow_kind: Optional[str] = None
    description: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.domain, self.action)


def _p(
    domain: str, action: str, max_effect: Effect, default_effect: Effect,
    risk: RiskLevel, side_effecting: bool, shadow_kind: Optional[str], description: str,
) -> ToolPolicy:
    return ToolPolicy(
        domain=domain, action=action, max_effect=max_effect,
        default_effect=default_effect, risk=risk, side_effecting=side_effecting,
        shadow_kind=shadow_kind, description=description,
    )


A = Effect.ALLOW
D = Effect.DENY
C = Effect.REQUIRE_CONFIRMATION
S = Effect.ALLOW_FOR_SESSION
R = Effect.ALLOW_FOR_ROUTINE

LOW, MED, HIGH, CRIT = RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL

SEED_POLICIES: Final[tuple[ToolPolicy, ...]] = (
    # Email. Sending can never be automatic; deletion is not offered at all —
    # the brief is explicit that email deletion is out of scope initially, and
    # an explicit deny is more honest than an absent policy.
    _p("email", "search", S, D, LOW, False, None, "Search the mailbox"),
    _p("email", "read", C, D, MED, False, None, "Read a selected message"),
    _p("email", "draft", S, C, LOW, False, None, "Prepare a draft — never sends"),
    _p("email", "send", C, C, HIGH, True, "email_send", "Send an email"),
    _p("email", "delete", D, D, CRIT, True, None, "Not supported in this build"),

    # Calendar.
    _p("calendar", "read", S, D, LOW, False, None, "Read the calendar"),
    _p("calendar", "create", C, C, MED, True, "calendar_change", "Create an event"),
    _p("calendar", "delete", C, D, HIGH, True, "calendar_change", "Delete an event"),

    # Files. Deletion reaches Action Guard, which holds the protected-path
    # denylist and takes a rollback snapshot BEFORE approval.
    _p("files", "read", S, D, LOW, False, None, "Read a file"),
    _p("files", "write", C, C, MED, True, "file_change", "Write a file"),
    _p("files", "delete", C, D, HIGH, True, "file_change", "Delete a file"),

    # Memory. An assistant that can silently forget is one that can be MADE to
    # forget, so deletion confirms and never becomes automatic.
    _p("memory", "read", A, A, LOW, False, None, "Read stored memory"),
    _p("memory", "write", S, C, LOW, False, None, "Store a memory"),
    _p("memory", "delete", C, C, MED, True, "memory_deletion", "Delete a memory"),

    # Shell. Graded by the Action Guard's existing two gates.
    _p("shell", "run", C, D, HIGH, True, "shell_command", "Run a shell command"),

    # Outbound network — off by default since Phase 0.
    _p("web", "search", C, D, MED, False, "external_api_call", "Search the web"),
    _p("web", "fetch", C, D, MED, False, "external_api_call", "Fetch a URL"),

    # Consumer-cloud exclusions (brief §7). Explicit denies so an attempt is
    # refused BY POLICY and leaves a decision row, rather than falling through
    # an unknown-action path where it would be indistinguishable from a typo.
    _p("payment", "execute", D, D, CRIT, True, None, "Never supported"),
    _p("social", "publish", D, D, CRIT, True, None, "Never supported"),
    _p("security", "change", D, D, CRIT, True, "permission_change", "Not changeable by an agent"),
)

_BY_KEY: Final[dict[tuple[str, str], ToolPolicy]] = {p.key: p for p in SEED_POLICIES}


def policy_for(domain: str, action: str) -> Optional[ToolPolicy]:
    """The policy for an action, or None if this build has never classified it.

    None is not "allowed". The broker turns it into a denial with a reason
    naming the missing policy, because an unclassified action is the one nobody
    has thought about.
    """
    return _BY_KEY.get((domain, action))


def all_policies() -> tuple[ToolPolicy, ...]:
    return SEED_POLICIES


def never_automatic() -> tuple[tuple[str, str], ...]:
    """Actions that can never resolve to silent execution, whatever is granted.

    Derived from `max_effect` rather than listed separately — a second list
    would be one edit away from disagreeing with the policies it describes.
    """
    from core.governance.effects import is_permissive

    return tuple(p.key for p in SEED_POLICIES if not is_permissive(p.max_effect))


__all__ = [
    "SEED_POLICIES",
    "ToolPolicy",
    "all_policies",
    "never_automatic",
    "policy_for",
]
