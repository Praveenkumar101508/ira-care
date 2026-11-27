# Review pack: F7.a, wrist IMU activity dataset selected with a licence that allows commercial use

**Requirement.** F7.a (area ai; parent F7, activity recognition).

**Current evidence.** `docs/DATASETS.md` (WISDM row); `docs/ai/activity/MODEL_CARD.md` "Data and
licence"; `scripts/training/activity_wisdm.py` (download hash check and the data handling); the
CC BY notice beside `watch/app/src/main/assets/activity_model.json`.

**What was selected.** WISDM Smartphone and Smartwatch Activity and Biometrics Dataset, UCI 507,
DOI 10.24432/C5HK59: LG G Watch on the dominant wrist, accelerometer and gyroscope at a nominal
20 Hz, 18 activities of 3 minutes each, 51 adult volunteers (ages not stated), no falls.
Licence as stated on the UCI page, read on 25 November 2026: Creative Commons Attribution 4.0
International, "sharing and adaptation of the datasets for any purpose". Archive SHA-256
`6e0147f181d8f5275918db65eeb9dc842a0185d37cb6b2f30cbf74ee6e205469`. Only the watch files are used;
the subject id keeps each person in one evaluation fold and identifies nobody; the data is
downloaded outside the repository and never committed.

**Acceptance criterion.** The dataset's licence permits commercial use of a model trained on it,
the attribution the licence requires is present where the model ships, the selection is justified
against the alternatives considered, and the limits of the data for IRA's users are written down.

**Alternatives considered (from `docs/DATASETS.md`).** Fall datasets (SisFall, KFall, UMAFall,
FallAllD, WEDA-FALL) are not activity datasets and their terms are unresolved or non-commercial
(F1.d to F1.g). PAMAP2 and Opportunity are chest or multi-sensor sets, not wrist-only. WISDM is the
only wrist-worn, commercially licensed set with gyroscope found.

**Known limitations.**
- Ages are not stated; the volunteers are university-age adults by the dataset description. The
  model's accuracy on older adults is unknown (F7.k and physical validation are blocked on the
  watch).
- The LG G Watch's sampling and placement differ from the IRA watch.
- Attribution: CC BY 4.0 requires the citation G. Weiss (2019), UCI Machine Learning Repository,
  doi:10.24432/C5HK59, to ship with the model; the notice file beside the model asset is where it
  lives.

**Review questions.**
1. Does the UCI page still state CC BY 4.0 (re-read it; record the date)?
2. Is the attribution notice beside the shipped model complete and will it reach the user
   (licences screen in the dashboard and the apps)?
3. Is the "ages not stated" limitation carried into the model card's intended-use section?
4. Is the selection justification sufficient, or should another wrist IMU set be evaluated
   alongside?

**Decision to record.**

| | |
|---|---|
| Reviewer | Sravani Perni ([sravaniperni](https://github.com/sravaniperni)) |
| Date | 27 November 2026 |
| Decision (accept / changes needed) | accept (recorded by the owner, Praveen Kamineti, on Sravani Perni's behalf after her review) |
| Changes needed | none recorded |
