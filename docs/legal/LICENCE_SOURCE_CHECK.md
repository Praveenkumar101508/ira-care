# Licence registers re-read at their sources (requirements F1.a, F1.b)

Checked at 2026-11-27T14:03:39+00:00 by `scripts/legal/verify_licence_sources.py`, from the source list
`scripts/legal/licence_sources.json`. For each row of `docs/DATASETS.md` and
`docs/ai/MODEL_WEIGHTS.md` that comes from a published source, the script read the place where the
licence is stated (the Zenodo or Hugging Face record through its API, or the page itself) and
compared the licence it found with the register. The machine-readable record is
`LICENCE_SOURCE_CHECK.json`; `supracloud-jarvis/ira/tests/readiness/test_licence_source_check.py`
fails if the record stops covering the list or ever records a disagreement.

**Result: 45 sources agree with the register, 0 differ, 6 could not be reached** (re-read on 28 November 2026, with the Erciyes row added).

| Register | Row | Source read | Register says | Source says | Result |
|---|---|---|---|---|---|
| DATASETS.md | SisFall | <https://zenodo.org/api/records/22212281> | cc-by-4.0 | cc-by-4.0 | agrees |
| DATASETS.md | SisFall | <http://sistemic.udea.edu.co/en/investigacion/proyectos/english-falls/> | cc-by-4.0 | — | unreachable (HTTP 503) |
| DATASETS.md | BITS-2 Dataset for Fall Detection | <https://zenodo.org/api/records/10013090> | cc-by-4.0 | cc-by-4.0 | agrees |
| DATASETS.md | BITS-2 Dataset for Fall Detection | <https://zenodo.org/api/records/8082667> | cc-by-4.0 | cc-by-4.0 | agrees |
| DATASETS.md | WEDA-FALL | <https://raw.githubusercontent.com/joaojtmarques/WEDA-FALL/main/LICENSE> | odc-by-1.0 | odc-by-1.0 | agrees |
| DATASETS.md | KFall | <https://sites.google.com/view/kfalldataset> | by-request | by-request | agrees |
| DATASETS.md | MobiFall v2.0 | <https://bmi.hmu.gr/?p=1642> | by-request | — | unreachable |
| DATASETS.md | UMAFall | <https://figshare.com/articles/dataset/UMA_ADL_FALL_Dataset_zip/4214283> | cc-by-4.0 | — | unreachable (HTTP 202) |
| DATASETS.md | FallAllD | <https://ieee-dataport.org/open-access/fallalld-comprehensive-dataset-human-falls-and-activities-daily-living> | cc-by | cc-by | agrees |
| DATASETS.md | UCI HAR | <https://archive.ics.uci.edu/dataset/240/human+activity+recognition+using+smartphones> | cc-by-4.0 | cc-by-4.0 | agrees |
| DATASETS.md | WISDM | <https://archive.ics.uci.edu/dataset/507/wisdm+smartphone+and+smartwatch+activity+and+biometrics+dataset> | cc-by-4.0 | cc-by-4.0 | agrees |
| DATASETS.md | HARTH | <https://archive.ics.uci.edu/dataset/779/harth> | cc-by-4.0 | cc-by-4.0 | agrees |
| DATASETS.md | PPG-DaLiA | <https://archive.ics.uci.edu/dataset/495/ppg+dalia> | cc-by-4.0 | cc-by-4.0 | agrees |
| DATASETS.md | GalaxyPPG | <https://zenodo.org/api/records/14635823> | cc-by-4.0 | cc-by-4.0 | agrees |
| DATASETS.md | MIMIC-III waveform | <https://physionet.org/content/mimic3wdb-matched/1.0/> | odbl-1.0 | odbl-1.0 | agrees |
| DATASETS.md | Sleep-EDF Expanded | <https://physionet.org/content/sleep-edfx/1.0.0/> | odc-by-1.0 | odc-by-1.0 | agrees |
| DATASETS.md | BIDSleep | <https://physionet.org/content/bidsleep-dataset/1.0.1/> | odc-by-1.0 | odc-by-1.0 | agrees |
| DATASETS.md | EmpatheticDialogues | <https://huggingface.co/api/datasets/facebook/empathetic_dialogues> | cc-by-nc-4.0 | cc-by-nc-4.0 | agrees |
| DATASETS.md | LibriSpeech test-clean | <https://huggingface.co/api/datasets/openslr/librispeech_asr> | cc-by-4.0 | cc-by-4.0 | agrees |
| DATASETS.md | LibriSpeech test-clean | <https://www.openslr.org/12> | cc-by-4.0 | — | unreachable |
| DATASETS.md | Mozilla Common Voice | <https://datacollective.mozillafoundation.org> | cc0-1.0 | — | unreachable |
| DATASETS.md | Simulated Falls and Daily Living Activities (Erciyes) | <https://archive.ics.uci.edu/dataset/455/simulated+falls+and+daily+living+activities+data+set> | cc-by-4.0 | cc-by-4.0 | agrees |
| MODEL_WEIGHTS.md | gemma4:e4b | <https://huggingface.co/api/models/google/gemma-4-E4B-it> | apache-2.0 | apache-2.0 | agrees |
| MODEL_WEIGHTS.md | gemma4:e2b | <https://huggingface.co/api/models/google/gemma-4-E2B-it> | apache-2.0 | apache-2.0 | agrees |
| MODEL_WEIGHTS.md | gemma4:12b | <https://huggingface.co/api/models/google/gemma-4-12B-it> | apache-2.0 | apache-2.0 | agrees |
| MODEL_WEIGHTS.md | gemma4:e4b | <https://ai.google.dev/gemma/docs/gemma_4_license> | apache-2.0 | — | unreachable |
| MODEL_WEIGHTS.md | sentence-transformers/all-MiniLM-L6-v2 | <https://huggingface.co/api/models/sentence-transformers/all-MiniLM-L6-v2> | apache-2.0 | apache-2.0 | agrees |
| MODEL_WEIGHTS.md | sentence-transformers/all-MiniLM-L6-v2 | <https://huggingface.co/api/models/nreimers/MiniLM-L6-H384-uncased> | mit | mit | agrees |
| MODEL_WEIGHTS.md | Silero VAD v6 | <https://raw.githubusercontent.com/snakers4/silero-vad/master/LICENSE> | mit | mit | agrees |
| MODEL_WEIGHTS.md | qwen3.5:2b | <https://huggingface.co/api/models/Qwen/Qwen3.5-2B> | apache-2.0 | apache-2.0 | agrees |
| MODEL_WEIGHTS.md | qwen3.5:2b | <https://huggingface.co/api/models/Qwen/Qwen3.5-4B> | apache-2.0 | apache-2.0 | agrees |
| MODEL_WEIGHTS.md | qwen3.5:2b | <https://huggingface.co/api/models/Qwen/Qwen3.5-9B> | apache-2.0 | apache-2.0 | agrees |
| MODEL_WEIGHTS.md | Piper and the `en_GB-cori-high` voice | <https://huggingface.co/api/models/rhasspy/piper-voices> | mit | mit | agrees |
| MODEL_WEIGHTS.md | Piper and the `en_GB-cori-high` voice | <https://raw.githubusercontent.com/rhasspy/piper/master/LICENSE.md> | mit | mit | agrees |
| MODEL_WEIGHTS.md | OmniVoice | <https://huggingface.co/k2-fsa/OmniVoice/raw/main/README.md> | cc-by-nc | cc-by-nc | agrees |
| MODEL_WEIGHTS.md | Supertonic 3 | <https://huggingface.co/api/models/Supertone/supertonic-3> | openrail | openrail | agrees |
| MODEL_WEIGHTS.md | faster-whisper | <https://huggingface.co/api/models/Systran/faster-whisper-base> | mit | mit | agrees |
| MODEL_WEIGHTS.md | faster-whisper | <https://huggingface.co/api/models/Systran/faster-whisper-small> | mit | mit | agrees |
| MODEL_WEIGHTS.md | faster-whisper | <https://huggingface.co/api/models/Systran/faster-whisper-large-v3> | mit | mit | agrees |
| MODEL_WEIGHTS.md | faster-whisper | <https://huggingface.co/api/models/Systran/faster-distil-whisper-large-v3> | mit | mit | agrees |
| MODEL_WEIGHTS.md | bge-large-en-v1.5 | <https://huggingface.co/api/models/BAAI/bge-large-en-v1.5> | mit | mit | agrees |
| MODEL_WEIGHTS.md | bge-large-en-v1.5 | <https://huggingface.co/api/models/BAAI/bge-m3> | mit | mit | agrees |
| MODEL_WEIGHTS.md | bge-reranker-v2-m3 | <https://huggingface.co/api/models/BAAI/bge-reranker-v2-m3> | apache-2.0 | apache-2.0 | agrees |
| MODEL_WEIGHTS.md | ECAPA speaker embeddings | <https://huggingface.co/api/models/speechbrain/spkrec-ecapa-voxceleb> | apache-2.0 | apache-2.0 | agrees |
| MODEL_WEIGHTS.md | pyannote speaker-diarization-3.1 | <https://huggingface.co/api/models/pyannote/speaker-diarization-3.1> | mit | mit | agrees |
| MODEL_WEIGHTS.md | Indic Parler-TTS | <https://huggingface.co/api/models/ai4bharat/indic-parler-tts> | apache-2.0 | apache-2.0 | agrees |
| MODEL_WEIGHTS.md | Kokoro-82M | <https://huggingface.co/api/models/hexgrad/Kokoro-82M> | apache-2.0 | apache-2.0 | agrees |
| MODEL_WEIGHTS.md | qwen3:14b | <https://huggingface.co/api/models/Qwen/Qwen3-14B> | apache-2.0 | apache-2.0 | agrees |
| MODEL_WEIGHTS.md | qwen3:14b | <https://huggingface.co/api/models/Qwen/Qwen3-8B> | apache-2.0 | apache-2.0 | agrees |
| MODEL_WEIGHTS.md | qwen3:14b | <https://huggingface.co/api/models/Qwen/Qwen2.5-VL-7B-Instruct> | apache-2.0 | apache-2.0 | agrees |
| MODEL_WEIGHTS.md | qwen3:14b | <https://huggingface.co/api/models/Qwen/Qwen2.5-Coder-14B-Instruct> | apache-2.0 | apache-2.0 | agrees |

## What this does not cover

- **Sources this machine cannot reach** (above, `unreachable`): the SisFall laboratory page
  (HTTP 503), the MobiFall laboratory page, figshare for UMAFall (it serves a bot check, HTTP 202),
  OpenSLR for LibriSpeech (the Hugging Face copy, which is the one used, agrees), the Mozilla Data
  Collective, and Google's Gemma licence page (the Hugging Face card for each Gemma model, which is
  where the weights come from, agrees). These need a browser: open each link and compare with the
  register's row. The permission requests for SisFall and UMAFall are already sent.
- **Kaggle re-uploads** (Falls vs Normal Activities, UNIUD, the wearable PPG set, the empathy and
  mental-health sets, and the Kaggle copies of KFall, MobiFall and FallAllD): Kaggle answers only
  to a signed-in client, so they were not re-read. None of them is used for anything that ships.
- **Our own models** (the fall, heart, activity and classifier models): their licence is ours; the
  register records their training data, whose rows are checked above.
- `hermes-agent` is not a published model the name identifies; the register already says to
  identify it before any use.

## Read in a browser by the owner, 27 November 2026

The six sources the script could not reach were opened in a browser. What each page said:

| Source | Result | What the page says |
|---|---|---|
| SisFall laboratory page (<http://sistemic.udea.edu.co/en/investigacion/proyectos/english-falls/>) | **not confirmed** | The page did not load on http or https in three attempts; the server appears to be down. The Zenodo copy (CC BY 4.0) is still the only licence read. The confirmation request sent on 26 November is unanswered. SisFall is in no build. |
| MobiFall and MobiAct (now <https://bmi.hmu.gr/el/the-mobifall-and-mobiact-datasets/>) | **agrees** (no open licence) | Data on request by email to bmi@hmu.gr, for non-commercial, research and educational purposes only, after a signed database usage agreement. The register already says commercial use "no"; MobiFall is not used. |
| UMAFall on figshare (DOI 10.6084/m9.figshare.4214283) | **confirmed: CC BY 4.0** | Version 8, posted 1 December 2025. Publications that use it must cite the figshare page and either Santoyo-Ramón et al. (2018) or Casilari et al. (2017). The register row is updated from "unclear" to CC BY 4.0. UMAFall is not used. |
| LibriSpeech on OpenSLR (now <https://openslr.org/12/>) | **agrees: CC BY 4.0** | Audio from LibriVox public-domain audiobooks; cite Panayotov et al., ICASSP 2015. The original and the Hugging Face copy used agree. |
| Mozilla Data Collective (now <https://mozilladatacollective.com/>) | **not stated on the homepage** | Each dataset has its own terms, set by its uploader (open, community-governed or paid). The Common Voice release's own page must be read before it is used; it is not used yet (F8.c). |
| Gemma licence page (now <https://gemma.ai/resources/apache-2/>) | **agrees: Apache 2.0** | Google's link redirects to this page, which shows the standard Apache License 2.0 text. The new address is recorded in the model register. |

After this, F1.a still has two rows not confirmed at their original source: SisFall (the server
is down) and the Common Voice release (its own page is yet to be read). Neither is used in
anything that ships. F1.a stays `implemented` until both are read.

## What it means for the register

- **F1.b, the model-weight register: accepted.** Every published model in the register was read at
  its source and agrees, including the non-commercial OmniVoice weights and the use-restricted
  Supertonic licence.
- **F1.a, the dataset register: still implemented.** Every dataset used for anything that ships
  (BITS-2, UCI HAR, WISDM, PPG-DaLiA) and every evaluation set (WEDA-FALL, GalaxyPPG, LibriSpeech)
  agrees at its source. After the browser check, two rows remain unconfirmed at their original
  source: SisFall and the Common Voice release.
