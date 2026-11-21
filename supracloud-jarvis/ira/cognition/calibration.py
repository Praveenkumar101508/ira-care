"""Owner-scoped prediction outcomes and conservative calibration evidence.

The ledger measures the existing world model; it never updates weights.  A
model may only be labelled calibrated after a minimum real-outcome sample,
acceptable error, and zero recorded safety incidents.
"""
from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Iterable

from cognition.models import Prediction


@dataclass(frozen=True)
class PredictionOutcome:
    owner_id: str
    prediction_id: str
    succeeded: bool
    observed_effects: tuple[str, ...] = ()
    safety_incident: bool = False
    evidence_event_id: str | None = None
    outcome_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.owner_id or not self.prediction_id:
            raise ValueError("owner_id and prediction_id are required")
        if len(self.observed_effects) > 20:
            raise ValueError("observed_effects exceeds 20 items")
        if any(not item.strip() or len(item) > 500 for item in self.observed_effects):
            raise ValueError("observed effects must contain 1-500 characters")

    def to_dict(self) -> dict:
        out = asdict(self)
        out["observed_effects"] = list(self.observed_effects)
        return out


@dataclass(frozen=True)
class CalibrationReport:
    sample_count: int
    minimum_samples: int
    brier_score: float | None
    expected_calibration_error: float | None
    mean_predicted_success: float | None
    observed_success_rate: float | None
    successful_outcomes: int
    failed_outcomes: int
    safety_incidents: int
    calibrated: bool
    status: str

    def to_dict(self) -> dict:
        return asdict(self)


class PredictionLedger:
    """Bounded, owner-isolated registry for predictions and real outcomes."""

    def __init__(self, capacity: int = 2048, minimum_samples: int = 20) -> None:
        self.capacity = max(32, min(int(capacity), 8192))
        self.minimum_samples = max(10, min(int(minimum_samples), 1000))
        self._predictions: OrderedDict[str, Prediction] = OrderedDict()
        self._outcomes: OrderedDict[str, PredictionOutcome] = OrderedDict()

    def register(self, prediction: Prediction) -> Prediction:
        self._predictions[prediction.prediction_id] = prediction
        self._predictions.move_to_end(prediction.prediction_id)
        while len(self._predictions) > self.capacity:
            expired_id, _ = self._predictions.popitem(last=False)
            self._outcomes.pop(expired_id, None)
        return prediction

    def get(self, owner_id: str, prediction_id: str) -> Prediction:
        prediction = self._predictions.get(prediction_id)
        if prediction is None:
            raise KeyError(prediction_id)
        if prediction.owner_id != owner_id:
            raise PermissionError("prediction belongs to another owner")
        return prediction

    def record(
        self,
        owner_id: str,
        prediction_id: str,
        *,
        succeeded: bool,
        observed_effects: Iterable[str] = (),
        safety_incident: bool = False,
        evidence_event_id: str | None = None,
    ) -> PredictionOutcome:
        self.get(owner_id, prediction_id)
        if prediction_id in self._outcomes:
            raise ValueError("an outcome is already recorded for this prediction")
        outcome = PredictionOutcome(
            owner_id=owner_id,
            prediction_id=prediction_id,
            succeeded=bool(succeeded),
            observed_effects=tuple(observed_effects),
            safety_incident=bool(safety_incident),
            evidence_event_id=evidence_event_id,
        )
        self._outcomes[prediction_id] = outcome
        return outcome

    def recent(self, owner_id: str, limit: int = 20) -> list[PredictionOutcome]:
        bounded = max(1, min(int(limit), 100))
        items = [item for item in reversed(self._outcomes.values()) if item.owner_id == owner_id]
        return items[:bounded]

    def report(self, owner_id: str) -> CalibrationReport:
        records = [
            (self._predictions[prediction_id], outcome)
            for prediction_id, outcome in self._outcomes.items()
            if outcome.owner_id == owner_id and prediction_id in self._predictions
        ]
        count = len(records)
        incidents = sum(1 for _, outcome in records if outcome.safety_incident)
        if not records:
            return CalibrationReport(
                sample_count=0,
                minimum_samples=self.minimum_samples,
                brier_score=None,
                expected_calibration_error=None,
                mean_predicted_success=None,
                observed_success_rate=None,
                successful_outcomes=0,
                failed_outcomes=0,
                safety_incidents=0,
                calibrated=False,
                status="awaiting_outcomes",
            )

        probabilities = [prediction.success_probability for prediction, _ in records]
        observations = [1.0 if outcome.succeeded else 0.0 for _, outcome in records]
        brier = sum((probability - observed) ** 2 for probability, observed in zip(probabilities, observations)) / count

        bins: list[list[tuple[float, float]]] = [[] for _ in range(5)]
        for probability, observed in zip(probabilities, observations):
            bins[min(4, int(probability * 5))].append((probability, observed))
        ece = 0.0
        for bucket in bins:
            if not bucket:
                continue
            predicted = sum(item[0] for item in bucket) / len(bucket)
            actual = sum(item[1] for item in bucket) / len(bucket)
            ece += (len(bucket) / count) * abs(predicted - actual)

        enough_data = count >= self.minimum_samples
        successful = int(sum(observations))
        failed = count - successful
        calibrated = enough_data and successful > 0 and failed > 0 and brier <= 0.20 and ece <= 0.10 and incidents == 0
        status = "calibrated" if calibrated else "needs_improvement" if enough_data else "collecting_outcomes"
        return CalibrationReport(
            sample_count=count,
            minimum_samples=self.minimum_samples,
            brier_score=round(brier, 6),
            expected_calibration_error=round(ece, 6),
            mean_predicted_success=round(sum(probabilities) / count, 6),
            observed_success_rate=round(sum(observations) / count, 6),
            successful_outcomes=successful,
            failed_outcomes=failed,
            safety_incidents=incidents,
            calibrated=calibrated,
            status=status,
        )


__all__ = ["CalibrationReport", "PredictionLedger", "PredictionOutcome"]
