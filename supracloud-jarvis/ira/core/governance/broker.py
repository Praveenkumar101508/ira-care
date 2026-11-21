"""The Permission Broker — the one place a proposed action is judged.

Implements the `PermissionBroker` protocol Phase 1 defined. The pipeline is the
transformation brief's, in order:

    ActionRequest
        ↓  tool policy lookup            unknown action → DENY
        ↓  risk classification           reuses the EXISTING risk engines
        ↓  Constitution Engine           the user's permanent rules
        ↓  permission grants             what they actually allowed, for what
        ↓  clamp to the policy ceiling   nothing exceeds the deployment baseline
        ↓  critical-risk backstop        critical is blocked outright
    PermissionDecision (+ ExecutionTicket when permitted)

── This is not a second safety system ───────────────────────────────────────

The brief is emphatic: *"Do not build a second disconnected safety system."*
So the broker deliberately does NOT classify risk itself. It asks the engines
IRA already has —`commands/risk.py` and the classifiers in
`trust/action_guard.py` — and takes the highest answer. When those say an action
is high risk, this says so too, automatically, because it is the same source.

What the broker adds is the layer above them: whose rules apply, what was
granted, and for what purpose. Execution, simulation, snapshots, approval tokens
and the ledger all remain the existing stack's job (see `gateway.py`).

── Fail closed, everywhere ──────────────────────────────────────────────────

Every path that cannot establish a positive answer returns DENY:

  * no policy for the action           → deny (nobody has classified it)
  * policy default is deny, no grant   → deny
  * a matching constitution DENY       → deny, and no grant can override it
  * critical risk                      → deny, and no approval can release it
  * an exception anywhere              → deny (see `decide`'s except clause)

The last one matters most. A broker that raises on an unexpected input would let
a caller with a bare `except` treat the failure as "carry on".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence

from core.governance import constitution as _constitution
from core.governance import ticket as _ticket
from core.governance.effects import (
    clamp_to_ceiling,
    higher_risk,
    is_permissive,
)
from core.governance.grants import PermissionGrant, find_covering
from core.governance.policies import ToolPolicy, policy_for
from core.governance.rules import ConstitutionRule
from core.interfaces.permission import (
    ActionRequest,
    Effect,
    PermissionDecision,
    RiskLevel,
)

logger = logging.getLogger("ira.governance.broker")


@dataclass(frozen=True, slots=True)
class BrokerDecision:
    """A decision, plus the ticket that permits execution when there is one.

    The ticket is `None` for every non-permissive effect, which is what makes
    "denied" and "needs approval" structurally unable to execute rather than
    merely discouraged from it.
    """

    decision: PermissionDecision
    ticket: Optional[_ticket.ExecutionTicket] = None
    policy: Optional[ToolPolicy] = None
    matched_rule: Optional[ConstitutionRule] = None
    matched_grant: Optional[PermissionGrant] = None

    @property
    def effect(self) -> Effect:
        return self.decision.effect

    @property
    def may_execute(self) -> bool:
        """True only when a ticket exists — which is the whole authority.

        This used to read `self.decision.is_allowed and self.ticket is not
        None`. The two clauses were equivalent, because a ticket was minted
        only for a permissive effect, and the redundancy was harmless.

        It stopped being equivalent when a confirmation became able to satisfy
        `require_confirmation` (P9-07). The DECISION still records
        `require_confirmation` — that is what the tenant's rules and the
        deployment ceiling said, and rewriting it would erase the requirement
        from the audit record — while the TICKET records that the requirement
        was met. Asking the decision would then refuse every confirmed action.

        Keying on the ticket is not a weakening: `ticket.issue()` REFUSES to
        mint for a non-permissive effect, so a ticket cannot exist for a denial
        or for an unmet confirmation requirement. The ticket is also the only
        thing `gateway.execute` accepts, and it has no `ticket=None` path — so
        this property now answers exactly the question the gateway will ask.
        """
        return self.ticket is not None


def classify_risk(request: ActionRequest, policy: ToolPolicy) -> RiskLevel:
    """Risk for this request — the policy baseline, raised by existing engines.

    Never lowered. `trust/action_guard.py` already enforces "a caller may raise
    risk, never lower it" for its derived risk; the same rule holds here so the
    two layers cannot disagree about which direction is safe.
    """
    risk = policy.risk

    # Shell commands: reuse the Action Guard's two-gate grading rather than
    # inventing a second opinion about what a dangerous command looks like.
    if request.domain == "shell" and request.action == "run":
        try:
            from trust.action_guard import classify_command_risk

            derived, _why = classify_command_risk(str(request.resource or ""))
            risk = higher_risk(risk, RiskLevel(derived))
        except Exception:  # pragma: no cover - defensive; policy risk still applies
            logger.warning("command risk classifier unavailable; using policy risk")
            risk = higher_risk(risk, RiskLevel.HIGH)

    # File operations: the Action Guard knows the protected-path denylist.
    if request.domain == "files" and request.action in ("write", "delete"):
        try:
            from pathlib import Path

            from trust.action_guard import classify_file_risk

            derived = classify_file_risk(Path(str(request.resource or ".")), request.action)
            risk = higher_risk(risk, RiskLevel(derived))
        except Exception:  # pragma: no cover - defensive
            logger.warning("file risk classifier unavailable; using policy risk")
            risk = higher_risk(risk, RiskLevel.HIGH)

    # An action the model proposed rather than the user asking for directly is
    # not automatically more dangerous — but it IS untrusted-origin, and a
    # low-risk classification on a side-effecting action proposed by a model is
    # exactly the combination prompt injection aims for.
    if request.model_proposed and policy.side_effecting and risk is RiskLevel.LOW:
        risk = RiskLevel.MEDIUM

    return risk


class IRAPermissionBroker:
    """The Permission Broker.

    Stateless with respect to rules and grants: they are passed in. That keeps
    the decision logic pure and exhaustively testable, and leaves loading — and
    therefore tenant scoping — to `store.py`, which does it through
    `tenant_connection`.
    """

    def __init__(
        self,
        *,
        rules: Sequence[ConstitutionRule] = (),
        grants: Sequence[PermissionGrant] = (),
        now: Optional[datetime] = None,
    ) -> None:
        self._rules = tuple(rules)
        self._grants = tuple(grants)
        self._now = now

    # ── The protocol method ─────────────────────────────────────────────────

    async def decide(self, request: ActionRequest) -> PermissionDecision:
        """PermissionBroker protocol. Returns the decision only."""
        return self.evaluate(request).decision

    # ── The full answer ─────────────────────────────────────────────────────

    def evaluate(self, request: ActionRequest, *,
                 confirmation: Optional[str] = None) -> BrokerDecision:
        """Decide, and mint a ticket when execution may proceed.

        `confirmation` is a reference to an approval the caller has ALREADY
        obtained and verified — in the product path, the one-shot token
        `utils/approval.ApprovalGuardrail` issued and is about to consume. It
        satisfies an effect of `require_confirmation` and nothing else: a
        denial stays denied and a critical action stays blocked. Passing it
        without holding a real approval is a lie told to the audit record, not
        a bypass of one — the token is still consumed inside the executor, and
        a stale or replayed one fails there.
        """
        try:
            return self._evaluate(request, confirmation=confirmation)
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            # Fail closed on anything unexpected. Raising here would let a
            # caller with a bare `except` treat the failure as "carry on".
            logger.exception("permission evaluation failed; denying")
            return BrokerDecision(
                decision=PermissionDecision(
                    effect=Effect.DENY,
                    reason=f"Permission evaluation failed and denied: {type(exc).__name__}",
                    risk=RiskLevel.HIGH,
                    source="broker-exception",
                )
            )

    def _evaluate(self, request: ActionRequest, *,
                  confirmation: Optional[str] = None) -> BrokerDecision:
        # ── 1. Policy ───────────────────────────────────────────────────────
        policy = policy_for(request.domain, request.action)
        if policy is None:
            return BrokerDecision(
                decision=PermissionDecision(
                    effect=Effect.DENY,
                    reason=(
                        f"No tool policy exists for {request.domain}.{request.action}. "
                        f"An action this build has never classified is refused."
                    ),
                    risk=RiskLevel.HIGH,
                    source="no-policy",
                )
            )

        # ── 2. Risk ─────────────────────────────────────────────────────────
        risk = classify_risk(request, policy)

        # Critical is blocked outright and no approval can release it — the same
        # rule actions/shadow_world.APPROVAL_BY_RISK already encodes
        # ("critical": "blocked"). Checked before rules and grants so that
        # nothing a user writes can reach a critical action at all.
        if risk is RiskLevel.CRITICAL:
            return BrokerDecision(
                decision=PermissionDecision(
                    effect=Effect.DENY,
                    reason=(
                        f"{request.domain}.{request.action} is classified critical "
                        f"and is blocked outright. No approval can release it."
                    ),
                    risk=risk,
                    source="critical-blocked",
                ),
                policy=policy,
            )

        # ── 3. Constitution ─────────────────────────────────────────────────
        verdict = _constitution.evaluate(request, self._rules, risk=risk)

        if verdict.effect is Effect.DENY:
            return BrokerDecision(
                decision=PermissionDecision(
                    effect=Effect.DENY,
                    reason=verdict.reason,
                    risk=risk,
                    source="constitution",
                ),
                policy=policy,
                matched_rule=verdict.deciding_rule,
            )

        # ── 4. Grants ───────────────────────────────────────────────────────
        grant = find_covering(request, self._grants, now=self._now)
        granted_effect = grant.effect() if grant is not None else policy.default_effect
        grant_reason = (
            f"Granted for {grant.purpose!r} until "
            f"{grant.expires_at.isoformat() if grant.expires_at else 'revoked'}."
            if grant is not None
            else f"No grant covers this; the deployment default applies."
        )

        # ── 5. Combine, then clamp to the ceiling ───────────────────────────
        combined = _constitution.combine_with(verdict, granted_effect)
        effect = clamp_to_ceiling(combined, policy.max_effect)

        clamped = effect is not combined
        reason_parts = [verdict.reason if verdict.has_opinion else grant_reason]
        if clamped:
            reason_parts.append(
                f"Clamped to {effect.value!r}: this deployment never allows "
                f"{request.domain}.{request.action} to be more permissive than that."
            )
        elif verdict.has_opinion and grant is None and combined is not policy.default_effect:
            # The user's rule moved the answer off the deployment default. Worth
            # saying, so a permissive outcome never looks like it came from
            # nowhere when someone reads the decision log.
            reason_parts.append(
                f"This overrides the deployment default of "
                f"{policy.default_effect.value!r} for this action."
            )

        source = (
            "constitution" if verdict.has_opinion
            else ("grant" if grant is not None else "policy-default")
        )
        if clamped:
            source = f"{source}+policy-ceiling"

        decision = PermissionDecision(
            effect=effect,
            reason=" ".join(reason_parts),
            risk=risk,
            source=source,
        )

        # ── 6. Ticket, only when execution may actually proceed ─────────────
        issued = None
        if is_permissive(effect):
            issued = _ticket.issue(
                request,
                effect=effect,
                risk=risk,
                reason=decision.reason,
                source=source,
            )
        elif effect is Effect.REQUIRE_CONFIRMATION and confirmation:
            # ── A confirmation satisfies a requirement for confirmation ─────
            #
            # And nothing else. This branch was missing until P9-07, and its
            # absence meant NO SIDE-EFFECTING ACTION COULD EVER EXECUTE through
            # the governed path: every such policy has a `max_effect` ceiling of
            # `require_confirmation` (`email.send`, `calendar.create`,
            # `files.write`), the clamp pins the answer there whatever the
            # tenant's rule says, and a non-permissive effect mints no ticket.
            # The governance machinery was complete and terminal.
            #
            # It surfaced only when the product was actually wired to it —
            # which is the argument for wiring the product to it.
            #
            # The rule is deliberately the narrowest one that works:
            #
            #   * DENY is untouched. A confirmation cannot release a refusal —
            #     `verdict.effect is Effect.DENY` returned several branches ago
            #     and never reaches here.
            #   * CRITICAL is untouched, for the same reason, and its own
            #     branch says "no approval can release it".
            #   * REQUIRE_CONFIRMATION with NO confirmation still mints nothing.
            #     The caller must present one; absence is not consent.
            #
            # The DECISION still reads `require_confirmation` — that is what the
            # tenant's rules and this deployment's ceiling actually said, and
            # rewriting it to `allow` would erase the requirement from the audit
            # record. What changes is the TICKET: it records that the
            # requirement was met, and by which approval.
            issued = _ticket.issue(
                request,
                effect=Effect.ALLOW,
                risk=risk,
                reason=f"{decision.reason} Confirmation was obtained.",
                source=f"{source}+confirmed",
                approval_request_id=str(confirmation),
            )

        return BrokerDecision(
            decision=decision,
            ticket=issued,
            policy=policy,
            matched_rule=verdict.deciding_rule,
            matched_grant=grant,
        )

    # ── Confirmation ────────────────────────────────────────────────────────

    def ticket_after_approval(
        self,
        request: ActionRequest,
        *,
        approval_request_id: str,
        risk: RiskLevel = RiskLevel.MEDIUM,
    ) -> _ticket.ExecutionTicket:
        """Mint a ticket for an action whose confirmation has been OBTAINED.

        Called only by the approval flow, after a user actually approved. It
        re-checks the policy ceiling and the critical backstop rather than
        trusting that the earlier decision is still valid — a rule may have been
        added, or the policy reloaded, between the request and the approval.
        """
        policy = policy_for(request.domain, request.action)
        if policy is None:
            raise _ticket.TicketError(
                f"No tool policy for {request.domain}.{request.action}; refusing "
                f"to issue a ticket even with an approval."
            )
        effective_risk = higher_risk(risk, classify_risk(request, policy))
        if effective_risk is RiskLevel.CRITICAL:
            raise _ticket.TicketError(
                "Critical actions are blocked outright; an approval cannot "
                "release one."
            )
        if policy.max_effect is Effect.DENY:
            raise _ticket.TicketError(
                f"{request.domain}.{request.action} is denied by deployment "
                f"policy; an approval cannot release it."
            )

        # A re-check of the user's own rules too: an approval obtained before a
        # rule was written must not outlive the rule.
        verdict = _constitution.evaluate(request, self._rules, risk=effective_risk)
        if verdict.effect is Effect.DENY:
            raise _ticket.TicketError(
                f"Refusing despite approval: {verdict.reason}"
            )

        return _ticket.issue(
            request,
            effect=Effect.ALLOW,
            risk=effective_risk,
            reason="Approved by the user.",
            source="approval",
            approval_request_id=approval_request_id,
        )


def build_broker(
    *,
    rules: Iterable[ConstitutionRule] = (),
    grants: Iterable[PermissionGrant] = (),
) -> IRAPermissionBroker:
    return IRAPermissionBroker(rules=tuple(rules), grants=tuple(grants))


__all__ = ["BrokerDecision", "IRAPermissionBroker", "build_broker", "classify_risk"]
