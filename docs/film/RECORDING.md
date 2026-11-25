# How the film was recorded (25 November 2026)

`docs/film/ira_care.mp4` was re-recorded on the remediated stack: `main` at `c6e31dd`, after PRs
#63 to #90. Everything in it is a real screen from a stack brought up from a clean clone of that
commit, with the made-up people of the demonstration. Nothing was sent outside the machine: the
message providers were log-only and nothing dials emergency services.

## The run

| Step | What ran | Result |
|---|---|---|
| Stack | `scripts/staging/clean_clone_proof.sh --keep --skip-e2e`: PostgreSQL and Redis containers, the migration chain as `ira_migrator`, the care server and worker, the dashboard (standalone build, with the demonstration pages switched on for the recording), the phone app exported for the web, the demo organisation seeded | passed in 190 s |
| Model | Ollama 0.40.18 serving `gemma4:e4b` (digest `dc35e8d9c606`) then `gemma4:e2b` (`b37049369adf`), on the same 4-core machine, no GPU | as deployed (`IRA_CARE_MODEL_PRIMARY`, `_SECONDARY`) |
| Scenes 3 to 9 | `scripts/film/record_scenes.mjs` with Playwright 1.63.0 and its Chromium, Node 22.23.5 | all seven recorded live |
| Film | `scripts/film/make_film.py` with ffmpeg 6.1.1 (libass) and Piper | 1920 × 1080, 30 fps, H.264 and AAC, 356.3 s, 18,295,427 bytes (17.4 MB, under the 60 MB limit), SHA-256 `e15ea416…ba5af2` |

Checked after the build: `ffprobe` reports the streams above, the audio has speech throughout
(mean −17.8 dB), and frames sampled at 3, 40, 75, 130, 200, 290 and 340 s show the title card,
the watch with IRA's captioned words, Sravani's phone, the dashboard (with the care plan, caseload,
visit plan and search added since round 4) and the hardware renders.

## The voice

The voice-over and IRA's spoken lines are Piper with the `en_GB-cori-high` voice, downloaded from
`huggingface.co/rhasspy/piper-voices` (model file SHA-256 begins `470b4dd634c98f8a`). Its model card
gives the training data as LibriVox recordings, **public domain**. The Piper command used was the
`piper-tts` 1.8.0 Python package, which is licensed **GPL-3.0-or-later** (the earlier Piper releases
round 4 used were MIT). It is a tool run once on the build machine to make the audio; nothing from it
is in the product, and no voice model is in this repository.

## What changed since round 4

- **Narration** (`NARRATION.md`), only where the product changed, every sentence checked against the
  merged code:
  - scene 3: the check-in question is written by the open model when it answers in time, and is
    one of IRA's set questions when it does not (this recording's is a set question);
  - scene 6: care plans kept as versions, search that never covers what anyone said (#74, #85), and
    the record that does not say all well when the system cannot vouch for it (#70);
  - scene 7: the watch shows the carer's arrival at its next sync, about 40 s after it was recorded,
    so the narration now says so at that moment ("within a minute");
  - scene 9: the demonstration pages are off in production builds (#67); the 22-step care journey
    runs as one automated test (D-079);
  - scene 10: the watch's fall model is trained only on openly licensed data and is not yet measured
    on older people or on this watch (D-080);
  - scene 11: "IRA CARE is not a medical device and makes no diagnosis", the product's own claim
    (`docs/compliance/INTENDED_PURPOSE.md`).
- **Every recorded scene** is new: the screens are the current product.

## Found while recording

The first take of scene 3 ran while the machine was busy with a full test run, and the open model
missed its time budget for Ada's talk: IRA answered with its fixed line, "I'm here with you, Ada.
Shall I let Praveen Kamineti know you'd like a word?" (`FALLBACK_LINE`, `care/conversation.py`).
That is the product behaving as designed, but the narration says the reply is the model's own, so
scene 3 was recorded again with the machine quiet and the model loaded, and the film uses that take:
the instant acknowledgement, then `gemma4:e4b`'s own words 9 s after Ada spoke.

## Not in this recording

No physical watch or phone; no real messages; no older people. Model speed is this 4-core,
no-GPU machine's, and is slower than a deployment with a GPU would be.
