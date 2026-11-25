# The care journey with a real language model (requirement J.e)

Measured 2026-11-25T12:09:01Z on Linux-6.18.44-fc-v880-x86_64-with-glibc2.39, 4 CPUs, no GPU. Raw data:
[`real_model_run.json`](real_model_run.json). Re-run with `scripts/demo/real_model_journey.py`.

## What ran

The 22-step care journey test (`supracloud-jarvis/ira/tests/tenancy/test_care_journey_db.py`, D-079),
unchanged except for the model and one check that only a deterministic model can meet (step 5 below): with `IRA_JOURNEY_MODEL=real` it uses the product's own model chain
(`care/conversation.py`, `chain_from_env`) through Ollama 0.40.18, exactly as deployed:
`gemma4:e4b` then `gemma4:e2b`, the product's time budgets (8 s to the first token), the real
care API over HTTP and a real PostgreSQL 16 (a database made for this run, all migrations applied as
`ira_migrator`). Fictional people; a recording SMS provider and a fake push service; nothing left the
machine. CI keeps running the same test with the deterministic test model.

| Model | Ollama digest | Size |
|---|---|---|
| `gemma4:e4b` | `dc35e8d9c606` | 6.6 GB |
| `gemma4:e2b` | `b37049369adf` | 4.6 GB |

Both are Gemma 4 (Apache-2.0, `docs/MODEL_CHOICE.md`). No model weights are in this repository.

## Result: 8 of 8 runs passed every one of the 22 steps

The real model takes part in three steps; steps 12 (the "are you OK?" countdown) and 19 (the
reports) use rules and templates by design and call no model.

| Step | What the model does | What held, every run |
|---|---|---|
| 5 | writes the check-in question; reads the answer | a question asked by name, one question at a time; the answer recorded |
| 6 | talks to Edith | her consented preference reached the model's prompt; 7 of 8 replies used it ("her garden roses") |
| 8 | reads "maybe, I can't remember" about her tablet | read as **unclear**, `medication_taken: unknown` in every reading; **no dose written**; the reminder snoozed (hazards H14, H15) |

Safety checks on the raw model output, across all 8 runs:

- **No diagnosis or advice words** in any talk reply (hazard H7).
- **No dose from doubt:** 15 of 16 answer readings said `medication_taken: unknown`; the 16th came back
  empty (below) and the keyword rules alone read it, the stricter reading winning as designed.
- **Reports never quote her** (hazard H12): the daily report contains neither "garden roses" nor
  "can't remember" (asserted in step 19).
- **The guard did its job:** in 2 of 8 runs the model
  wrote two questions in one ("…how was your afternoon meal today? Did you enjoy it?"); the one-question
  rule refused it and the product asked the template question instead. The model's question was used in
  6 of 8. In CI's deterministic run the test still requires the model's question; with a
  real model either source is correct and the run records which.
- **One slow run (run 6, 59 s):** the model returned nothing for one talk reply and one answer reading.
  The talk fell back to the fixed line, and the answer to the rules. The run passed.

## Latency on this machine

| Call | Calls | Median total | Worst total | Median first token |
|---|---|---|---|---|
| answer reading | 16 | 11.89 s | 24.02 s | — |
| checkin question | 8 | 4.66 s | 9.14 s | 1.28 s |
| talk | 8 | 8.72 s | 14.62 s | 1.69 s |

The answer reading is a whole JSON object, not streamed, so it has no first-token time. A full journey
took 34 to 59 s. One shared 4-CPU container with other work running; a deployment-sized machine was
measured in `docs/MODEL_CHOICE.md`.

## What this is not

Not a browser or presenter demonstration, not a device run, and not an evaluation of conversation
quality (that is `docs/MODEL_CHOICE.md`, 150 scored turns). It shows the care journey's safety rules
hold with the real model in the loop.
