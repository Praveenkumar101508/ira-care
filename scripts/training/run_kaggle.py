#!/usr/bin/env python3
"""Push a training script to Kaggle's free GPU as a private kernel, wait, and read
the results back from the kernel log.

    python scripts/training/run_kaggle.py fall           # scripts/training/kaggle_fall_model.py
    python scripts/training/run_kaggle.py fall --fetch   # read the log of the last run only

The kernel is private, GPU on, internet on, and carries no token: it pulls only
public data. Results come back as lines in the log (`METRICS {json}`,
`TFLITE_B64 ...`, `KAGGLE_MODEL ...`) because output-file downloads go through
a host this build machine cannot reach. The metrics JSON, the model file and the
GPU time used are written under docs/evidence/training/.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EVIDENCE = ROOT / "docs" / "evidence" / "training"

JOBS = {
    "fall": {"code": "kaggle_fall_model.py", "slug": "ira-care-fall-model-training", "title": "ira-care-fall-model-training",
             "artefact": ("TFLITE_B64", "ira_fall_model.tflite")},
    "classifier": {"code": "kaggle_redflag_classifier.py", "slug": "ira-care-redflag-classifier", "title": "ira-care-redflag-classifier",
                   "artefact": ("HEADS_PART", "classifier_heads.json"), "datasets": ["ira-care-training-data"]},
    "conversation": {"code": "kaggle_conversation_qlora.py", "slug": "ira-care-conversation-qlora", "title": "ira-care-conversation-qlora",
                     "artefact": None},
    # Round 4: how fast the open models answer on Kaggle's free GPU (docs/MODEL_CHOICE.md).
    "gpu_bench": {"code": "kaggle_ollama_bench.py", "slug": "ira-care-gpu-bench", "title": "ira-care-gpu-bench", "artefact": None},
    # The GPU profile (D-074): the real evaluation, through the backend's own code, on Kaggle's GPU.
    # The code goes up first as a private dataset (upload_code); the reports come back in parts.
    "gpu_eval": {"code": "kaggle_gpu_eval.py", "slug": "ira-care-gpu-eval", "title": "ira-care-gpu-eval",
                 "artefact": ("EVAL_PART", "gpu_eval_reports.json"), "datasets": ["ira-care-code"], "upload_code": True},
    # F2.e-g: QLoRA on the versioned care-conversation dataset, judged against the base model (FINETUNE_CARD.md).
    # Its own code dataset and its own evidence file, so neither the GPU evaluation nor F2.c's record is touched.
    "conversation_finetune": {"code": "kaggle_conversation_finetune.py", "slug": "ira-care-conversation-finetune",
                              "title": "ira-care-conversation-finetune", "datasets": ["ira-care-finetune-code"],
                              "upload_code": "finetune", "evidence": "conversation_finetune_2026-11-25.json",
                              "artefact": ("EVAL_PART", "conversation_finetune_2026-11-25_results.json")},
}

CODE_DATASET = "ira-care-code"


def upload_finetune_code(workdir: Path) -> None:
    """The fine-tune's inputs, laid out as in the repository: the product's conversation code and its
    evaluation set, the dataset builder and the versioned dataset. A private dataset of its own."""
    import subprocess
    import tarfile
    user = os.environ["KAGGLE_USERNAME"]
    ira = ROOT / "supracloud-jarvis" / "ira"
    workdir.mkdir(parents=True, exist_ok=True)
    keep = lambda info: None if "__pycache__" in info.name or info.name.endswith(".pyc") else info
    with tarfile.open(workdir / "ira-finetune.tar.gz", "w:gz") as tar:
        for child in sorted(ira.iterdir()):
            if child.name not in ("tests", ".venv", "node_modules"):
                tar.add(child, arcname=f"supracloud-jarvis/ira/{child.name}", filter=keep)
        for rel in ("tests/__init__.py", "tests/care/__init__.py", "tests/care/eval"):
            tar.add(ira / rel, arcname=f"supracloud-jarvis/ira/{rel}", filter=keep)
        tar.add(HERE / "conversation_dataset.py", arcname="scripts/training/conversation_dataset.py")
        tar.add(ROOT / "docs" / "ai" / "conversation" / "dataset", arcname="docs/ai/conversation/dataset", filter=keep)
    name = "ira-care-finetune-code"
    meta = {"title": name, "id": f"{user}/{name}", "licenses": [{"name": "other"}], "isPrivate": True}
    (workdir / "dataset-metadata.json").write_text(json.dumps(meta, indent=2))
    out = subprocess.run(["kaggle", "datasets", "version", "-p", str(workdir), "-m", "fine-tune inputs"], capture_output=True, text=True, check=False)
    if out.returncode != 0 or "not found" in (out.stdout + out.stderr).lower():
        out = subprocess.run(["kaggle", "datasets", "create", "-p", str(workdir)], capture_output=True, text=True, check=False)
    print((out.stdout or out.stderr).strip())
    if out.returncode != 0:
        raise SystemExit(out.returncode)
    time.sleep(90)


def upload_code(workdir: Path) -> None:
    """Put the backend's code (supracloud-jarvis/ira without its tests, plus the evaluation set)
    on Kaggle as a private dataset, as a new version when it already exists. Nothing secret is
    in it: the same files are in this repository, which the secret scan checks."""
    import subprocess
    import tarfile
    user = os.environ["KAGGLE_USERNAME"]
    ira = ROOT / "supracloud-jarvis" / "ira"
    workdir.mkdir(parents=True, exist_ok=True)
    keep = lambda info: None if "__pycache__" in info.name or info.name.endswith(".pyc") else info
    with tarfile.open(workdir / "ira-code.tar.gz", "w:gz") as tar:
        for child in sorted(ira.iterdir()):
            if child.name not in ("tests", ".venv", "node_modules"):
                tar.add(child, arcname=f"ira/{child.name}", filter=keep)
        for rel in ("tests/__init__.py", "tests/care/__init__.py", "tests/care/eval"):
            tar.add(ira / rel, arcname=f"ira/{rel}", filter=keep)
    meta = {"title": CODE_DATASET, "id": f"{user}/{CODE_DATASET}", "licenses": [{"name": "other"}]}
    (workdir / "dataset-metadata.json").write_text(json.dumps(meta, indent=2))
    out = subprocess.run(["kaggle", "datasets", "version", "-p", str(workdir), "-m", "code for the GPU evaluation"], capture_output=True, text=True, check=False)
    if out.returncode != 0 or "not found" in (out.stdout + out.stderr).lower():
        out = subprocess.run(["kaggle", "datasets", "create", "-p", str(workdir)], capture_output=True, text=True, check=False)
    print((out.stdout or out.stderr).strip())
    if out.returncode != 0:
        raise SystemExit(out.returncode)
    time.sleep(60)   # Kaggle processes a new version before a kernel can attach it


def kaggle_api():
    from kaggle.api.kaggle_api_extended import KaggleApi
    api = KaggleApi()
    api.authenticate()
    return api


def push(job: dict, workdir: Path) -> None:
    import shutil
    import subprocess
    user = os.environ["KAGGLE_USERNAME"]
    workdir.mkdir(parents=True, exist_ok=True)
    shutil.copy(HERE / job["code"], workdir / job["code"])
    for extra in job.get("extra_files", []):
        shutil.copy(HERE / extra, workdir / extra)
    meta = {"id": f"{user}/{job['slug']}", "title": job["title"], "code_file": job["code"], "language": "python",
            "kernel_type": "script", "is_private": True, "enable_gpu": True, "enable_internet": True,
            "dataset_sources": [f"{user}/{d}" for d in job.get("datasets", [])],
            "competition_sources": [], "kernel_sources": [], "model_sources": []}
    (workdir / "kernel-metadata.json").write_text(json.dumps(meta, indent=2))
    out = subprocess.run(["kaggle", "kernels", "push", "-p", str(workdir)], capture_output=True, text=True)
    print(out.stdout.strip() or out.stderr.strip())
    if out.returncode != 0:
        raise SystemExit(out.returncode)


def status(api, slug: str) -> str:
    r = api.kernels_status(f"{os.environ['KAGGLE_USERNAME']}/{slug}")
    return str(getattr(r, "status", r)).split(".")[-1].strip('"').lower()


def read_log(api, slug: str) -> tuple[list[str], list[str]]:
    from kagglesdk.kernels.types.kernels_api_service import (
        ApiListKernelSessionOutputRequest,
    )
    with api.build_kaggle_client() as k:
        req = ApiListKernelSessionOutputRequest()
        req.user_name = os.environ["KAGGLE_USERNAME"]
        req.kernel_slug = slug
        resp = k.kernels.kernels_api_client.list_kernel_session_output(req)
    files = [f.file_name for f in (resp.files or [])]
    try:
        entries = json.loads(resp.log or "[]")
        lines = [e.get("data", "").rstrip("\n") for e in entries if e.get("stream_name") == "stdout"]
    except ValueError:
        lines = (resp.log or "").splitlines()
    return files, lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("job", choices=sorted(JOBS))
    ap.add_argument("--fetch", action="store_true", help="read the log of the last run; do not push")
    ap.add_argument("--wait-minutes", type=int, default=120)
    args = ap.parse_args()
    job = JOBS[args.job]
    api = kaggle_api()
    started = dt.datetime.now(dt.UTC)
    if not args.fetch:
        if job.get("upload_code") == "finetune":
            upload_finetune_code(ROOT / ".kaggle-runs" / "finetune-code")
        elif job.get("upload_code"):
            upload_code(ROOT / ".kaggle-runs" / "code")
        push(job, ROOT / ".kaggle-runs" / args.job)
        time.sleep(20)
        deadline = time.time() + args.wait_minutes * 60
        while time.time() < deadline:
            s = status(api, job["slug"])
            print(f"{dt.datetime.now(dt.UTC):%H:%M:%S} {s}", flush=True)
            if s in ("complete", "error", "cancelacknowledged"):
                break
            time.sleep(60)
    files, lines = read_log(api, job["slug"])
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    metrics = None
    parts: dict[int, str] = {}
    for line in lines:
        if line.startswith("METRICS "):
            metrics = json.loads(line[len("METRICS "):])
        if job["artefact"] and line.startswith(job["artefact"][0] + " "):
            payload = line.split(" ", 1)[1]
            if job["artefact"][0].endswith("_PART"):
                index, rest = payload.split(" ", 1)
                parts[int(index.split("/")[0])] = rest
                continue
            data = payload.encode() if job["artefact"][1].endswith(".json") else base64.b64decode(payload)
            (EVIDENCE / job["artefact"][1]).write_bytes(data)
            print(f"wrote {EVIDENCE / job['artefact'][1]}")
    if parts:
        blob = "".join(parts[i] for i in sorted(parts))
        json.loads(blob)   # must be whole
        (EVIDENCE / job["artefact"][1]).write_text(blob + "\n")
        print(f"wrote {EVIDENCE / job['artefact'][1]} from {len(parts)} parts")
    run = {"job": args.job, "kernel": f"{os.environ['KAGGLE_USERNAME']}/{job['slug']}", "status": status(api, job["slug"]),
           "pushed_at": started.isoformat(timespec="seconds"), "fetched_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
           "output_files": files, "gpu_seconds_reported_by_script": (metrics or {}).get("seconds"), "metrics": metrics,
           "log_tail": [ln for ln in lines if not ln.startswith(("TFLITE_B64", "HEADS", "EVAL_PART"))][-40:]}
    (EVIDENCE / job.get("evidence", f"{args.job}.json")).write_text(json.dumps(run, indent=2) + "\n")
    print(json.dumps({k: v for k, v in run.items() if k not in ("metrics", "log_tail")}, indent=2))
    if metrics:
        print(json.dumps(metrics, indent=2)[:3000])
    return 0 if metrics else 1


if __name__ == "__main__":
    sys.exit(main())
