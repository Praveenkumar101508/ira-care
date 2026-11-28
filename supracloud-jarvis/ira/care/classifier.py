"""The small red-flag and answer classifier (round 3, phase 5e).

A frozen open text encoder (`sentence-transformers/all-MiniLM-L6-v2`, Apache-2.0,
run on the CPU through ONNX Runtime) and four logistic heads trained on Kaggle
(`scripts/training/kaggle_redflag_classifier.py`; the heads are the JSON file
`care/models/classifier_heads.json`). It reads an answer into the same fields
the keyword rules and the language model produce: needs_help, pain, wellbeing,
said, advice.

It never relaxes anything. `conversation.merge` takes the stricter reading of
the rules, the model and this classifier: a help signal from any of the three is
a help signal; a "poor" from any is "poor". Off by default in CI (no encoder on
the runner); `IRA_CARE_CLASSIFIER=on` and the encoder files turn it on, and
`scripts/training/fetch_encoder.py` downloads them.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

logger = logging.getLogger("ira.care.classifier")

HEADS_PATH = Path(__file__).resolve().parent / "models" / "classifier_heads.json"
ENCODER_DIR_ENV = "IRA_CARE_ENCODER_DIR"
ENABLED_ENV = "IRA_CARE_CLASSIFIER"


@dataclass(frozen=True)
class ClassifierReading:
    needs_help: bool
    pain: bool
    wellbeing: str          # good | ok | poor | unknown
    said: str               # yes | no | unclear | none
    advice: bool
    confidence: dict


class Heads:
    """The trained logistic heads over a normalised embedding."""

    def __init__(self, spec: dict) -> None:
        self.encoder = spec.get("encoder", "")
        self.heads = {name: (h["classes"], np.array(h["W"], np.float32), np.array(h["b"], np.float32))
                      for name, h in spec["heads"].items()}

    @classmethod
    def load(cls, path: Path = HEADS_PATH) -> "Heads":
        return cls(json.loads(Path(path).read_text()))

    def read(self, embedding: np.ndarray) -> ClassifierReading:
        out: dict[str, str] = {}
        conf: dict[str, float] = {}
        for name, (classes, W, b) in self.heads.items():
            z = embedding @ W + b
            z = z - z.max()
            p = np.exp(z) / np.exp(z).sum()
            k = int(p.argmax())
            out[name] = classes[k]
            conf[name] = round(float(p[k]), 3)
        return ClassifierReading(
            needs_help=out.get("needs_help") == "yes", pain=out.get("pain") == "yes",
            wellbeing=out.get("wellbeing", "unknown"), said=out.get("said", "none"),
            advice=out.get("advice") == "yes", confidence=conf)


class OnnxEncoder:
    """all-MiniLM-L6-v2 through ONNX Runtime: mean pooling, then L2 normalisation,
    exactly as sentence-transformers does, so the heads trained on its vectors apply."""

    def __init__(self, directory: Path) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.tok = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tok.enable_truncation(128)
        self.sess = ort.InferenceSession(str(directory / "model.onnx"), providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}

    def __call__(self, texts: Sequence[str]) -> np.ndarray:
        enc = self.tok.encode_batch(list(texts))
        n = max(len(e.ids) for e in enc)
        ids = np.zeros((len(enc), n), np.int64); mask = np.zeros((len(enc), n), np.int64)
        for i, e in enumerate(enc):
            ids[i, :len(e.ids)] = e.ids; mask[i, :len(e.ids)] = 1
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self.sess.run(None, feed)[0]                       # (batch, tokens, 384)
        m = mask[:, :, None].astype(np.float32)
        pooled = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
        return pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)


Encoder = Callable[[Sequence[str]], np.ndarray]


TALK_HEAD_PATH = Path(__file__).resolve().parent / "models" / "redflag_talk_head.json"


class TalkRedFlagHead:
    """The red-flag head for what the person says to IRA (D-114): a small two-layer network on the
    same encoder, trained on an original synthetic corpus (`scripts/training/redflag_talk_head.py`).
    It only ever ADDS a red flag behind the keyword rules; it never removes one."""

    def __init__(self, spec: dict) -> None:
        self.W1 = np.asarray(spec["W1"], np.float32)
        self.b1 = np.asarray(spec["b1"], np.float32)
        self.w2 = np.asarray(spec["w2"], np.float32)
        self.b2 = float(spec["b2"])
        self.threshold = float(spec["threshold"])
        self.corpus_sha256 = spec.get("corpus", {}).get("sha256", "")

    @classmethod
    def load(cls, path: Path = TALK_HEAD_PATH) -> "TalkRedFlagHead":
        return cls(json.loads(Path(path).read_text()))

    def probabilities(self, embeddings) -> list[float]:
        h = np.tanh(np.asarray(embeddings, np.float32) @ self.W1 + self.b1)
        return [float(v) for v in 1 / (1 + np.exp(-(h @ self.w2 + self.b2)))]


class Classifier:
    def __init__(self, encoder: Encoder, heads: Heads, talk_head: Optional[TalkRedFlagHead] = None) -> None:
        self.encoder, self.heads, self.talk_head = encoder, heads, talk_head

    def read(self, text: str) -> ClassifierReading:
        return self.heads.read(self.encoder([text])[0])

    def talk_red_flag(self, text: str) -> bool:
        """True when the talk head reads an emergency in what was said (at its threshold)."""
        if self.talk_head is None or not (text or "").strip():
            return False
        return self.talk_head.probabilities(self.encoder([text]))[0] >= self.talk_head.threshold


_instance: Optional[Classifier] = None
_tried = False


def configured() -> Optional[Classifier]:
    """The classifier when it is turned on and its files exist; None otherwise.
    Tests inject one with `use_classifier`."""
    global _instance, _tried
    if _instance is not None or _tried:
        return _instance
    _tried = True
    if (os.getenv(ENABLED_ENV) or "off").strip().lower() not in ("on", "1", "true", "yes"):
        return None
    directory = Path(os.getenv(ENCODER_DIR_ENV) or (Path(__file__).resolve().parent / "models" / "encoder"))
    try:
        talk_head = TalkRedFlagHead.load() if TALK_HEAD_PATH.exists() else None
        _instance = Classifier(OnnxEncoder(directory), Heads.load(), talk_head)
        logger.info("care classifier on: %s from %s", _instance.heads.encoder, directory)
    except Exception as exc:  # noqa: BLE001 - the rules and the model still run
        logger.warning("care classifier unavailable (%s): the keyword rules and the model carry on", type(exc).__name__)
        _instance = None
    return _instance


def use_classifier(instance: Optional[Classifier]) -> None:
    """Test hook."""
    global _instance, _tried
    _instance, _tried = instance, True


__all__ = ["Classifier", "ClassifierReading", "Heads", "OnnxEncoder", "TalkRedFlagHead", "TALK_HEAD_PATH", "configured", "use_classifier", "HEADS_PATH"]
