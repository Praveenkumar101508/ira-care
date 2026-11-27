# Older-adult speech evaluation: the plan (requirement F8.c, round 9)

F8.c asks for the recogniser's word error rate measured on a public older-adult speech set with a
usable licence. It is blocked on access, not on code: the only public set found that records
speakers' ages under a licence IRA CARE can use is Mozilla Common Voice, now distributed through
the Mozilla Data Collective, and downloading it means accepting its terms with an account. That is
the owner's decision; nothing here accepts terms, downloads a dataset or uploads audio. This page
is what to do once access exists, so the run is mechanical.

## What will be measured

The same recogniser and settings as the LibriSpeech result in `RESULTS.md`: `voice/stt.py`,
faster-whisper `small` at the pinned revision, CPU int8, the product's decoding settings, scored by
`voice/wer.py`. The one addition is the breakdown by age:

```bash
python3 scripts/eval/speech_wer.py transcribe MANIFEST --out docs/ai/speech/results_common_voice.json --by-age
```

`--by-age` adds, beside the overall and per-group figures:

- `age_buckets`: pooled WER for under 60, 60 to 69, 70 to 79, 80 and over, and unknown;
- `older_vs_younger`: 60 and over against under 60, the comparison the requirement is about;
- each utterance's `age_bucket`.

A row's `age` is a number of years or Common Voice's own label (`sixties`, `seventies`,
`eighties`, `nineties`; `fifties` and below fall under 60). A missing or unreadable age is
`unknown` and is never guessed from the voice. The buckets partition the corpus, so their words
and errors add up to the overall figure; `tests/readiness/test_speech_wer_age_buckets.py` holds the
harness to that.

## The manifest

One JSON line per clip, built from Common Voice's `validated.tsv` (English):

```json
{"id": "<clip file name>", "audio": "<local path to the clip>", "reference": "<sentence>",
 "group": "common-voice-en", "speaker": "<client_id>", "age": "<age column>"}
```

Selection, fixed before any result is seen:

1. Validated clips only (`validated.tsv`), English.
2. Every speaker whose age label is `sixties` or older, with at most 10 clips per speaker, so a few
   prolific speakers cannot dominate the figure.
3. An under-60 comparison set drawn the same way, matched in number of speakers where the data
   allows; the selection seed is written into the results file.
4. Clips are MP3. The harness converts non-WAV audio to 16-bit PCM WAV for evaluation only (the
   module docstring says how); the product's decode path is unchanged.

## What the result will and will not say

- Common Voice ages are self-reported and optional, and older speakers are a minority of it. The
  results file records the number of speakers in each bucket; a bucket with fewer than 20 speakers
  is reported as too small to compare.
- Common Voice is read speech recorded on the speaker's own microphone. It is not conversational
  speech, nor speech at a wrist, nor speech from people with the conditions IRA CARE's users may
  have. F8.d (consented recordings of older people) stays the measure of that.
- The watch and the phone use their platforms' recognisers. Their transcripts of the same clips
  can be scored later with `score` mode and the same `--by-age` breakdown.

## Acceptance

F8.c is accepted when the results file exists for this selection, the overall figure and both
`older_vs_younger` figures are reported in `RESULTS.md` with the speaker counts, and the gap
between them is stated as it is, however large. No threshold is set in advance for passing, since
no product claim depends on one; the figure informs the speech roadmap (D-083 and the review pack
`docs/ai/review/REVIEW_PACK_F8a_SPEECH_LICENCES.md`).

## Owner action

Decide whether to create the Mozilla Data Collective account and accept the Common Voice terms. If
yes, download the English release onto your own machine (not into this repository), build the
manifest as above, and run the command. Raw clips never enter the repository; only the results file
does, which holds sentences and transcripts, not audio.
