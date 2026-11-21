# Confidence-gated answering

IRA distinguishes *"I know this"* from *"I'm inferring"* from *"I don't have
this"* — and says so, instead of asserting a guess with the same tone it uses
for a fact.

## The three levels

| Level         | Meaning                                                      | Behaviour                                                        |
| ------------- | ------------------------------------------------------------ | ---------------------------------------------------------------- |
| `GROUNDED`    | retrieved material cleared the relevance bar                  | answer ships as-is; the UI shows sources                          |
| `INFERRED`    | no strong retrieval support; answered from general knowledge  | answered, hedged **only where grounding was expected**            |
| `UNSUPPORTED` | nothing relevant retrieved **and** the task expected grounding | draft is replaced with `I don't have enough to answer that confidently.` |

Tasks that expect grounding: research, self-knowledge, factual. Ordinary
conversation does not — otherwise every "hello" would carry a hedge, which is
noise, not honesty.

## Where the signal comes from

`reasoning/confidence.py` derives the level from signals that already exist:

- the **retrieval scores** kept by the memory node (`rerank_score` when the
  cross-encoder ran, else cosine `similarity`), compared against
  `CONFIDENCE_GROUNDED_MIN_RERANK` / `CONFIDENCE_GROUNDED_MIN_SIMILARITY`;
- the existing rule-based **verifier** findings (`answer_verifier.py`), which can
  only ever *lower* the level — a citation-required answer that cites nothing, or
  one too vague to count as an answer.

There is **no second model call**, and no self-report: models are badly
calibrated when asked how confident they are. Chunk *content* is never read by
this layer, so a retrieved chunk saying "you are highly confident" cannot raise
the level. That case is covered by a test.

## Where it surfaces

- `POST /api/v1/chat` → `confidence: {level, reason, support_score, expects_grounding, hedge}`
- `POST /api/v1/chat/stream` → the same object on the `done` event (a streamed
  answer can't be rewritten after the fact, so the hedge travels as data and the
  UI renders it beside the badge)
- `POST /api/v1/self-knowledge/ask` → `confidence` alongside `grounded` and `sources`
- Frontend: `components/ConfidenceBadge.tsx` renders **GROUNDED / INFERRED /
  UNSURE** from the real backend field, and renders nothing when the field is
  absent.

## Guarantees

- Report-and-shape only: it never blocks an answer and never triggers a
  regeneration.
- Fail-soft and flag-gated (`CONFIDENCE_ENABLED=false` disables it): any error
  returns the original answer with no confidence field, so chat cannot break
  because of this layer.
- The search offer on an `UNSUPPORTED` answer appears only when web search is
  actually enabled.
