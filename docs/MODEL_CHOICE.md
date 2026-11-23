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

## Licences

| Model | Weights licence | Source |
| --- | --- | --- |
| Gemma 4 (`gemma4:e4b`, `gemma4:12b`, `gemma4:26b-a4b`) | Apache-2.0 (the Hugging Face model cards; Google also publishes the Gemma 4 terms at ai.google.dev/gemma/docs/gemma_4_license) | https://huggingface.co/google/gemma-4-E4B-it, https://huggingface.co/google/gemma-4-12B-it |
| Qwen3.5 (`qwen3.5:4b`, `qwen3.5:9b`) | Apache-2.0 | https://huggingface.co/Qwen |

No hosted model API is used or available (D-056). The budgets: `IRA_CARE_MODEL_FIRST_TOKEN_S`
(3 s by default: the chain moves to the next model), `IRA_CARE_MODEL_FIRST_SENTENCE_S` (now
12 s: the set question takes over). The fine-tuning of the primary on synthetic care turns is
phase 5 (`feat/own-models`); its before-and-after numbers are added to this file when it lands.
