# IRA Care Fall v2: model card

> **Superseded (25 November 2026):** v3 (`MODEL_CARD_V3.md`, D-080) passed the shipping gate that v2 failed and is the model release builds carry. This card is kept as the record of v2.

A wrist fall classifier trained only on data whose original licence permits commercial use.
It is a candidate to replace the v1 fall model, which was trained on a SisFall copy (Zenodo
22212281) that is a third-party re-upload with unconfirmed terms. The owner has excluded v1 from
commercial use, so since D-077 (#66) it is only in debug builds
(`watch/app/src/debug/assets/fall_model.tflite`) and release builds run the fall rules alone.

Register item F4. Status: **evaluated on a public dataset only, NOT shipped**. The candidate
does not pass the shipping gate (see "Verdict"). Nothing here has been run on the IRA watch.

- Training and evaluation: `scripts/training/fall_model_v2.py`
- Every number below: `docs/ai/fall/results_v2.json`
- Candidate artefacts (not in the watch assets): `docs/ai/fall/fall_model_v2.tflite`
  (18,216 bytes, SHA-256 `62e04cc0f0873321f9f247a929fce4cf032ee40626544d3b9901d3ca9462ae11`) and
  `docs/ai/fall/fall_model_v2.json` (the `FallModelSpec`)

## Verdict

- **The gate is not met.** The model may not reduce sensitivity relative to the rule detector.
  In the watch the model can only veto an impact the rule has seen. On held-out subjects it
  vetoed **5 of the 195 falls the rule caught**:
  - pipeline sensitivity: 0.6013, against 0.6171 for the rule alone;
  - no shared threshold, chosen after the fact, avoids every veto: even at 0.02, 4 falls are
    vetoed (`post_hoc_shared_threshold_sweep_not_held_out`).
- **It cuts false alarms sharply.** On the held-out daily-activity recordings, alarms fell from
  118 to 24: from 52.84 to 10.75 per hour of recorded activity.
- **It was therefore not copied into `watch/app/src/main/assets/`.** The shipped
  `fall_model.tflite` is unchanged. The owner and a reviewer decide whether the trade-off (fewer
  false alarms, five more missed falls in 316) is acceptable; see "Switching" below.

## Data, licence and provenance check (verified 2026-11-25)

### BITS-2 Dataset for Fall Detection (fall positives and daily-activity negatives)

- Record: <https://zenodo.org/records/10013090>, DOI 10.5281/zenodo.10013090, published
  2023-10-17. Read through the Zenodo API (`https://zenodo.org/api/records/10013090`).
- Creators: **Purab Nandi and K.R. Anupama**, Birla Institute of Technology and Science,
  Pilani, Goa Campus.
- Licence: **Creative Commons Attribution 4.0 International (CC BY 4.0)**, stated on the
  record. CC BY 4.0 permits commercial use with attribution.
- Provenance. The record is the authors' own release, not a re-upload, as far as the API
  shows:
  - the creators are the authors;
  - the record links to two preprints with `isDescribedBy` and `isPublishedIn`:
    10.20944/preprints202305.0917.v2 and 10.20944/preprints202306.0804.v1;
  - the same Zenodo owner account (id 573263) also published the earlier version, record
    8082667 ("BITS-2 Dataset for Fall detection", 2023-06-26, CC BY 4.0, same authors). That
    archive carries the authors' git checkout of the data; its files are byte-identical to
    this record's for the file compared.

  What could not be checked:
  - `preprints.org`, `doi.org` and `api.crossref.org` are blocked from this machine, so the
    preprints' titles and author lists were not read;
  - Zenodo does not show who owns the account.
- Contents, from the record's description:
  - a custom wrist band worn on the **left wrist** (Qualcomm Snapdragon 820c). Sensors:
    - MPU6500 (3-axis acceleration, linear acceleration and gyroscope);
    - MAX30102 (heart rate and SpO2);
    - GY273 magnetometer.
  - **41 volunteers**, performing **16 daily activities and 8 falls**, 5 trials each. The
    falls are simulated by volunteers.
  - The record does not state the participants' ages or the activity names.
- Contents, from the files: 984 csv files (41 users × 24 activities), with rows
  `t, x, y, z, axes, label`. The labels are:
  - `acc`: linear acceleration;
  - `acg`: acceleration with gravity, m/s²;
  - `gyro`, `mgm` and `hrt`.

  Only `acg` is used. It is what a watch's `TYPE_ACCELEROMETER` gives. Values are clipped
  at about ±78.4 m/s² (±8 g).
- **Sampling rate: not stated on the record, and the timestamps cannot give it directly.**
  The csv timestamps were saved with three significant figures (for example `2.92E+12`).
  Evidence gathered from the files:
  - the one file whose timestamps survived intact (`fall/user16/user16_fall3.csv`) shows 677
    consecutive intervals of 20.0 ms (median), which is **50 Hz**;
  - where whole one-second buckets of the truncated timestamps can be counted, user19 and
    user27 files show 50 samples per second;
  - **user1's files show 6 to 7 per second**, so **user1 is excluded**.

  The other files are assumed to be 50 Hz, the watch's rate, with sample order as time.
  This is an assumption, and it is the largest data risk.
- Cleaning:
  - 3,299 exact duplicate rows of 627,774 `acg` rows are dropped;
  - files with fewer than 10 unique samples are dropped (4 fall files).

  The result is 956 recordings from 40 subjects: 316 fall files and 640 daily-activity files,
  that is 2.23 h of activity.
- Archive `full_dataset.zip`, SHA-256
  `346cd2748ffaa006e775d1876a674f531430dc0e090bf0766dffb4437e137785`. The script checks it.
  The data was downloaded outside the repository and is never committed.
- Citation:
  > Nandi, P., & K.R., A. (2023). BITS-2 Dataset for Fall Detection [Data set]. Zenodo.
  > https://doi.org/10.5281/zenodo.10013090

### UCI HAR (daily-activity negatives only)

- UCI dataset 240, CC BY 4.0, as already recorded in `docs/DATASETS.md`. Archive SHA-256
  `c00b803081a5c797cd5e4b83700a9810b38d53d9d84e01917e090e1fdbc81031`.
- Content: a waist smartphone, `total_acc` in g, at 50 Hz, from 30 people.
- Preparation: the 50 %-overlapping 128-sample windows are stitched back into continuous
  signals. The overlaps match exactly. They are then cut into 5,847 windows of 4 s.
- Citation: D. Anguita, A. Ghio, L. Oneto, X. Parra, J. L. Reyes-Ortiz, "A Public Domain
  Dataset for Human Activity Recognition Using Smartphones", ESANN 2013.

### Datasets not used

- SisFall (any copy): excluded by the owner.
- KFall, FallAllD, UMAFall and MobiFall: excluded because their terms are unclear, research
  only or conflicting (`docs/DATASETS.md`).

## The watch's input contract (unchanged, drop-in)

The model was built to match the existing Kotlin, so no Kotlin change is needed:
`FallModelSpec`, `SampleWindow` and `ModelFallDetector` in
`watch/app/src/main/java/care/ira/watch/sensors/FallModel.kt`, and `TfLiteInference.kt`.

- Input `[1, 200, 4]` float32: x, y, z and |a| in g, at 50 Hz (4 s).
- Normalisation `(v - mean[c]) / std[c]`, with the values in the spec json.
- Output `[1, 1]`: the sigmoid P(fall).
- The spec json has the same keys as the shipped one (`threshold`, `mean`, `std`,
  `windowSamples`, `rateHz`, plus informational keys that `ignoreUnknownKeys` skips).
- Window alignment matches the runtime. `ModelFallDetector` asks the model once the rule's
  impact (the first sample ≥ 2.5 g) is `AFTER_IMPACT_MS` = 2 s old, and `SampleWindow`
  holds the last 200 samples. The impact is therefore at index 100, and v2 is trained with
  the impact there (±10 samples of jitter).
  - Note: the shipped v1 script (`scripts/training/kaggle_fall_model.py`) placed the impact
    at index 66 (`WIN // 3`). The Kotlin comment says "about a third of the way in", but the
    code delivers index 100. v1 therefore sees impacts later in the window than it was
    trained on.

## Method

- **Windows.**
  - *Falls.* One window per fall file. It is aligned on the rule impact whose 1.5 s settle
    contains the file's largest magnitude. 59 of the 316 falls never reach 2.5 g: the rule
    gate cannot fire, so they are missed by both detectors. Their model window is aligned on
    the maximum. Other impacts inside fall files (28) have unknown labels (getting up, or a
    later trial) and are left out.
  - *Daily activities.* Every rule impact in an activity file is a hard negative (1,075). In
    addition, plain windows are taken every 2 s (3,104).
  - *Padding.* Windows that run past the start or the end of a recording are edge-padded:
    98 of the fall windows and 210 of the activity-impact windows.
- **Model.** The same small 1-D CNN as v1: Conv 16/32/32, global max pool, dense 16, sigmoid.
  It has 6,705 parameters and was trained with Adam for 25 epochs, using class weights.
- **Augmentation.** A random 3-D rotation of every training window: the research band's
  axes are not the watch's. The impact is shifted by up to ±10 samples. Each fall gets 12
  copies and each activity impact gets 4.
- **Subject-independent evaluation.**
  - 5 folds over the 40 BITS-2 subjects (8 test subjects per fold). The 30 UCI HAR subjects
    are folded separately.
  - Each fold's threshold is chosen on validation subjects taken from that fold's training
    subjects. It is the highest threshold that keeps every validation fall the rule caught,
    and it is never chosen on the test fold.
  - The fold thresholds were 0.12, 0.11, 0.64, 0.05 and 0.05. The spread shows how few
    validation falls there are per fold.
  - The final model is trained on all 40 subjects with the median threshold, 0.11.
- **Rule detector.** `FallDetector.feed` is re-implemented line for line in Python
  (`rule_events`):
  - impact ≥ 2.5 g;
  - a 1.5 s settle, which tracks the peak;
  - then |g − 1| > 0.3 counts as moving, and more than 3 moving samples clears the impact;
  - a fall is declared after 20 s;
  - samples are taken as 20 ms apart.

  It is scored two ways:
  - on the 4 s window alone (`rule_on_window`, as the v1 script did);
  - on the rest of the recording, up to 20 s (event level).
- **Event level.** This is the watch's pipeline: the rule impact, then the model at +2 s,
  then the rule's stillness check. Most recordings end before 20 s: 170 of the falls and
  117 of the 118 activity alarms. The rule had not cleared these impacts (the person had
  not moved), and they are **counted as alarms for both detectors**. This is pessimistic
  for daily activity, where a longer recording would often clear the impact.
- Determinism: TensorFlow op determinism is on and every seed is fixed. Two complete runs
  gave identical results and a byte-identical tflite. Two earlier runs without op
  determinism gave these results:

  | Run | Pipeline sensitivity | Vetoed falls | Activity alarms |
  | --- | --- | --- | --- |
  | 1 | 0.5886 | 9 | 17 |
  | 2 | 0.5949 | 7 | 24 |

  The conclusion is the same in every run.

## Results (held-out subjects, out-of-fold)

### Event level: the watch pipeline against the rule detector

316 falls and 1,075 activity impacts, from 40 subjects.

| | Rule detector (today) | Rule + model v2 |
| --- | --- | --- |
| Sensitivity (recall) | 0.6171 (195/316) | 0.6013 (190/316) |
| Missed falls | 121 | 126 (5 vetoed by the model) |
| Specificity (over impacts) | 0.8902 | 0.9777 |
| Precision | 0.6230 | 0.8879 |
| F1 | 0.6200 | 0.7170 |
| Alarms on daily activity | 118, 52.84 per hour | 24, 10.75 per hour |
| False alarms per day (assumption below) | 845.45 | 171.96 |

- **False-alarm assumption.** The alarms on 2.23 h of held-out BITS-2 activity recordings
  are scaled to 16 waking hours a day *of the same activity mix*. That mix is dominated by
  short, impact-heavy activities: activities 3, 4 and 12 alone give 940 of the 1,075
  impacts. It is far denser than an older person's day.
  - The per-day figure is a pessimistic number for this protocol, not a prediction of
    real-world false alarms.
  - The per-hour numbers and the ratio (about 5× fewer) are the meaningful comparison.
- **The missed falls** are mostly not the model's doing:
  - 59 falls never reach 2.5 g at the wrist;
  - the rule clears 62 because the wrist moves within 20 s (or the next trial starts in the
    same file);
  - the model vetoes 5.

  Per fall type, for the rule and then for rule + model:

  | Fall type | Rule | Rule + model |
  | --- | --- | --- |
  | fall1 | 27/39 | 27/39 |
  | fall2 | 23/40 | 23/40 |
  | fall3 | 25/39 | 24/39 |
  | fall4 | 27/39 | 25/39 |
  | fall5 | 20/40 | 20/40 |
  | fall6 | 26/39 | 26/39 |
  | fall7 | 16/40 | 15/40 |
  | fall8 | 31/40 | 30/40 |

### Window level: the model alone, at each fold's threshold

| | Model v2 alone | Rule on the 4 s window |
| --- | --- | --- |
| Sensitivity (recall) | 0.9304 (294/316) | 0.0000 |
| Specificity | 0.9581 | 0.9981 |
| Precision | 0.8673 | 0.0000 |
| F1 | 0.8977 | 0.0000 |

- **The rule cannot be judged on a 4 s window.** With the impact at index 100, it sees only
  0.5 s after its settle, and the wrist is rarely that still. This column shows that a
  window-only comparison is meaningless; the event level above is the comparison that
  counts.
- **Randomly rotated test windows** (a different orientation from training): sensitivity
  0.9399, specificity 0.9535.
- **Other negatives:**
  - plain activity windows flagged by the model: 255/3,104;
  - UCI HAR windows flagged by the model: 28/5,847;
  - the rule on the UCI HAR windows: 0/5,847.

### Dropped watch and off-wrist (synthetic, never trained on)

BITS-2 has no drops, so 50 synthetic table drops were generated: 0.3 s of free fall, an impact
of 3 to 8.4 g, then motionless at 1 g, in a random orientation.

- The rule detector alarms on **50/50** of them.
- The rule + model pipeline alarms on **6/50**.
- 50 motionless "not worn" windows: the model flags **0/50**. The rule gate never fires on
  them anyway.
- Off the wrist, the watch's `onWrist` flag stops both detectors. These numbers apply when
  that flag is unknown.
- They are synthetic signals and say nothing about real drops.

### Size and latency

- tflite: **18,216 bytes**, dynamic-range quantised, 6,705 parameters. It agrees with Keras
  on 200/200 windows.
- Latency: a median of **0.009 ms** and a p95 of 0.016 ms per window, with one thread, on
  this build machine's x86 CPU.
- A watch CPU is slower, and **its latency was not measured**.

## Limitations

- **Population.**
  - Volunteers simulated the falls. The record does not state their ages; the authors are at
    a university.
  - These are **not older adults** and **not real falls**.
- **Device.**
  - The data comes from a custom research band (MPU6500, ±8 g, left wrist). It is **not the
    IRA watch**, and not a Wear OS sensor.
  - The axes differ from the watch's, which rotation augmentation only approximates.
  - Inputs above 8 g on the watch are outside the training range.
- **Timing.**
  - The sampling rate is inferred, not documented; see above. If some files are not 50 Hz,
    their windows are time-stretched.
  - The 20 s stillness rule could rarely be tested in full, because most recordings are
    shorter than 20 s.
- **Labels.**
  - A fall file can hold several trials, and only the largest impact is labelled as the fall.
  - The activity names are not given on the record.
- **Small evaluation.**
  - 316 falls from 40 people. The fold thresholds vary from 0.05 to 0.64.
  - The final threshold, 0.11, is low; at that operating point the model contributes by
    rejecting impacts that look clearly unlike a fall.
- **Nothing here is a medical claim.** Accuracy on the IRA watch with older adults is
  **needs external**: consented recordings on the device.

## Switching (if the owner and a reviewer accept the trade-off)

The model is not switched in this change. Option 1 needs no Kotlin change:

1. Replace the asset pair under the existing names:
   - copy `docs/ai/fall/fall_model_v2.tflite` to `watch/app/src/main/assets/fall_model.tflite`;
   - copy `docs/ai/fall/fall_model_v2.json` to `watch/app/src/main/assets/fall_model.json`.

   `TfLiteInference.load` reads exactly these two names. `FallModelReleaseTest` fails while a
   model is in `src/main/assets`, on purpose: switching means changing that test in the same
   change, with the owner's and a clinical reviewer's decision recorded in `docs/DECISIONS.md`.
2. Alternatively, keep both pairs in the assets (`fall_model_v2.tflite` and
   `fall_model_v2.json`) and change `MODEL_ASSET` and `SPEC_ASSET` in `TfLiteInference.kt`.
   This is a two-constant Kotlin change.

Either way:
- Add the BITS-2 and UCI HAR attribution (CC BY 4.0 requires it) to the app's licence notices.
- Re-run the watch's unit tests.

Until a decision is made, the owner's exclusion of v1 still stands. Without either model, the
watch falls back to the rule detector alone. This is already what happens when the asset is
missing.

## Commands

```bash
pip install tensorflow-cpu numpy        # TensorFlow 2.21.0 was used
mkdir -p /path/outside/repo/fall2
python3 -I scripts/training/fall_model_v2.py --data-dir /path/outside/repo/fall2 \
    --out /path/outside/repo/fall2/out --epochs 25 --folds 5
# about 6 minutes on 4 CPU cores; writes results_v2.json, fall_model_v2.{tflite,json,keras}
```
