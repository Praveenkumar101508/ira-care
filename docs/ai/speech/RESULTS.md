# Speech recognition accuracy (requirements F8.b, F8.c)

## What is measured

The word error rate (WER) of the recogniser the server runs (`voice/stt.py`): faster-whisper
`small`, pinned to `Systran/faster-whisper-small` at revision
`536b0662742c02347bc0e980a01041f333bce120`, CPU int8, with the product's own decoding settings
(beam 5, voice-activity filter, automatic language detection). The harness is
`scripts/eval/speech_wer.py`; the scoring is `voice/wer.py` (pooled corpus WER over a
minimum-edit word alignment, with the normalisation stated in that module); its tests are
`tests/test_speech_wer.py`.

The watch and the phone use their platforms' own recognisers, which cannot run on the build
machine. The harness has a `score` mode that scores transcripts from any recogniser without
running it, so device runs are scored the same way when there are devices.

## Result: LibriSpeech test-clean, 25 November 2026

| | Value |
|---|---|
| Utterances | 160 (the first 4 of each of the 40 test-clean speakers, by id) |
| Audio | 1,477.9 s |
| Reference words | 3,820 |
| Substitutions / deletions / insertions | 135 / 19 / 8 |
| **WER** | **4.24 %** |
| Median time per utterance | 3.9 s (4-CPU shared container, CPU only) |
| Utterances with no error | 77 of 160 |

Every utterance, its reference and the recogniser's words are in `results.json`.

## What this does and does not show

- **Not older speakers.** LibriSpeech is read audiobook speech from adult volunteers and records
  no ages. This is a baseline for the recogniser and the harness, not a measurement on the people
  IRA CARE is for, who often speak more slowly, more quietly, with dentures or with conditions
  that change speech. Nothing here validates recognition on older adults.
- **Read speech, a quiet room, good microphones.** A watch on a wrist in a kitchen is not that.
- **Normalisation errors are counted.** Some errors are spelling, not hearing: "down stairs"
  against "downstairs", "Saint" against "St.", the text's "consid'ble" against "considerable".
  The normalisation does not paper over them (`voice/wer.py`), so the figure is, if anything,
  pessimistic.
- **Possible overlap with Whisper's training data.** OpenAI has not published Whisper's
  training set; LibriSpeech may be in it. A figure on unseen speech could be higher.
- **The check-in answers.** The safety of a check-in does not rest on recognition being right:
  an unclear answer is read as unclear and never as OK (requirement C-1; `test_care_safety_answers`).

## Older speakers (F8.c): blocked

Mozilla Common Voice (CC0) records self-reported age bands including sixties, seventies and
eighties, and would allow WER per age band with this harness unchanged (set `group` to the
age band in the manifest). Since October 2025 it is available only from the Mozilla Data
Collective, which needs an account that accepts its terms; that is the owner's decision, and
the site was unreachable from the build machine in any case. No other older-speaker corpus with
a licence that allows this use was found. F8.c stays blocked until the owner obtains access.

## Reproduce

```
python3 -I scripts/eval/librispeech_manifest.py test_clean_0000.parquet OUT --per-speaker 4
python3 scripts/eval/speech_wer.py transcribe OUT/manifest.jsonl --out results.json
```

`test_clean_0000.parquet` is `all/test.clean/0000.parquet` from the Hugging Face dataset
`openslr/librispeech_asr` (CC BY 4.0). Data and model weights stay outside the repository.
