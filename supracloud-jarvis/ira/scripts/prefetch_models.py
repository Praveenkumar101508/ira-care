#!/usr/bin/env python3
"""Download the memory models at setup, at their pinned revisions.

    python scripts/prefetch_models.py --embedder BAAI/bge-large-en-v1.5
    python scripts/prefetch_models.py --embedder NAME --reranker NAME

Setup used to do this with an inline `SentenceTransformer('<name>')`, which
fetched whatever the Hub served at `main` (MOD-02), and on Linux and macOS
pasted the `.env` value into the Python source. This takes the names as
arguments and asks `core/models/artefacts.py` for each revision, the same
answer the app's own loaders get, so the bytes setup caches are the bytes the
app will load. A name the pin table does not record is refused here too.

Exit status: 0 when every model asked for is cached, 1 otherwise. Setup treats
a failure as a warning: the model then downloads, pinned, on first use.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models.artefacts import UnpinnedArtefact, hub_revision_or_local  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--embedder", help="EMBEDDING_MODEL, a Hub id or a local folder")
    parser.add_argument("--reranker", help="RERANKER_MODEL, a Hub id or a local folder")
    args = parser.parse_args(argv)
    if not (args.embedder or args.reranker):
        parser.error("name at least one of --embedder, --reranker")

    from sentence_transformers import CrossEncoder, SentenceTransformer

    failed = False
    for kind, name in (("embedder", args.embedder), ("reranker", args.reranker)):
        if not name:
            continue
        try:
            revision = hub_revision_or_local(name)
            if kind == "embedder":
                SentenceTransformer(name, device="cpu", revision=revision)
            else:
                CrossEncoder(name, device="cpu", revision=revision)
        except UnpinnedArtefact as exc:
            print(f"{kind} {name}: refused: {exc}", file=sys.stderr)
            failed = True
            continue
        except Exception as exc:  # noqa: BLE001 — setup reports it and carries on
            print(f"{kind} {name}: not cached: {type(exc).__name__}: {exc}", file=sys.stderr)
            failed = True
            continue
        print(f"{kind} {name}: cached at {revision or 'the local folder'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
