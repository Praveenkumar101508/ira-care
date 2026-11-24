# Which model IRA talks with, and why

**Round 4 changed the choice** to `gemma4:e4b` then `gemma4:e2b`, with the model kept loaded,
a shorter prompt, an instant acknowledgement and prepared check-in questions: see "Round 4"
at the end. The round 3 sections are kept as they were measured.

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

- Intel(R) Xeon(R) Processor @ 2.10GHz, 4 cores, 15.7 GB memory, no GPU, Ollama 0.40.18; the
  same class of machine as round 3.
- Each model ran alone (`OLLAMA_MAX_LOADED_MODELS=1`), loaded before its run and kept loaded,
  one turn at a time, with the first-token and first-sentence budgets raised so the model, not
  the fallback, was measured.
- The raw reports, turn by turn, are in `docs/evidence/eval/round4/`; the variant timings in
  `docs/evidence/eval/round4/variants.jsonl`.
- **First words** is the time from the person's words reaching the conversation service to
  the first line it hands the watch to speak. With the acknowledgement on (D-070) that line is
  produced by the rules without waiting for anything, so it measures 0.0 s on the server; what
  the person hears adds the network hop to the watch and the speech engine's start, which are
  not measured here (they need the watch, `docs/READINESS.md`). **First sentence** is the time
  to the model's first complete sentence, as round 3 measured it.

### What was tried, one change at a time

`gemma4:e4b`, 12 talk turns from the evaluation set per variant (6 for the cold one, whose
turns take up to half a minute each), through the repository's own `conv.talk()`. Each line
adds one change to the one above. Samples of 12 turns vary by about a second between runs on
this machine; the 162-turn runs below are the figures to quote.

| # | Variant | First words s, p50 | First sentence s, p50 / p95 |
| --- | --- | --- | --- |
| 1 | Round 3 code, the model unloaded before every turn (what the first person after a quiet spell met) | 15.4 | 15.4 / 30.9 |
| 2 | Round 3 code, the model kept loaded (`keep_alive`) | 5.1 | 5.1 / 8.0 |
| 3 | + the shorter prompt and capped context (context window still 2048) | 4.0 | 4.0 / 6.4 |
| 4 | + a context window of 1024 | 3.5 | 3.5 / 5.7 |
| 5 | + `num_thread` 4, one per core | 3.4 | 3.4 / 7.2 |
| 6 | + the quantisation-aware 4-bit build of the same model (`gemma4:e4b-it-qat`) | 2.3 | 2.3 / 3.7 |
| 7 | variant 4 + the instant acknowledgement (D-070) | 0.0 | 4.4 / 6.3 |

Kept: 2, 3, 4 and 7. Not kept: 5 (no better at the median and worse at the 95th percentile
than leaving the thread count to Ollama; the setting stays available as
`IRA_CARE_MODEL_THREADS`). Variant 6 was measured on all 162 turns before it was decided (below).
No quantisation below 4 bits is published for these models in Ollama's library, so none was
tried. Two further changes have no latency of their own to measure on a talk turn: sentences
now reach the watch as each one is spoken (the talk route used to wait for the whole reply),
and a scheduled check-in's question is composed up to five minutes before it is due (D-071),
so the watch asks it the moment it is due with no model time at all.

### The numbers, every candidate on the round 4 code

| Model (Ollama tag) | Quality | Red-flag recall | Advice refused | Injection resisted | Redirect | Check-in reading | First words s, p50 | First sentence s, p50 / p95 | Fallbacks | Minutes for 162 turns |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `gemma4:12b` | 0.895 | 1.00 | 0.92 | 1.00 | 0.92 | 0.73 | 0.0 | 10.5 / 12.8 | 0 | 31 |
| `gemma4:e2b` | 0.882 | 1.00 | 0.92 | 0.95 | 0.77 | 0.53 | 0.0 | 1.9 / 3.5 | 0 | 10 |
| `gemma4:e4b-it-qat` | 0.844 | 1.00 | 0.88 | 0.95 | 0.96 | 0.60 | 0.0 | 2.0 / 2.7 | 0 | 9 |
| `gemma4:e4b` | 0.909 | 1.00 | 0.88 | 1.00 | 0.96 | 0.60 | 0.0 | 2.8 / 5.9 | 0 | 16 |
| `qwen3.5:2b` | 0.834 | 1.00 | 0.88 | 1.00 | 0.96 | 0.00 | 0.0 | 1.6 / 2.4 | 1 | 6 |

Every row ran the round 4 code with the acknowledgement on, on all 162 turns. Quality is the
share of turns that pass every check in their category, weighted equally across categories, as
in round 3. Red-flag recall is 1.00 for every row because the keyword rules run on every answer
whatever the model says (D-056). Every model scores higher than in round 3 on the same weights
(`gemma4:e4b` 0.779 then, 0.909 now; `gemma4:12b` 0.860 then, 0.895 now): the shorter prompt
names the person to offer when someone sounds worried or low, and the redirect rate rose from
0.05 to 0.96 for `gemma4:e4b`.

- `qwen3.5:2b` read none of the 15 check-in answers: it writes the mood words as one string
  ("fine thank you") where the form asks for a list, so every structured reading was refused
  and the keyword rules alone were used (checked by hand on 24 November 2026).
- `gemma4:e4b-it-qat`, the quantisation-aware 4-bit build of the same model, is the fastest
  Gemma here (2.0 s), but its quality is 0.065 below the best.

### The choice

**Primary: `gemma4:e4b`. Secondary: `gemma4:e2b`.** Chosen as in round 3, quality first, then
latency, now with the round 4 rule that a model may be at most 0.03 below the best measured.

- `gemma4:e4b` is the best measured model on the round 4 code (0.909), with red-flag recall
  1.00, every injection resisted, and the first sentence at 2.8 s p50 (5.9 s p95). With the
  acknowledgement the first words are handed to the watch at once. Both targets are met:
  first words within 1.5 s and the first sentence within 3 s, at the median.
- `gemma4:e2b` (0.882) is the only other model inside the 0.03 band and the fastest of those
  (1.9 s). It is the second model in the chain, loaded only when the primary has not started
  inside the first-token budget.
- `gemma4:12b` read check-in answers best (0.73 against 0.60) but takes 10.5 s to its first
  sentence on four cores; it is the model to make primary on a machine with a GPU (below).
- What is still slow: the 95th percentile of the first sentence (5.9 s) is above the 3 s
  target, on long or emotional turns where the model writes a long first sentence. The
  acknowledgement covers that wait; it does not shorten it. A check-in question never waits
  on the model at all (D-071).

What this means for the person: IRA answers at once with a short line, and the reply itself
follows within about three seconds on the 4 vCPU machine `docs/DEPLOY.md` describes, against
about ten seconds in round 3 (and fifteen after a quiet spell, when the model had unloaded).

### What a small GPU server would give

Nothing below was measured on this machine: it has no GPU. Each figure says where it was read.
A measurement of the same prompts on Kaggle's free GPU is scripted
(`scripts/training/kaggle_ollama_bench.py`, run with `scripts/training/run_kaggle.py gpu_bench`);
on 24 November 2026 it waited over an hour in Kaggle's queue for a GPU, and its numbers are
added here when it has run.
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
