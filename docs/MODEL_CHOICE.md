# Which model IRA talks with, and why

Measured on 2026-11-23 with `scripts/eval_care_model.py` over the 150-turn
evaluation set in `supracloud-jarvis/ira/tests/care/eval/turns.jsonl` (nine categories; the
README beside it says how each is scored). Every candidate is a free open-weight model served
by Ollama 0.40 on the deployment-sized machine, with no GPU:

- Intel(R) Xeon(R) Processor @ 2.10GHz, 4 cores, 15.7 GB memory, none (CPU only).
- Each model ran alone (the others unloaded), one turn at a time, with the first-token and
  first-sentence budgets raised so the model, not the fallback, was measured.
- The raw reports, turn by turn, are in `docs/evidence/eval/<model>.json`.

## The numbers

| Model (Ollama tag) | Quality | Red-flag recall | Advice refused | Injection resisted | Redirect | Check-in reading | First sentence s, p50 / p95 | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `gemma4:12b` | 0.860 | 1.00 | 0.92 | 1.00 | 0.45 | 0.80 | 10.0 / 12.2 | 0 fallbacks, 31 min for 150 turns |
| `gemma4:e4b` | 0.779 | 1.00 | 0.88 | 0.95 | 0.05 | 0.60 | 4.6 / 6.6 | 0 fallbacks, 20 min for 150 turns |
| `qwen3.5:9b` | 0.778 | 1.00 | 0.88 | 0.95 | 0.35 | 0.73 | 6.5 / 9.5 | 0 fallbacks, 40 min for 150 turns |
| `qwen3.5:4b` | 0.734 | 1.00 | 0.92 | 0.90 | 0.40 | 0.33 | 4.4 / 9.9 | 2 fallbacks, 22 min for 150 turns |
| `gemma4:26b-a4b` | did not load | | | | | | | 18 GB on disk; killed while loading on 15.7 GB |
| rules only (`IRA_CARE_MODEL=off`) | 0.966 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.0 | the set questions and keyword rules, no model |

Quality is the share of turns that pass every check in their category, weighted equally
across categories. Red-flag recall is 1.00 for every row because the keyword rules run on
every answer whatever the model says (D-056): the model never decides whether "I can't
breathe" is urgent.

## The choice

**Primary: `gemma4:12b`. Secondary: `gemma4:e4b`.** Chosen on quality first, then latency.

- `gemma4:12b` reads check-in answers best (0.80 of the structured-answer turns right, against
  0.60 for `gemma4:e4b` and 0.33 for `qwen3.5:4b`), resists every injection in the set, and
  redirects a distressed person to a named carer in 0.45 of the distress turns where the
  small models almost never do. Its cost is time: about 10 s to the first sentence on four
  CPU cores, 12 s at the 95th percentile.
- `gemma4:e4b` is the fastest model that still catches every red flag and refuses advice at
  0.88: 4.6 s to the first sentence. It is the second model in the chain: when the primary
  has not started speaking inside the first-token budget, the chain moves on, and when
  neither speaks inside the first-sentence budget the set question is asked instead.
- `qwen3.5:9b` is slower than `gemma4:e4b` for no gain; `qwen3.5:4b` fell back twice in 150
  turns (JSON the schema could not read) and read check-in answers poorly.
- `gemma4:26b-a4b` (a mixture-of-experts model with 4B active parameters) did not fit: the
  kernel killed Ollama's server while it loaded 18 GB into 15.7 GB of memory. On a 32 GB
  machine it would be the one to measure next.
- The rules alone score higher than any model on this set because the set asks for the
  things the rules guarantee (a refusal, a redirect, a red flag). The model's value is in
  the warmth and the reading of free text, which the set measures through the check-in
  categories, where the model and rules together beat the rules alone (the "stricter
  reading wins" merge in `care/conversation.py`).

What this means for the person: on the 4-vCPU machine `docs/DEPLOY.md` describes, IRA
answers a spoken question after roughly ten seconds. The watch says "I'm listening" and shows
the words as they arrive, and the set questions never wait on a model. A machine with eight
cores or a small GPU roughly halves the wait; `IRA_CARE_MODEL_PRIMARY=gemma4:e4b` trades
reading quality for a 4.6 s first sentence on the same hardware. Both models are 8.0 GB and
6.6 GB on disk and are not held in memory together on 16 GB: Ollama loads the secondary when
the chain falls back, which costs one model load (about 10 s) on that turn.

## Our own models (round 3, phase 5)

Three models were trained on Kaggle's free GPU (private notebooks, no tokens in them), each
from an open pretrained starting point and only on data whose licence allows commercial use
(`docs/DATASETS.md`). The evidence files are in `docs/evidence/training/`.

| Model | Starts from | Data | Before | After | GPU time | Shipped? |
| --- | --- | --- | --- | --- | --- | --- |
| Fall detector (watch, TensorFlow Lite, 6,705 parameters, 17.9 kB) | a fresh small network (no pretrained weights exist for this sensor layout) | SisFall (CC BY 4.0) resampled to 50 Hz, UCI HAR (CC BY 4.0) negatives; 17 training and 21 test subjects | the rule detector on the same windows: sensitivity 0.906, specificity 0.984, 344 false alarms a day | sensitivity 0.990, specificity 0.9998, 3.8 false alarms a day on 6,243 held-out windows; TensorFlow Lite agrees with Keras on 200 of 200 windows | 186 s | yes, beside the rule detector; the watch's off-body sensor gates both (`fall.json`) |
| Answer classifier (server, MiniLM embeddings + logistic heads) | `sentence-transformers/all-MiniLM-L6-v2` (Apache-2.0) | 1,120 synthetic answers from the product's own rules and phrasing lists | rules alone on the 150-turn eval: 147 of 150 | with the classifier at the chosen confidence gates: 147 of 150, five false alarms without the gates | 48 s | behind `IRA_CARE_CLASSIFIER`, default off: it equals the rules and never beats them (`classifier_eval.json`) |
| Conversation fine-tune (QLoRA on `gemma4:e4b`) | `google/gemma-4-E4B-it` | 507 synthetic care conversations generated by the base model and kept by the product's rules; no public dialogue set has a commercial licence | rule pass rate on 50 held-out prompts: 0.94 | 0.92; training loss 0.321 to 0.369 over 457 steps | about 2.6 h over four attempts (1,170 s of training in the one that completed) | no: it did not beat the base model, so the chain above stays as chosen (`conversation.json`) |

What this means: the watch's fall detection is better by a trained model and ships with
it; IRA's words come from the open base models exactly as phase 4 measured them; the two
server-side models that did not beat what was there are recorded with their numbers and
left off.

## Licences

| Model | Weights licence | Source |
| --- | --- | --- |
| Gemma 4 (`gemma4:e4b`, `gemma4:12b`, `gemma4:26b-a4b`) | Apache-2.0 (the Hugging Face model cards; Google also publishes the Gemma 4 terms at ai.google.dev/gemma/docs/gemma_4_license) | https://huggingface.co/google/gemma-4-E4B-it, https://huggingface.co/google/gemma-4-12B-it |
| Qwen3.5 (`qwen3.5:4b`, `qwen3.5:9b`) | Apache-2.0 | https://huggingface.co/Qwen |

No hosted model API is used or available (D-056). The budgets: `IRA_CARE_MODEL_FIRST_TOKEN_S`
(3 s by default: the chain moves to the next model), `IRA_CARE_MODEL_FIRST_SENTENCE_S` (now
12 s: the set question takes over). The fine-tuning of the primary on synthetic care turns is
phase 5 (`feat/own-models`); its before-and-after numbers are added to this file when it lands.

## Round 4: a conversation that feels smooth on four CPU cores

Target set for round 4 (phase 1, `feat/fast-conversation`): first spoken words within 1.5 s
(p50) and the first full sentence within 3 s (p50) on the same 4 vCPU machine, red-flag recall
still 1.00, quality no more than 0.03 below the best measured model. Everything below was
measured on this machine on 24 November 2026 with `scripts/eval_care_model.py` over the
evaluation set, which is now 162 turns (the 150 of round 3 plus the 12 of phase 7).

__ROUND4_HARDWARE__

### What was tried, one change at a time

__ROUND4_VARIANTS__

### The numbers, every candidate on the round 4 code

__ROUND4_TABLE__

### The choice

__ROUND4_CHOICE__

### What a small GPU server would give

Nothing below was measured here: this machine has no GPU. Each figure says where it was read.
The product does not need a GPU; one makes the model's part of a turn several times faster.

- **Why the CPU is the limit.** On four cores, `gemma4:e4b` processes the prompt at about
  100 tokens a second and generates at 3.5 to 5 tokens a second (measured here, round 4
  bench runs); `qwen3.5:2b` generates at about 15 tokens a second. Token generation is bound
  by memory bandwidth: a rule of thumb is tokens per second ≈ memory bandwidth ÷ model size
  in memory, as a ceiling (read from a comparison of local inference engines at
  https://computingforgeeks.com/ollama-vs-vllm-vs-llama-cpp/). A server's DDR4 gives tens
  of GB/s; a small GPU gives hundreds.
- **Measured by others on small GPUs.** Gemma 4 E4B at Q4_K_M: a user-reported 74 tokens a
  second on an RTX 3060 12 GB with llama.cpp (LM Studio), about 128 on an RTX 4090, and a
  listed 37.9 on an RTX 4060 8 GB where the model only just fits (all read from
  https://willitrunai.com/models/gemma-4-e4b). Those are 10 to 30 times this machine's
  generation rate; at 74 tokens a second a ten-token first sentence is ready in well under
  half a second after the prompt is processed, and the prompt itself is processed in tens of
  milliseconds. Estimates for the same card by other sites (26 to 61 tokens a second;
  https://llmconfigurator.com/en/gpu/rtx-4060, https://modelfit.io/can-i-run/gemma4-e4b-q4-on-rtx-4060/)
  are engine estimates, not measurements. A measurement of Gemma 4 E4B under Ollama on a
  96 GB Blackwell card reported 196 tokens a second
  (https://allenkuo.medium.com/gemma-4-on-vllm-vs-ollama-benchmarks-on-a-96-gb-blackwell-gpu-804ca4845a21),
  which says what the ceiling looks like, not what a small server gives.
- **What it would mean for IRA.** With `gemma4:12b` (8 GB on disk, about 9 GB loaded) on a
  20 or 24 GB card, the model that read check-in answers best in round 3 could be primary
  again with a first sentence well inside the 3 s target, and the secondary could stay
  loaded beside it. The acknowledgement (D-070) and the prepared questions (D-071) would
  still apply; they cost nothing.
- **What it costs (read from the providers' pages on 24 November 2026; check the live price).**
  A dedicated server with an NVIDIA RTX 4000 SFF Ada (20 GB) at Hetzner (GEX44) was launched at
  €184 a month plus a one-off setup fee (https://www.hetzner.com/press-release/new-gpu-server;
  the product page https://www.hetzner.com/dedicated-rootserver/gex44/ showed €184 to €205
  and a price-adjustment note listed €232.30); its successor with an RTX PRO 4000 Blackwell
  (24 GB), the GEX45, is listed at €214 a month
  (https://www.hetzner.com/dedicated-rootserver/gex45/). A cloud instance with one NVIDIA L4
  (24 GB) at Scaleway (L4-1-24G) is listed at €0.75 an hour, about €550 a month if left running
  (https://www.scaleway.com/en/pricing/gpu/). The 4 vCPU, 16 GB machine `docs/DEPLOY.md`
  describes costs a fraction of either, which is why the CPU path is the one that was made
  to feel smooth.
- **Nothing hosted.** A GPU server runs the same Ollama and the same open models on the care
  team's own machine; no model API is involved (D-056). The only change to the deployment
  is giving the `ollama` container the GPU through the NVIDIA container runtime, which
  Ollama's Docker documentation describes (https://docs.ollama.com/docker).
