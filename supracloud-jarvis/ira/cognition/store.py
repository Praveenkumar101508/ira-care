"""Optional PostgreSQL persistence for cognitive records (parameterized only)."""
from __future__ import annotations

import json
import logging

from cognition.calibration import PredictionOutcome
from cognition.cognitive_loop import CognitiveCycle
from cognition.models import CognitiveEvent, Goal, Prediction
from core.identity.db_context import owner_connection

logger = logging.getLogger("ira.cognition.store")


class CognitiveStore:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    async def append_event(self, event: CognitiveEvent) -> bool:
        if not self.enabled:
            return False
        try:
            async with owner_connection("cognitive loop") as conn:
                await conn.execute(
                    "INSERT INTO cognitive_events (event_id, user_id, kind, source, summary, payload, metrics) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    event.event_id, event.user_id, event.kind.value, event.source,
                    event.summary, json.dumps(event.payload), json.dumps({
                        "trust": event.trust, "confidence": event.confidence,
                        "salience": event.salience, "urgency": event.urgency,
                        "novelty": event.novelty,
                    }),
                )
            return True
        except Exception as exc:
            logger.warning("cognitive event persistence unavailable: %s", exc)
            return False

    async def save_goal(self, goal: Goal) -> bool:
        if not self.enabled:
            return False
        try:
            async with owner_connection("cognitive loop") as conn:
                await conn.execute(
                    "INSERT INTO cognitive_goals (goal_id, owner_id, title, desired_outcome, priority, state, deadline, source) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                    "ON CONFLICT (goal_id) DO UPDATE SET priority = EXCLUDED.priority, state = EXCLUDED.state, updated_at = NOW()",
                    goal.goal_id, goal.owner_id, goal.title, goal.desired_outcome,
                    goal.priority, goal.state.value, goal.deadline, goal.source,
                )
            return True
        except Exception as exc:
            logger.warning("cognitive goal persistence unavailable: %s", exc)
            return False

    async def append_prediction(self, prediction: Prediction) -> bool:
        if not self.enabled:
            return False
        try:
            async with owner_connection("cognitive loop") as conn:
                await conn.execute(
                    "INSERT INTO cognitive_predictions "
                    "(prediction_id, owner_id, action, expected_effects, success_probability, risk, uncertainty, reversible, model_version) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                    prediction.prediction_id, prediction.owner_id, prediction.action,
                    json.dumps(prediction.expected_effects), prediction.success_probability,
                    prediction.risk, prediction.uncertainty, prediction.reversible,
                    prediction.model_version,
                )
            return True
        except Exception as exc:
            logger.warning("cognitive prediction persistence unavailable: %s", exc)
            return False

    async def append_outcome(self, outcome: PredictionOutcome) -> bool:
        if not self.enabled:
            return False
        try:
            async with owner_connection("cognitive loop") as conn:
                await conn.execute(
                    "INSERT INTO cognitive_prediction_outcomes "
                    "(outcome_id, prediction_id, owner_id, succeeded, observed_effects, safety_incident, evidence_event_id) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    outcome.outcome_id,
                    outcome.prediction_id,
                    outcome.owner_id,
                    outcome.succeeded,
                    json.dumps(list(outcome.observed_effects)),
                    outcome.safety_incident,
                    outcome.evidence_event_id,
                )
            return True
        except Exception as exc:
            logger.warning("cognitive outcome persistence unavailable: %s", exc)
            return False

    async def append_cycle(self, cycle: CognitiveCycle) -> bool:
        if not self.enabled:
            return False
        try:
            async with owner_connection("cognitive loop") as conn:
                await conn.execute(
                    "INSERT INTO cognitive_cycles "
                    "(cycle_id, owner_id, summary, percept_event_id, alternatives, selected_proposal_id) "
                    "VALUES ($1, $2, $3, $4, $5, $6)",
                    cycle.cycle_id,
                    cycle.owner_id,
                    cycle.summary,
                    cycle.percept_event_id,
                    json.dumps([item.to_dict() for item in cycle.alternatives]),
                    cycle.selected_proposal_id,
                )
            return True
        except Exception as exc:
            logger.warning("cognitive cycle persistence unavailable: %s", exc)
            return False


__all__ = ["CognitiveStore"]
