"""Build a held-out evaluation manifest from LibriSpeech test-clean (CC BY 4.0).

    python3 -I scripts/eval/librispeech_manifest.py PARQUET OUT_DIR [--per-speaker N]

PARQUET is `all/test.clean/0000.parquet` from the Hugging Face copy `openslr/librispeech_asr`
(the original host, openslr.org, was unreachable from the build machine). Writes each chosen
utterance's FLAC to OUT_DIR and a manifest.jsonl with id, audio, reference, group and speaker.
The selection is deterministic: the first N utterances of every speaker, by id. Run with -I:
it reads a downloaded file. Nothing it writes goes into the repository.

LibriSpeech gives no speaker ages, so every row is in one group, "librispeech-test-clean".
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("parquet")
    ap.add_argument("out_dir")
    ap.add_argument("--per-speaker", type=int, default=4)
    a = ap.parse_args()
    import pyarrow.parquet as pq

    rows = sorted(pq.read_table(a.parquet).to_pylist(), key=lambda r: r["id"])
    by_speaker = defaultdict(list)
    for r in rows:
        if len(by_speaker[r["speaker_id"]]) < a.per_speaker:
            by_speaker[r["speaker_id"]].append(r)
    out = Path(a.out_dir)
    (out / "audio").mkdir(parents=True, exist_ok=True)
    n = 0
    with open(out / "manifest.jsonl", "w", encoding="utf-8") as f:
        for speaker in sorted(by_speaker):
            for r in by_speaker[speaker]:
                path = out / "audio" / f"{r['id']}.flac"
                path.write_bytes(r["audio"]["bytes"])
                f.write(json.dumps({"id": r["id"], "audio": str(path), "reference": r["text"],
                                    "group": "librispeech-test-clean", "speaker": str(speaker)}) + "\n")
                n += 1
    print(f"{n} utterances from {len(by_speaker)} speakers -> {out / 'manifest.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
