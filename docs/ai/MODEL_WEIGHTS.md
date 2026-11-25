# Model weights IRA CARE ships or downloads

Completion register item F1. Every model whose weights the product ships, or downloads when it
is installed, with the licence read on 25 November 2026 at the model's own card or registry
entry. The datasets those models were trained or evaluated on are in
[`docs/DATASETS.md`](../DATASETS.md), with the same columns.

Found by searching the repository for Hugging Face identifiers, `.onnx`, `.tflite`, Ollama
tags, `model_id` and the speech and embedding settings (`deploy/`, `scripts/training/`,
`watch/`, `mobile/`, `supracloud-jarvis/ira/core/models/artefacts.py`,
`supracloud-jarvis/.env.example`). No weights were downloaded to check them: the Hugging Face
model API and model cards, and the Ollama registry's manifest and licence layer (a small text
file), were read instead.

How the cells are read (checked by
`supracloud-jarvis/ira/tests/readiness/test_dataset_register.py`):

- **Used for** starts with `shipped:` when the IRA CARE product carries the weights or fetches
  them on install; such a row may not say `no` under **Commercial use**.
- **Commercial use**: `yes`, `no` or `unclear`, as in `docs/DATASETS.md`.
- No model from the Llama family is named anywhere in the repository, so no Llama community
  licence applies. The Gemma 4 models are under Apache 2.0, not under the Gemma Terms of Use
  and Prohibited Use Policy that governed earlier Gemma generations (details in the table).

## In the IRA CARE product

| Model | Source and revision | Used for | Licence | Commercial use | Conditions and use restrictions | Verified (2026-11-25) | Attribution |
| --- | --- | --- | --- | --- | --- | --- | --- |
| IRA fall model v3 (`fall_model.tflite`, 6,705 parameters) | Our own: trained by `scripts/training/fall_model_v3.py` (deterministic: two runs byte-identical); the shipped file `watch/app/src/main/assets/fall_model.tflite`, SHA-256 `2cab7c9bcbf60de51d7c42f5690ad549bcbf4f13eb6266c35bf5d0a59b063278`, pinned in `FallModelReleaseTest` | shipped model: on the watch in every build, release and debug, "recover" mode beside the rule detector (D-080, D-082) | Our own weights (no pretrained start). The training data's terms carry over: BITS-2 (CC BY 4.0) and UCI HAR (CC BY 4.0) | yes | Both datasets require credit; the product's licence notices must cite them | Both licences read at their source on 2026-11-25 (`docs/DATASETS.md`); the file hash checked by `FallModelReleaseTest` | Credit BITS-2 (P. Nandi and K.R. Anupama, doi:10.5281/zenodo.10013090) and UCI HAR in the product's notices |
| `gemma4:e4b` (CPU primary; GPU secondary) | Ollama library `gemma4:e4b`, from `google/gemma-4-E4B-it` (Hugging Face revision `4ecc2ba63bcac23ba4a767fc87025b1450f84c9c`) | shipped: pulled by `ollama-pull` in `deploy/docker-compose.yml` and `deploy/compose.gpu.yml` | Apache License 2.0 | yes | Apache 2.0: keep the licence and any NOTICE with redistributed weights; state changes. No use-based restrictions in the licence text. Google's page that the card links to, <https://ai.google.dev/gemma/docs/gemma_4_license>, **not verified: source unreachable from the build environment** | Hugging Face API and card read, <https://huggingface.co/google/gemma-4-E4B-it>: `license: apache-2.0`. Ollama registry licence layer for `gemma4:e4b` (`sha256:7339fa41...`) read: Apache License 2.0 text | Google DeepMind, Gemma 4 |
| `gemma4:e2b` (CPU secondary) | Ollama library `gemma4:e2b`, from `google/gemma-4-E2B-it` (revision `4adf15748de9e1a0097b6274cee9c15836c41c37`) | shipped: pulled by `ollama-pull` in `deploy/docker-compose.yml` | Apache License 2.0 | yes | As above | Hugging Face API read, <https://huggingface.co/google/gemma-4-E2B-it>: `license: apache-2.0`. Ollama licence layer for `gemma4:e2b` (same `sha256:7339fa41...`) read: Apache 2.0 | Google DeepMind, Gemma 4 |
| `gemma4:12b` (GPU primary) | Ollama library `gemma4:12b`, from `google/gemma-4-12B-it` (revision `43c9d014b231474fa7212dbb48a762566c2b255d`) | shipped: pulled by `ollama-pull` in `deploy/compose.gpu.yml` | Apache License 2.0 | yes | As above | Hugging Face API read, <https://huggingface.co/google/gemma-4-12B-it>: `license: apache-2.0`. Ollama licence layer for `gemma4:12b` (`sha256:0d542e0c...`) read: Apache 2.0 | Google DeepMind, Gemma 4 |
| `sentence-transformers/all-MiniLM-L6-v2` (ONNX encoder for the answer classifier) | Hugging Face, fetched at image build by `scripts/training/fetch_encoder.py` into `deploy/Dockerfile.api` (current revision on the Hub `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`; the script fetches `main`) | shipped: in the server image; the classifier is off by default (`IRA_CARE_CLASSIFIER=off`) | Apache License 2.0 | yes | Apache 2.0 notice. Base model `nreimers/MiniLM-L6-H384-uncased` is MIT (Hugging Face API). The card says it was fine-tuned on about 1 billion sentence pairs from many public sets; their individual terms are not restated by the card | Hugging Face API and card read, <https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2>: `license: apache-2.0` | sentence-transformers (N. Reimers and contributors) |
| Answer-classifier heads (`classifier_heads.json`) | Our own: `scripts/training/kaggle_redflag_classifier.py`, on 1,120 synthetic answers from the product's own rules (`scripts/training/classifier_data.jsonl`) | shipped: in the server image, off by default | Our own weights | yes | None beyond the encoder's Apache 2.0 | Training data is generated in the repository by `scripts/training/make_classifier_data.py` | — |
| IRA Care Heart selector (`heart_selector.json`, 200 trees, 12,200 nodes, 467,177 bytes, SHA-256 `3b0863a9a8cf81f906f5475fe54cc3878a38fbe7f7b1cc88b3c851d85f1c35dc`) | Our own: `scripts/training/heart_ppg_dalia.py --model-out`, a gradient-boosted tree classifier exported to plain JSON (`ira-gbt-v1`, no pickle), read by `supracloud-jarvis/ira/care/heart_learned.py` | shipped: in the server image; not called by any alert path (wellbeing trend only) | Our own weights (no pretrained start). The training data's terms carry over: PPG-DaLiA, CC BY 4.0 | yes | CC BY 4.0 requires credit to PPG-DaLiA and its authors (citation in `docs/DATASETS.md`). Not a medical device; not validated on older adults or on the IRA watch (`docs/ai/heart/MODEL_CARD.md`) | PPG-DaLiA licence re-read on the UCI page 2026-11-25; file hash recorded at export | Credit PPG-DaLiA (Reiss, Indlekofer, Schmidt, Van Laerhoven) in the product's notices |
| Watch and phone speech recognition and text to speech | The platform's own engines (Android `SpeechRecognizer` and `TextToSpeech` on the watch; `expo-speech` and the platform recogniser on the phone). IRA ships no speech weights | not a shipped model: platform services | Platform terms (Google, Apple), not a model licence | unclear | Not a weights question: whether audio leaves the device depends on the platform engine; covered by the DPIA item in the completion register (C-13) | Not checked here (no model card exists) | — |

## Evaluated or used for tooling, not shipped

| Model | Source and revision | Used for | Licence | Commercial use | Conditions and use restrictions | Verified (2026-11-25) | Attribution |
| --- | --- | --- | --- | --- | --- | --- | --- |
| IRA fall model v1 (`fall_model.tflite`, 6,705 parameters, research record only, in no build) | Our own: trained by `scripts/training/kaggle_fall_model.py` on Kaggle (private notebook); kept only as `docs/evidence/training/ira_fall_model.tflite` (SHA-256 `8182ea37154ab52e1d9383b3de42add2a2bb902cafb38daa6056f767d84ae6cb`), outside every build source set. Until 25 November 2026 debug builds carried it from `watch/app/src/debug/assets/` | research only: evaluated earlier (`docs/evidence/training/fall.json`); no build of any kind carries it (D-077 for release, D-082 for debug and demonstration builds; `FallModelReleaseTest` fails on its hash in any variant's assets) | Our own weights (no pretrained start). The training data's terms carry over: SisFall and UCI HAR | unclear | Depends on SisFall, whose original terms are not confirmed (see `docs/DATASETS.md`); UCI HAR is CC BY 4.0 and needs credit. Confirmation draft: `docs/legal/dataset_permission_requests/SISFALL.md` | File hashes compared in the repository; data licences as in `docs/DATASETS.md`; no Kaggle model exists on the training account (Kaggle API, 25 November 2026) | Credit UCI HAR (CC BY 4.0) and SisFall if it is ever used |
| `qwen3.5:2b`, `qwen3.5:4b`, `qwen3.5:9b` | Ollama library; `Qwen/Qwen3.5-2B`, `-4B`, `-9B` on Hugging Face | evaluated only (`docs/MODEL_CHOICE.md`); not the default chain | Apache License 2.0 | yes | Apache 2.0 notice | Hugging Face API read for all three: `license: apache-2.0`. Ollama licence layer for `qwen3.5:2b` read: Apache 2.0 | Qwen team, Alibaba |
| `gemma4:e4b-it-qat`, `gemma4:26b-a4b` | Ollama library | evaluated only | Apache License 2.0 (Gemma 4 family) | yes | As for Gemma 4 | Not read separately; same family as the rows above | Google DeepMind |
| Conversation QLoRA adapter on `google/gemma-4-E4B-it` | Our own: `scripts/training/kaggle_conversation_qlora.py`, on synthetic conversations | trained, not shipped (did not beat the base model) | Apache 2.0 base; our own adapter | yes | — | Base as above | — |
| Piper and the `en_GB-cori-high` voice | Piper software (`rhasspy/piper`), voice from `rhasspy/piper-voices` (revision `5ec43b186ef915a195d3ddbcfd9916e76c503c3f`) | tooling only: narrates the demonstration film (`scripts/film/make_film.py`); not in the product | Piper: MIT. Voices repository: MIT. The voice's training data: public domain (LibriVox), per its model card | yes | MIT notice | Piper `LICENSE.md` read (MIT, 2022 Michael Hansen); Hugging Face API for `rhasspy/piper-voices`: `license: mit`; voice model card `en/en_GB/cori/high/MODEL_CARD` read | Piper (Michael Hansen); the voice's model card points to brycebeattie.com for its training |

## In the foundation code only, not in the IRA CARE deployment

The backend this product grew from (`supracloud-jarvis/`) names more models. The IRA CARE image
(`deploy/Dockerfile.api`) installs `requirements-test.txt`, not the full model stack, and no
care code path loads these. They are listed so that nobody enables one without seeing its terms.

| Model | Source and revision | Used for | Licence | Commercial use | Conditions and use restrictions | Verified (2026-11-25) | Attribution |
| --- | --- | --- | --- | --- | --- | --- | --- |
| OmniVoice | `k2-fsa/OmniVoice`, pinned `c5fdb5ccb189668d56333f77ba2629f4cd7535f4` in `supracloud-jarvis/ira/core/models/artefacts.py` | foundation only (voice sidecar) | Code Apache 2.0; **pre-trained model CC BY-NC** "due to constraints from its training data (e.g., Emilia)" | no | Must not be enabled in a commercial deployment | Model card read, <https://huggingface.co/k2-fsa/OmniVoice> (License section) | k2-fsa |
| Supertonic 3 | `Supertone/supertonic-3`, pinned `724fb5abbf5502583fb520898d45929e62f02c0b` | foundation only; the package is deliberately not installed | OpenRAIL-M (model); sample code MIT | unclear | Use-restricted licence: behavioural restrictions travel with the model and must be passed on to anyone given it; the foundation code notes that hosted access may count as distribution. Owner determination still open | Hugging Face API (`license: openrail`) and card read, <https://huggingface.co/Supertone/supertonic-3> | Supertone |
| faster-whisper base, small, large-v3, distil-large-v3 | `Systran/faster-whisper-*`, pinned in `artefacts.py` | foundation only (local speech to text) | MIT | yes | MIT notice | Hugging Face API read for all four: `license: mit` | Systran; OpenAI Whisper |
| bge-large-en-v1.5, bge-m3 | `BAAI/bge-large-en-v1.5` (pinned `d4aa6901d3a41ba39fb536a557fa166f842b0e09`), `BAAI/bge-m3` | foundation only (memory embeddings) | MIT | yes | MIT notice | Hugging Face API read: `license: mit` | BAAI |
| bge-reranker-v2-m3 | `BAAI/bge-reranker-v2-m3`, pinned `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` | foundation only (memory reranker) | Apache License 2.0 | yes | Apache 2.0 notice | Hugging Face API read: `license: apache-2.0` | BAAI |
| ECAPA speaker embeddings | `speechbrain/spkrec-ecapa-voxceleb`, pinned `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286` | foundation only (voice-print lock) | Apache License 2.0 | yes | Apache 2.0 notice. A voice print is biometric data: a DPIA question before any use | Hugging Face API read: `license: apache-2.0` | SpeechBrain |
| pyannote speaker-diarization-3.1 | `pyannote/speaker-diarization-3.1`, pinned `84fd25912480287da0247647c3d2b4853cb3ee5d` | foundation only (meetings) | MIT | yes | Gated on Hugging Face: users must accept the access conditions with an account | Hugging Face API read: `license: mit`, `gated: auto` | pyannote |
| Indic Parler-TTS | `ai4bharat/indic-parler-tts`, pinned `7b527af5ee8ed1f9a28d80b19703ed9bb8ba10ca` | foundation only | Apache License 2.0 | yes | Gated on Hugging Face (accept conditions) | Hugging Face API read: `license: apache-2.0`, `gated: auto` | AI4Bharat |
| Kokoro-82M | `hexgrad/Kokoro-82M` | foundation only (legacy voice) | Apache License 2.0 | yes | Apache 2.0 notice | Hugging Face API read: `license: apache-2.0` | hexgrad |
| `qwen3:14b`, `qwen3:8b`, `qwen2.5vl`, `qwen2.5-coder:14b` | Ollama tags named in `supracloud-jarvis/.env.example` | foundation only | Apache License 2.0 (Hugging Face cards of `Qwen/Qwen3-14B`, `Qwen/Qwen3-8B`, `Qwen/Qwen2.5-VL-7B-Instruct`, `Qwen/Qwen2.5-Coder-14B-Instruct`) | yes | Apache 2.0 notice. Some Qwen sizes in earlier generations were released under Qwen's own licences rather than Apache 2.0: check the card of the exact size before use | Hugging Face API read for the four cards named | Qwen team, Alibaba |
| `hermes-agent` (`IRA_CORTEX_MODEL`) | named in `supracloud-jarvis/.env.example`; not identified | foundation only | not identified | unclear | Identify the model before any use | Not checked: the name does not identify a published model | — |

## Notes

- Ollama serves its own quantised conversions; the licence that travels with each pull is the
  `application/vnd.ollama.image.license` layer read above, and it matches the Hugging Face card
  for every model checked.
- No copy of the SisFall-trained v1 was ever published on Kaggle. Until 25 November 2026 this note
  said `scripts/training/kaggle_fall_model.py` published it under "Apache 2.0"; that was wrong. The
  script's own run log (`docs/evidence/training/fall.json`) shows the upload refused
  (`KAGGLE_MODEL_UPLOAD_SKIPPED BackendError: Permission 'models.create' was denied`), and on
  25 November 2026 the Kaggle API listed no models on the training account, with the notebook and
  both datasets private. The upload call has since been removed, and no training script may publish
  model weights (D-082, `tests/readiness/test_training_scripts_publish_nothing.py`).
- `supracloud-jarvis/ira/core/models/artefacts.py` refers to an owner determination file
  (`MODEL_LICENCE_DETERMINATIONS_REQUIRED.md`, item L-06) that is not in this repository.
