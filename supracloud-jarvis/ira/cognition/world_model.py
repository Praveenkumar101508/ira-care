"""Local predictive world model backed by a small deterministic neural network.

This is an actual feed-forward neural transition model, implemented without a
large runtime dependency so it works in IRA's lightweight and offline modes.
Its initial weights are deterministic and untrained; output is therefore
labelled uncertain. Evaluated adapter activation is handled separately by the
plasticity quarantine and is OFF by default.
"""
from __future__ import annotations

import hashlib
import math
import random
import re
from collections import OrderedDict
from typing import Any

from cognition.models import Belief, Prediction, clamp

_TOKEN = re.compile(r"[a-z0-9_./:-]+")
_IRREVERSIBLE = {"send", "delete", "publish", "purchase", "transfer", "revoke"}
_RISKY = {"password", "secret", "payment", "permission", "admin", "shell", "cloud", "external"}


class TinyNeuralTransitionModel:
    """24→16→4 MLP used to estimate success, impact, risk and uncertainty."""

    version = "tiny-transition-v1-untrained"

    def __init__(self, seed: int = 8112026) -> None:
        rng = random.Random(seed)
        self.input_size = 24
        self.hidden_size = 16
        self.w1 = [[rng.uniform(-0.22, 0.22) for _ in range(self.input_size)] for _ in range(self.hidden_size)]
        self.b1 = [rng.uniform(-0.05, 0.05) for _ in range(self.hidden_size)]
        self.w2 = [[rng.uniform(-0.22, 0.22) for _ in range(self.hidden_size)] for _ in range(4)]
        self.b2 = [0.0, 0.0, -0.15, 0.65]  # conservative: high uncertainty before calibration

    def encode(self, text: str, context: dict[str, Any]) -> list[float]:
        vector = [0.0] * self.input_size
        tokens = _TOKEN.findall(text.lower())[:128]
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = digest[0] % self.input_size
            vector[index] += 1.0 if digest[1] % 2 else -1.0
        for index, key in enumerate(sorted(context)[:8]):
            value = context[key]
            if isinstance(value, (int, float, bool)):
                vector[(index * 3 + 1) % self.input_size] += float(value)
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def forward(self, features: list[float]) -> tuple[float, float, float, float]:
        hidden = [
            math.tanh(sum(weight * value for weight, value in zip(row, features)) + bias)
            for row, bias in zip(self.w1, self.b1)
        ]
        raw = [sum(weight * value for weight, value in zip(row, hidden)) + bias for row, bias in zip(self.w2, self.b2)]
        return tuple(1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, value)))) for value in raw)  # type: ignore[return-value]

    def predict(self, text: str, context: dict[str, Any]) -> tuple[float, float, float, float]:
        return self.forward(self.encode(text, context))


class PredictiveWorldModel:
    def __init__(self, max_beliefs: int = 256) -> None:
        self.neural = TinyNeuralTransitionModel()
        self.max_beliefs = max(32, min(int(max_beliefs), 2048))
        self._beliefs: OrderedDict[str, Belief] = OrderedDict()

    def observe(self, key: str, value: Any, confidence: float, evidence_id: str) -> Belief:
        existing = self._beliefs.get(key)
        evidence = list(existing.evidence_ids[-7:]) if existing else []
        evidence.append(evidence_id)
        belief = Belief(key=key, value=value, confidence=clamp(confidence), evidence_ids=evidence)
        self._beliefs[key] = belief
        self._beliefs.move_to_end(key)
        while len(self._beliefs) > self.max_beliefs:
            self._beliefs.popitem(last=False)
        return belief

    def beliefs(self) -> list[Belief]:
        return list(reversed(self._beliefs.values()))

    def predict(self, owner_id: str, action: str, context: dict[str, Any] | None = None) -> Prediction:
        context = dict(context or {})
        success, impact, neural_risk, uncertainty = self.neural.predict(action, context)
        words = set(_TOKEN.findall(action.lower()))
        lexical_risk = min(1.0, len(words & _RISKY) * 0.22 + len(words & _IRREVERSIBLE) * 0.16)
        risk = clamp(0.55 * neural_risk + 0.45 * lexical_risk)
        reversible = not bool(words & _IRREVERSIBLE)
        expected = ["state transition predicted from the supplied action"]
        if impact > 0.62:
            expected.append("material downstream impact is possible")
        if lexical_risk:
            expected.append("sensitive or external resources may be affected")
        return Prediction(
            owner_id=owner_id,
            action=action,
            expected_effects=expected,
            success_probability=round(clamp(success * (1 - 0.25 * uncertainty)), 6),
            risk=round(risk, 6),
            uncertainty=round(max(0.35, clamp(uncertainty)), 6),
            reversible=reversible,
            model_version=self.neural.version,
        )


__all__ = ["PredictiveWorldModel", "TinyNeuralTransitionModel"]
