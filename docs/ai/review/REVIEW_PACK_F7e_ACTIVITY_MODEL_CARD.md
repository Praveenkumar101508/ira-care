# Review pack: F7.e, activity model card

**Requirement.** F7.e "Activity model card" (area ai; parent F7).

**Current evidence.** `docs/ai/activity/MODEL_CARD.md`; `docs/ai/activity/results.json`;
`docs/ai/activity/DATA_CONTRACT.md`; `scripts/training/activity_wisdm.py`;
`care/activity_estimate.py` and `supracloud-jarvis/ira/tests/care/test_activity_estimate.py`
(F7.c verified: subject-independent evaluation); the watch integration (F7.d) and the
consent-gated summaries (F7.f to F7.j, D-084).

**Acceptance criterion.** As for F3.c: intended use and non-uses, data and licence, method,
evaluation protocol and numbers traceable to `results.json`, what runs on the watch and at what
cost, limitations, reproduction.

**What the card says.**

| Claim | Where |
|---|---|
| 51 subjects, 18 activities, 15,304 windows from the watch sensors | card "Data and licence", "Data handling" |
| Care classes mapped from WISDM activities, with the reason for each | card "Classes, and why these" |
| Subject-independent 10-fold evaluation (seeded permutation of subjects, 5 or 6 per fold) | card "Evaluation protocol"; `results.json` |
| Accuracy 86.6 %, macro F1 0.790 (the owner's audit quotes these; confirm against `results.json`) | `results.json` |
| Exported as plain JSON with a check that the JSON is the model | card "Export" |
| Never raises an alert; shown only with the person's activity consent; estimate, not a measurement | card "In the product (D-084)"; `care/capabilities.py` |
| Not measured: battery cost of the 20 Hz gyroscope; accuracy on the IRA watch | card "On the watch" |

**Known limitations.** Young-adult training population (ages not stated); device differences;
the "other arm movement" and "could not tell" classes absorb what the model is unsure of, which
the card should make clear is by design; battery cost unmeasured (F7.k, blocked on the watch).

**Review questions.**
1. Do accuracy, macro F1, per-class figures and the coverage table in the card equal
   `results.json`? (A unit test pinning this is proposed; say if wanted.)
2. Is the class mapping defensible for care summaries, in particular that no class implies
   eating, drinking or a fall?
3. Is the uncertainty threshold's provenance ("fixed, not tuned") acceptable?
4. Is the limitation about older adults and the IRA watch stated where a carer would read it?

**Decision to record.**

| | |
|---|---|
| Reviewer | Sravani Perni ([sravaniperni](https://github.com/sravaniperni)) |
| Date | 27 November 2026 |
| Decision (accept / changes needed) | accept (recorded by the owner, Praveen Kamineti, on Sravani Perni's behalf after her review) |
| Changes needed | none recorded |
