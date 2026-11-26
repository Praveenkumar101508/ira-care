# Review pack: F3.c, heart model card

**Requirement.** F3.c "Heart model card" (area ai; parent F3, heart rate from wrist PPG).

**Current evidence.** `docs/ai/heart/MODEL_CARD.md`; results `docs/ai/heart/results.json`;
cross-dataset check `docs/ai/heart/CROSS_DATASET_GALAXYPPG.md` and `cross_dataset_galaxyppg.json`;
research pipeline `docs/ai/heart/research_pipeline_eval.json` and `fallback_rule_analysis.json`
(D-086, D-094); code `care/heart_estimate.py` (classical), `care/heart_learned.py` and
`models/heart_selector.json` (learned selector), `care/heart_research.py` (raw-PPG research mode).

**Acceptance criterion (as the register and `docs/BUILD_RULES.md` put it).** The card states the
intended use and the uses it is not for, the data and its licence, the method, the evaluation
protocol and its numbers as the results file holds them, runtime and size, limitations, and what
ships against what is research only; every number in it can be traced to a file in the repository
and reproduced with the command the card gives.

**What the card says (for the reviewer to check against the files).**

| Claim | Where |
|---|---|
| Data: PPG-DaLiA (UCI 495), CC BY 4.0, 15 subjects aged 30.6 ± 9.6 | card "Data and licence"; `docs/DATASETS.md` |
| Classical estimator: FFT masking 0.5–3.5 Hz, Hann window, Gaussian prior σ 12 bpm around the previous estimate | card "Method" |
| Learned selector: HistGradientBoostingClassifier predicting "within 5 bpm", leave-one-subject-out over 15 subjects | card "Method", `results.json` |
| MAE, mean ± sd over subjects: classical 13.28 ± 5.01 bpm; learned 7.75 ± 3.23 bpm | card "Results"; `results.json` |
| Published reference 7.65 bpm (Reiss et al. 2019) quoted as reported, not reproduced | card "Results" |
| Unavailable when the learned selector's probability is below 0.5 (D-094) | card "Intended use"; `fallback_rule_analysis.json` |
| Not validated on older adults or on the IRA watch | card "Intended use", "Limitations" |

**Known limitations the reviewer should weigh.**
- The training population is 15 adults averaging 30 years; IRA's users are older. The card says
  so; whether that is stated prominently enough is a review question.
- Live raw PPG is not available from Wear OS; the learned path runs on recorded or simulated
  signals only (D-086). F3.d (shipping the learned selection) and F3.e (accuracy on the IRA watch
  against ECG) are blocked on hardware and are not this item.
- The cross-dataset check on Galaxy PPG is a different device and protocol; its numbers are in the
  card's companion file, not a product claim.

**Review questions.**
1. Does every number in the card equal the corresponding value in `results.json`,
   `cross_dataset_galaxyppg.json` and `research_pipeline_eval.json`? (A test for this does not yet
   exist; if the reviewer wants one, say so and it will be added.)
2. Is the intended-use section explicit enough that nobody reads the 7.75 bpm as a claim about
   older adults or about the IRA watch?
3. Is the "unavailable when unsure" rule (D-094) described so a clinician understands when the
   watch will show no value?
4. Is the licence and attribution for PPG-DaLiA complete (CC BY 4.0: attribution text present in
   `docs/legal`)?
5. Are the reproduction command and seeds sufficient to re-run the evaluation?

**Decision to record.** `accept` (F3.c moves to `accepted`, with this pack cited) or
`changes needed` (list them; the item stays `implemented`).

| | |
|---|---|
| Reviewer | |
| Date | |
| Decision | |
| Changes needed | |
