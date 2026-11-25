#!/usr/bin/env python3
"""Download the classifier's public text encoder for the server (CPU, ONNX).

    python scripts/training/fetch_encoder.py [--dest supracloud-jarvis/ira/care/models/encoder]

Fetches sentence-transformers/all-MiniLM-L6-v2 (Apache-2.0) from Hugging Face:
the ONNX graph and the tokenizer. No token is needed; the files are public. The
destination is ignored by git (about 90 MB); the deployment image fetches them
at build time (deploy/Dockerfile.api).
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

REPO = "sentence-transformers/all-MiniLM-L6-v2"
FILES = {"onnx/model.onnx": "model.onnx", "tokenizer.json": "tokenizer.json", "config.json": "config.json"}


def default_dest() -> Path:
    return Path(__file__).resolve().parents[2] / "supracloud-jarvis" / "ira" / "care" / "models" / "encoder"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dest", default=None, help="default: the repository's care/models/encoder")
    args = ap.parse_args()
    # The default is worked out only when no --dest is given: the deployment image runs this
    # file from /tmp, where there is no repository around it (deploy/Dockerfile.api).
    dest = Path(args.dest) if args.dest else default_dest()
    dest.mkdir(parents=True, exist_ok=True)
    for remote, local in FILES.items():
        target = dest / local
        if target.exists() and target.stat().st_size > 0:
            print(f"have {target}")
            continue
        url = f"https://huggingface.co/{REPO}/resolve/main/{remote}"
        print(f"fetching {url}")
        urllib.request.urlretrieve(url, target)
    (dest / "SOURCE.txt").write_text(f"{REPO} (Apache-2.0), fetched by scripts/training/fetch_encoder.py\n")
    print(f"encoder ready in {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
