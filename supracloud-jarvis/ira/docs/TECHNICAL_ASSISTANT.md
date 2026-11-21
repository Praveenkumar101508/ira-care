# Technical personal assistant

Two capabilities behind one prefix, deliberately kept apart so neither drags the
other down: **write/explain code**, and **act on the calendar**.

## `POST /api/v1/assist/code`

```json
{"request": "add a retry decorator to utils/llm.py", "deep": false, "context": ""}
```

Returns `{code, explanation, verification, verification_source, raw, model,
provider, routing_reason, deep_available, warnings}`.

- Uses the light **Engineer** prompt (`agents/engineer_agent.py`) plus the
  existing model router. Forge, Brain and NeuroKernel are not involved.
- `deep: true` takes Deep Intelligence Mode **only** when the router offered it
  *and* `IRA_ALLOW_EXTERNAL_API` is on. `apply_consent` refuses otherwise, so
  there is no silent external call; `deep_available` reports whether the option
  existed.
- Every answer carries a verification step. When the model omits one, the
  endpoint substitutes this repo's real command and says so via
  `verification_source: "default"` plus a `missing_test_step` warning — it never
  pretends the model supplied it.

## `POST /api/v1/assist/explain`

```json
{"target": "supracloud-jarvis/ira/reasoning/confidence.py"}
```

A file path gets symbols and imports from the deterministic parser
(`coding/intelligence/symbol_graph.py`); anything else gets a ranked project
context from `ContextBuilder`. That structure is handed to the model as ground
truth, with an instruction not to invent anything it doesn't show. The parsed
`structure` is returned alongside the prose so a reader can check the claims.

## `POST /api/v1/assist/schedule`

```json
{"request": "schedule a meeting with Anita next Tuesday at 3pm"}
```

**First call — simulate, never create.** The request is parsed deterministically
(no model call, so the preview is exactly what will run) into a `ScheduleIntent`
whose `assumptions` list every default that had to be applied ("no time in the
request — assumed 10:00"). It returns `status: "confirmation_required"`, a
token, the Shadow World simulation, and a Proof Card.

**Second call — with `confirm_token`.** Runs the existing governed chain:
`execute_gate` → CalDAV create → Trust Ledger records → Proof Card. A missing,
stale or wrong token creates nothing; a non-owner is refused before simulation.

Both responses carry `proof_card_summary`, a compact view built by
`trust.proof_card.summarise_proof_card`: what was attempted, what it touched,
whether it was simulated, who approved it, whether it ran, how to undo it, and
whether anything left the device. Every field is copied from the full card — the
renderer computes no new claims.

Reuse note: none of this re-implements calendar writing, approval or the
ledger. `assist/schedule.py` wires a natural-language entry point onto
`actions/calendar_dav.py` and `actions/gated_execution.py`. The one behavioural
improvement made along the way: `confirm_action` now builds the Proof Card from
the ledger records it just wrote (fail-soft), so the card shows who approved and
what ran instead of intent alone.
