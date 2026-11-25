"""Speech-recognition accuracy harness: word error rate per group (requirement F8.b).

    # transcribe with the product's own server recogniser, then score
    python3 scripts/eval/speech_wer.py transcribe MANIFEST --out RESULTS.json [--model small]
    # score transcripts that already exist (a watch's or a phone's platform recogniser)
    python3 scripts/eval/speech_wer.py score MANIFEST --out RESULTS.json

MANIFEST is JSON lines: {"id", "reference", "group", "audio"?, "hypothesis"?, "speaker"?}.
`transcribe` runs `voice/stt.py`'s own path: the pinned faster-whisper checkpoint
(`core/models/artefacts.py`), the same decoder, beam size, voice-activity filter and automatic
language detection the product uses.

The product decodes WAV only (voice/audio_decode.py; PyAV does not ship). LibriSpeech is FLAC,
so this harness, and only this harness, converts any non-WAV file to 16-bit PCM WAV with
`soundfile` (libsndfile) first, then hands the WAV to the product decoder. LibriSpeech is
16-bit, so the conversion is lossless. `soundfile` is not on the product's decode path. `score` runs nothing: it only needs the hypotheses, so
device runs (which cannot happen on this machine) are scored the same way later.

Scoring is `voice/wer.py`: pooled corpus WER per group, with its normalisation stated there.
The results file records the model, its pinned revision, the machine, and every utterance.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "supracloud-jarvis" / "ira"))

from voice.wer import by_group, corpus, score  # noqa: E402


def _rows(manifest: str) -> list[dict]:
    with open(manifest, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _as_wav(blob: bytes) -> tuple[bytes, str]:
    """The file as WAV, and how it became WAV. Evaluation-only: see the module docstring."""
    if blob[:4] in (b"RIFF", b"RF64") and blob[8:12] == b"WAVE":
        return blob, "none (already WAV)"
    import io

    import soundfile as sf
    samples, rate = sf.read(io.BytesIO(blob), dtype="int16", always_2d=True)
    out = io.BytesIO()
    sf.write(out, samples, rate, format="WAV", subtype="PCM_16")
    return out.getvalue(), f"soundfile {sf.__version__} (libsndfile {sf.__libsndfile_version__}) to 16-bit PCM WAV"


def _peak_rss_mib() -> float | None:
    try:
        import resource
    except ImportError:          # not on Windows
        return None
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def transcribe(rows: list[dict], model_name: str) -> dict:
    from voice import stt

    repository, revision = stt._whisper_source(model_name)
    model = stt._load_model(model_name, "cpu", "int8")
    seconds, decode_seconds, converted = [], [], set()
    for r in rows:
        wav, how = _as_wav(Path(r["audio"]).read_bytes())
        converted.add(how)
        t0 = time.perf_counter()
        audio = stt._decode_to_16k_mono(wav)
        decode_seconds.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        text, lang, _ = stt._transcribe_sync(audio, model, None)
        seconds.append(time.perf_counter() - t0)
        r["hypothesis"], r["detected_language"] = text, lang
        r["audio_seconds"] = round(len(audio) / 16000, 2)
    return {"recogniser": "faster-whisper (voice/stt.py, product settings)", "model": model_name,
            "repository": repository, "revision": revision,
            "compute": "cpu int8", "median_seconds_per_utterance": sorted(seconds)[len(seconds) // 2] if seconds else None,
            "total_audio_seconds": round(sum(r["audio_seconds"] for r in rows), 1),
            "decoder": "voice/audio_decode.py (WAV; no PyAV)",
            "evaluation_conversion": sorted(converted),
            "median_decode_seconds": sorted(decode_seconds)[len(decode_seconds) // 2] if decode_seconds else None,
            "peak_rss_mib": _peak_rss_mib()}


def results(rows: list[dict], about: dict) -> dict:
    groups = by_group((r["group"], r["reference"], r.get("hypothesis", "")) for r in rows)
    total = corpus((r["reference"], r.get("hypothesis", "")) for r in rows)

    def as_dict(c):
        return {"words": c.words, "substitutions": c.substitutions, "deletions": c.deletions,
                "insertions": c.insertions, "wer": None if c.wer is None else round(c.wer, 4)}
    return {
        "about": about,
        "machine": f"{platform.machine()}, {platform.system()} {platform.release()}",
        "utterances": len(rows),
        "speakers": len({r.get("speaker") for r in rows}),
        "overall": as_dict(total),
        "groups": {g: as_dict(c) for g, c in sorted(groups.items())},
        "per_utterance": [{"id": r["id"], "group": r["group"], "reference": r["reference"],
                           "hypothesis": r.get("hypothesis", ""),
                           "errors": score(r["reference"], r.get("hypothesis", "")).errors}
                          for r in rows],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("transcribe", "score"))
    ap.add_argument("manifest")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="small")
    a = ap.parse_args()
    rows = _rows(a.manifest)
    if a.mode == "transcribe":
        about = transcribe(rows, a.model)
    else:
        missing = [r["id"] for r in rows if "hypothesis" not in r]
        if missing:
            print(f"refused: {len(missing)} rows have no hypothesis (first: {missing[0]})", file=sys.stderr)
            return 2
        about = {"recogniser": "external transcripts (score mode)"}
    out = results(rows, about)
    Path(a.out).write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("utterances", "speakers", "overall", "groups")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
