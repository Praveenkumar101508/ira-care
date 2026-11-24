# IRA CARE: the small red-flag and answer classifier, trained on Kaggle's free GPU.
#
# A frozen open text encoder (sentence-transformers/all-MiniLM-L6-v2, Apache-2.0,
# 22 M parameters, runs on a CPU in milliseconds) turns a sentence into a 384-d
# vector; four small logistic heads read needs_help, pain, wellbeing and said /
# advice from it. Only the heads are trained (a few thousand numbers), so they
# travel back through the kernel log as JSON (`HEADS {json}`) and the server
# loads the public encoder itself. The training sentences are the repository's
# own templates (scripts/training/make_classifier_data.py), fetched from the
# public repository; the 150-turn evaluation set is never trained on.
import json
import time
import urllib.request

import numpy as np

T0 = time.time()
RAW = "https://raw.githubusercontent.com/Praveenkumar101508/ira-care/main/scripts/training/classifier_data.jsonl"
ENCODER = "sentence-transformers/all-MiniLM-L6-v2"

def log(m): print(m, flush=True)

import glob

attached = glob.glob("/kaggle/input/*/classifier_data.jsonl")   # the private Kaggle dataset run_kaggle.py attaches
if attached:
    rows = [json.loads(l) for l in open(attached[0]) if l.strip()]
    log(f"data from {attached[0]}")
else:
    rows = [json.loads(l) for l in urllib.request.urlopen(RAW, timeout=60).read().decode().splitlines() if l.strip()]
    log("data from the repository")
log(f"rows {len(rows)}")

import torch
from sentence_transformers import SentenceTransformer

dev = "cuda" if torch.cuda.is_available() else "cpu"
log(f"device {dev}")
enc = SentenceTransformer(ENCODER, device=dev)
X = enc.encode([r["text"] for r in rows], batch_size=128, normalize_embeddings=True, convert_to_numpy=True).astype(np.float32)

rng = np.random.default_rng(20261123)
idx = rng.permutation(len(rows)); cut = int(0.8 * len(rows))
tr, te = idx[:cut], idx[cut:]

def train_head(y, classes):
    """Multinomial logistic regression on the frozen embeddings (numpy, L2-regularised)."""
    yi = np.array([classes.index(v) for v in y]); K = len(classes); n, d = X.shape
    W = np.zeros((d, K), np.float32); b = np.zeros(K, np.float32)
    Y = np.eye(K, dtype=np.float32)[yi]
    weights = np.array([1.0 / max(1, (yi[tr] == k).sum()) for k in range(K)]); weights = weights / weights.mean()
    lr, lam = 0.5, 1e-3
    for it in range(600):
        z = X[tr] @ W + b; z -= z.max(1, keepdims=True); p = np.exp(z); p /= p.sum(1, keepdims=True)
        g = (p - Y[tr]) * weights[yi[tr]][:, None]
        W -= lr * (X[tr].T @ g / len(tr) + lam * W); b -= lr * g.mean(0)
    def predict(Xs): return (Xs @ W + b).argmax(1)
    acc_tr = float((predict(X[tr]) == yi[tr]).mean()); acc_te = float((predict(X[te]) == yi[te]).mean())
    # per-class recall on the held-out fifth
    recall = {c: round(float(((predict(X[te]) == k) & (yi[te] == k)).sum() / max(1, (yi[te] == k).sum())), 3) for k, c in enumerate(classes)}
    return {"classes": classes, "W": np.round(W, 3).tolist(), "b": np.round(b, 3).tolist(),
            "train_accuracy": round(acc_tr, 3), "test_accuracy": round(acc_te, 3), "test_recall": recall}

heads = {
    "needs_help": train_head([("yes" if r["needs_help"] else "no") for r in rows], ["no", "yes"]),
    "pain": train_head([("yes" if r.get("pain") else "no") for r in rows], ["no", "yes"]),
    "wellbeing": train_head([r["wellbeing"] for r in rows], ["unknown", "good", "ok", "poor"]),
    "said": train_head([r["said"] for r in rows], ["none", "yes", "no", "unclear"]),
    "advice": train_head([("yes" if r["advice"] else "no") for r in rows], ["no", "yes"]),
}
summary = {k: {kk: v[kk] for kk in ("train_accuracy", "test_accuracy", "test_recall")} for k, v in heads.items()}
log("METRICS " + json.dumps({"encoder": ENCODER, "licence": "Apache-2.0", "embedding_dim": int(X.shape[1]), "rows": len(rows),
                              "train_rows": len(tr), "test_rows": len(te), "heads": summary, "device": dev, "seconds": round(time.time() - T0, 1)}))
# The log keeps about 100 KB per line: the heads go out in numbered chunks.
blob = json.dumps({"encoder": ENCODER, "normalize": True, "heads": heads}, separators=(",", ":"))
CHUNK = 60000
parts = [blob[i:i + CHUNK] for i in range(0, len(blob), CHUNK)]
for i, part in enumerate(parts):
    log(f"HEADS_PART {i + 1}/{len(parts)} {part}")
log(f"DONE in {round(time.time() - T0)} s")
