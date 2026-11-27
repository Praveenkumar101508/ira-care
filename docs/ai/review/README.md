# Technical review packs (round 9)

Four register items are documents or selections whose acceptance is a technical reviewer's
decision, not a test's. Each pack below gives the reviewer the requirement, the evidence as it
stands, the acceptance criterion, the known limitations, the questions to answer and a line to
record the decision. On 27 November 2026 the owner recorded that Sravani Perni had reviewed all four and accepted them;
each pack's decision table says so, and the four items are `accepted` in the register.

| Item | Pack | Reviewer |
|---|---|---|
| F3.c Heart model card | [`REVIEW_PACK_F3c_HEART_MODEL_CARD.md`](REVIEW_PACK_F3c_HEART_MODEL_CARD.md) | the project's technical reviewer (ML) |
| F7.a Activity dataset selection | [`REVIEW_PACK_F7a_ACTIVITY_DATASET.md`](REVIEW_PACK_F7a_ACTIVITY_DATASET.md) | the same |
| F7.e Activity model card | [`REVIEW_PACK_F7e_ACTIVITY_MODEL_CARD.md`](REVIEW_PACK_F7e_ACTIVITY_MODEL_CARD.md) | the same |
| F8.a Speech engine licences | [`REVIEW_PACK_F8a_SPEECH_LICENCES.md`](REVIEW_PACK_F8a_SPEECH_LICENCES.md) | the same, with the owner for the commercial-use decision |

How to record a decision: fill in the last section of the pack (name, date, decision, changes
needed), commit it, and then change the register item's state in `docs/progress/requirements.json`
with an evidence entry `{"kind": "docs_only", "ref": "docs/ai/review/<pack>.md"}`. A `docs_only`
entry never counts as acceptance by itself (`scripts/completion_summary.py`), so the item moves
only if its functional evidence (the model evaluation files already cited) is also present, which
it is for F3.c and F7.e; F7.a and F8.a are selection and licence decisions and stay at the state
the reviewer assigns.
