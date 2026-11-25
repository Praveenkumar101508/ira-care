# IRA Care Activity: model card

What the wrist is doing, from one 10-second window of the watch's accelerometer and
gyroscope. Register item F7. Status: **evaluated on a public dataset only**. Nothing here
has been run on the IRA watch.

- Inference path: `supracloud-jarvis/ira/care/activity_estimate.py`. It uses numpy only,
  and reads its weights from JSON. Nothing is unpickled.
- Weights: `supracloud-jarvis/ira/care/models/activity_model.json` (242,171 bytes).
- Training and evaluation: `scripts/training/activity_wisdm.py`.
- Results, every number below: `docs/ai/activity/results.json`.
- Tests (synthetic signals only): `supracloud-jarvis/ira/tests/care/test_activity_estimate.py`.

## Intended use

- **A wellbeing context signal only.** Examples: "mostly sitting this afternoon", "walked
  after lunch", or "a meal-like wrist pattern around 1 pm" as context for a carer's daily
  summary.
- **Not a medical device.** It is not for diagnosis, it does not measure food or fluid
  intake, it does not measure frailty or mobility decline, and it is not a fall detector.
- **Never an alarm on its own.** No alert, escalation or SOS may be raised because of this
  estimate alone. At the time of writing, nothing in the backend calls this module.
- **Not for identifying people.** The dataset also supports biometrics (it tags every
  reading with a subject id). That is a stated non-goal. The subject id is used only to keep
  each person's windows in one cross-validation fold. No model here predicts or uses who
  someone is.
- The function returns **no estimate** (`activity=None`, with a `reason`) when:
  - the input has the wrong shape, or a sampling rate that is not positive and finite;
  - it contains NaN or infinite values;
  - it is shorter than 8 s (`too_short`);
  - either sensor is flat on every axis, which means it is stuck (`flat_accelerometer`,
    `flat_gyroscope`);
  - either sensor is saturated: more than 10% of one axis's samples sit exactly on that
    axis's extreme, and the extreme is at least 2 g (accelerometer) or 5 rad/s (gyroscope);
  - the mean accelerometer magnitude is outside 4 to 50 m/s² (`implausible_gravity`),
    which catches a watch reporting in g or in milli-g.
- It returns **`"unknown"`** (with `reason="low_confidence"` and the top class as
  `candidate`) when the top class probability is below 0.5.

Callers must show "no reading" or "not sure" in those cases and must not fill in a guess.

## On the watch

The watch runs the same model, read from the same JSON (`watch/app/src/main/assets/activity_model.json`,
byte for byte the backend's file, with its CC BY notice beside it):

- `care.ira.watch.activity.ActivityModel` and `ActivityFeatures` port `care/activity_estimate.py`:
  the same refusals, 20 Hz resampling, 121 features and softmax. `ActivityParityTest` reproduces
  the Python reference on a fixture it wrote (`scripts/training/activity_watch_fixture.py`):
  every feature and probability within 1e-6, every answer and refusal reason identical.
- `ActivityMonitor` takes the accelerometer (the fall detector's source, in g) and the gyroscope
  (20 Hz), interpolates both onto one 20 Hz grid by timestamp, and classifies every 10 s.
  Off the wrist, or with a gap of more than 0.5 s in either sensor, there is no estimate.
- **The estimate stays on the watch.** The backend has no reading kind for it, nothing is sent,
  and nothing alerts on it (`ActivityMonitorTest.theEstimateStaysOnTheWatch`).
- Not measured: battery cost of the 20 Hz gyroscope, and accuracy on the IRA watch or with
  older adults.

## Data and licence

- **WISDM Smartphone and Smartwatch Activity and Biometrics Dataset**, UCI Machine Learning
  Repository dataset 507,
  <https://archive.ics.uci.edu/dataset/507/wisdm+smartphone+and+smartwatch+activity+and+biometrics+dataset>,
  DOI 10.24432/C5HK59.
  - Licence: **Creative Commons Attribution 4.0 International (CC BY 4.0)**. Checked at
    the source on **2026-11-25**: the UCI page says "This dataset is licensed under a
    Creative Commons Attribution 4.0 International (CC BY 4.0) license. This allows for the
    sharing and adaptation of the datasets for any purpose", and links to
    `creativecommons.org/licenses/by/4.0/legalcode`. Commercial use is allowed with
    attribution.
  - Archive `wisdm+smartphone+and+smartwatch+activity+and+biometrics+dataset.zip`,
    SHA-256 `6e0147f181d8f5275918db65eeb9dc842a0185d37cb6b2f30cbf74ee6e205469`. The script
    checks this checksum.
  - The data is downloaded outside the repository and is never committed.
- Citation, as the UCI page gives it:
  > Weiss, G. (2019). WISDM Smartphone and Smartwatch Activity and Biometrics Dataset
  > [Dataset]. UCI Machine Learning Repository. https://doi.org/10.24432/C5HK59.
- Contents, confirmed from the dataset's `README.txt`, its description PDF
  (`WISDM-dataset-description.pdf`) and the files themselves:
  - 51 subjects (ids 1600 to 1650). Each performed 18 activities for 3 minutes.
  - An **LG G Watch** (Android Wear 1.5) on the **dominant** wrist, and a phone in the
    pocket.
  - Accelerometer in m/s², gyroscope in rad/s, at a nominal 20 Hz.
  - The description gives no ages or other demographics. The subjects are described only
    as subjects of a university lab's study.
- **Only the watch files are read.** The phone accelerometer and gyroscope are never
  opened.

### Data handling

- The data is untrusted. The script streams each watch file out of the nested zip, with
  no extraction, and parses it as text. It refuses any line that is not
  `subject,activity,timestamp,x,y,z;` for the expected subject and a known activity code.
  The per-subject cache is `.npz`, loaded with `allow_pickle=False`. Python is run with
  `-I`.
- What the files actually contain, and how the script deals with it:
  - **Duplicate rows.** Some blocks were recorded twice: the same timestamps and values
    appear twice in a row. Exact duplicate timestamps are dropped. This removed 129,773
    rows.
  - **Different rates.** Of the 907 subject-and-activity blocks, 835 are at 20 Hz, 65 at
    50 Hz and 7 at 100 Hz. Both sensors are resampled onto one 20 Hz grid by timestamp,
    using linear interpolation.
  - **Gaps.** 18 windows with a gap over 0.25 s in either sensor were dropped.
  - **Clipping.** Many recordings clip at about ±19.6 m/s² (±2 g); some go up to about
    78 m/s². The input checks refused 129 windows as `saturated_accelerometer`. These are
    mostly jogging, dribbling and other vigorous activities, which the care classes treat as
    "other" anyway.
  - **Duplicate subjects.** No two subject ids share an identical watch recording. The
    script checks for this and would keep such subjects in the same fold.
- Windows: non-overlapping 10 s windows (200 samples at 20 Hz), cut per subject and
  activity. This is the same window length the dataset authors use. The result is
  **15,304 windows** from all 51 subjects.

## Classes, and why these

The model outputs six classes, plus "unknown" and "no estimate".

| Care class | WISDM activities | Windows | Why it is a class |
| --- | --- | --- | --- |
| walking | walking | 870 | The main mobility signal for an older person's day. |
| stairs | stairs | 815 | Also mobility, and stairs are a known risk point. Kept separate from walking so a carer can see it, though it is often confused with walking. |
| sitting | sitting | 868 | The sedentary signal: long sitting is the most common care question about the day. |
| standing | standing | 850 | Separates still and upright from still and seated. |
| eating_drinking | eating soup, chips, pasta, a sandwich; drinking from a cup | 4,286 | Meal-time and drinking context. It is a hint for a "did you have lunch?" check-in, not a measure of intake. |
| other | jogging, typing, brushing teeth, kicking a ball, playing catch, dribbling, writing, clapping, folding clothes | 7,615 | Everything the watch sees that is none of the above. |

- Jogging and the ball sports are implausible for most of IRA CARE's users. Naming them
  would spend the model's capacity on movements that matter little here. They stay in
  training as "other", so the model learns to say "other" for vigorous motion rather than
  forcing it into walking.
- Typing, writing, brushing teeth, folding clothes and clapping are hand activities. They
  could be "sitting" or "standing" by posture, but the wrist sees the hands, not the
  posture. Labelling them by posture would teach the model that busy hands mean sitting.
  They are "other".
- **Unknown.** The model has an explicit "not confident" output: when the top probability
  is below 0.5, the answer is `"unknown"`. The 0.5 was set before the evaluation was run
  and was not tuned. As the coverage table shows, on WISDM the MLP is rarely below it, so
  "unknown" is a rare answer. See the limitations.

## Method

### Features (`care/activity_estimate.py`, `window_features`)

121 hand-made features per window:

- For each of 8 signals (the accelerometer x, y, z and magnitude, and the gyroscope x, y,
  z and magnitude), 14 features:
  - mean, standard deviation, 10th, 50th and 90th percentiles;
  - mean absolute first difference;
  - log power in 0.3–1, 1–2, 2–3, 3–5 and 5–10 Hz (Hann window, mean removed);
  - dominant frequency in 0.3–5 Hz;
  - normalised spectral entropy;
  - the highest normalised autocorrelation at lags 0.3–2 s (periodicity).
- The gravity direction: the unit vector of the mean accelerometer reading.
- The pairwise correlations of the accelerometer axes and of the gyroscope axes.

The training script calls the same function, so training and inference cannot drift
apart.

### Models compared

All three use the same folds and the same windows.

1. **Majority class (trivial baseline).** It always predicts the training folds' most
   common class, which is "other".
2. **Logistic regression (simple baseline).** Standardised features, then scikit-learn
   `LogisticRegression` with `C=1.0`, `class_weight="balanced"` and `max_iter=3000`.
3. **MLP (shipped).** Standardised features, then a hidden layer of 64 and a hidden layer
   of 32 (ReLU), then a 6-class softmax. It is a scikit-learn `MLPClassifier` with
   `alpha=1e-3`, `learning_rate_init=1e-3`, `batch_size=256`, `max_iter=200` and
   `random_state=20261125`.
   - The training windows are balanced by oversampling every class, with replacement and
     a seeded generator, up to the size of the largest class.
   - The settings were chosen once and not tuned on the evaluation.
   - The model has 10,328 parameters, counting the standardisation's 242.

### Export, and the check that the JSON is the model

- `export_mlp` writes the standardisation mean and scale, each layer's `W` and `b`, the
  class order and the feature names to JSON. Python's float repr is used, so the values
  are exact.
- `ActivityModel` in the shipped module refuses:
  - a file in another format;
  - feature names that differ from the module's;
  - wrong layer shapes;
  - a scale that is not positive;
  - non-finite weights.
- **Per fold, on held-out windows:** each fold's MLP is exported to JSON, read back
  through the shipped numpy evaluator, and compared with scikit-learn's `predict_proba`
  on that fold's held-out windows. The script stops if any probability differs by more
  than 1e-6 or any top class differs. The largest difference in every fold was **0.0**,
  and the agreement was 100%.
- **Shipped model:** the same comparison on all 15,304 windows also gave a largest
  difference of **0.0**.
- The JSON also carries 12 **synthetic** reference windows (made by the script, with no
  dataset content), with their features and scikit-learn's probabilities. The unit test
  checks the shipped evaluator against them to 1e-9.

### Evaluation protocol

- **Participant-independent:** 10-fold cross-validation grouped by subject.
  - A seeded permutation of the 51 subjects is dealt into 10 folds of 5 or 6 subjects.
  - Every subject is tested exactly once, by models that never saw any of their windows.
- Metrics are over the pooled out-of-fold predictions, using the top class (forced, with
  no "unknown"). The coverage table then shows what the 0.5 threshold does.

### Seeds and reproducibility

- `SEED = 20261125` is used for `random`, numpy, the fold permutation, the oversampling
  and the classifiers.
- BLAS runs with one thread per process, and the 10 folds run in 4 parallel processes.
  This keeps the arithmetic order fixed.
- **Two complete runs** gave:
  - identical `results.json`, apart from the timings;
  - a **byte-identical** model file, SHA-256
    `06475f27740a2bc9ba7e736333966c94b2325a27c78f2bc9ce36b1395462c445`.

## Commands run

```bash
# scikit-learn 1.9.1, numpy 2.5.5, Python 3.13.18, x86_64, 4 CPUs, no GPU
DATA_DIR=/tmp/<scratch>/wisdm            # outside the repository; SCRATCH likewise
# run 1, to a scratch location
python3 -I scripts/training/activity_wisdm.py --data-dir "$DATA_DIR" \
    --out "$SCRATCH/run1/results.json" --model-out "$SCRATCH/run1/activity_model.json"
# run 2, into the repository
python3 -I scripts/training/activity_wisdm.py --data-dir "$DATA_DIR" \
    --out docs/ai/activity/results.json \
    --model-out supracloud-jarvis/ira/care/models/activity_model.json
# then: results.json compared section by section (all equal apart from the timings),
# and the two model files compared byte for byte (identical)
cd supracloud-jarvis/ira && ruff check . && python3 -m pytest tests/care/test_activity_estimate.py tests/readiness -q
```

- `$DATA_DIR` was a scratch directory outside the repository, holding the archive and the
  per-subject cache. The commands are otherwise exactly as run.
- The committed run took 94 s in total:
  - 1.2 s to load the cached data;
  - 3.3 s to cut the windows;
  - 65.7 s for the cross-validation;
  - 22.4 s to train the final model.
- On top of that came the one-off 310 MB download and a per-subject cache build.
- An earlier version of the script ran each fold's training with multi-threaded BLAS, one
  fold after another. It was about ten times slower, and it was stopped after 7 folds. None of its numbers
  are used here.

## Results: 10-fold grouped by subject (15,304 windows, 51 subjects)

### Overall

| Method | Macro-F1 (pooled) | Macro-F1, mean ± sd of folds | Accuracy |
| --- | --- | --- | --- |
| Majority class ("other") | 0.111 | 0.111 ± 0.001 | 0.498 |
| Features + logistic regression | 0.740 | 0.746 ± 0.060 | 0.818 |
| **MLP (shipped)** | **0.790** | **0.796 ± 0.056** | **0.866** |

### Per class (pooled out-of-fold)

| Class | Support | Majority P / R / F1 | Logistic regression P / R / F1 | **MLP P / R / F1** |
| --- | --- | --- | --- | --- |
| walking | 870 | 0 / 0 / 0 | 0.831 / 0.870 / 0.850 | 0.855 / 0.862 / **0.859** |
| stairs | 815 | 0 / 0 / 0 | 0.644 / 0.860 / 0.736 | 0.814 / 0.815 / **0.814** |
| sitting | 868 | 0 / 0 / 0 | 0.397 / 0.705 / 0.508 | 0.522 / 0.629 / **0.570** |
| standing | 850 | 0 / 0 / 0 | 0.484 / 0.784 / 0.598 | 0.628 / 0.781 / **0.696** |
| eating_drinking | 4286 | 0 / 0 / 0 | 0.917 / 0.797 / 0.852 | 0.899 / 0.853 / **0.875** |
| other | 7615 | 0.498 / 1.000 / 0.665 | 0.957 / 0.837 / 0.893 | 0.937 / 0.915 / **0.926** |

### Confusion matrix, MLP (rows: true; columns: predicted)

| | walking | stairs | sitting | standing | eating_drinking | other |
| --- | --- | --- | --- | --- | --- | --- |
| walking | **750** | 47 | 1 | 1 | 1 | 70 |
| stairs | 66 | **664** | 1 | 1 | 5 | 78 |
| sitting | 0 | 2 | **546** | 61 | 138 | 121 |
| standing | 0 | 3 | 57 | **664** | 70 | 56 |
| eating_drinking | 2 | 11 | 265 | 206 | **3655** | 147 |
| other | 59 | 89 | 177 | 125 | 195 | **6970** |

### Confusion matrix, logistic regression (rows: true; columns: predicted)

| | walking | stairs | sitting | standing | eating_drinking | other |
| --- | --- | --- | --- | --- | --- | --- |
| walking | **757** | 66 | 2 | 0 | 3 | 42 |
| stairs | 47 | **701** | 2 | 0 | 0 | 65 |
| sitting | 2 | 2 | **612** | 114 | 96 | 42 |
| standing | 1 | 1 | 94 | **666** | 53 | 35 |
| eating_drinking | 7 | 15 | 430 | 317 | **3414** | 103 |
| other | 97 | 304 | 403 | 280 | 159 | **6372** |

The majority baseline predicts "other" for all 15,304 windows.

What the matrices show:

- The hard part is **still postures against eating.** A seated person eating and a seated
  person resting look alike to a wrist between bites.
  - 265 eating windows were read as sitting, and 206 as standing.
  - 138 sitting windows were read as eating.
- Walking and stairs are confused with each other in both directions.

### Per fold

| Fold | Test subjects | Test windows | Majority | Logistic regression | MLP |
| --- | --- | --- | --- | --- | --- |
| 0 | 1613, 1624, 1627, 1633, 1642, 1644 | 1778 | 0.110 | 0.729 | 0.754 |
| 1 | 1604, 1615, 1635, 1640, 1646 | 1494 | 0.113 | 0.701 | 0.773 |
| 2 | 1609, 1619, 1622, 1639, 1649 | 1495 | 0.111 | 0.699 | 0.766 |
| 3 | 1606, 1608, 1614, 1617, 1625 | 1500 | 0.110 | 0.812 | 0.858 |
| 4 | 1605, 1610, 1618, 1628, 1641 | 1517 | 0.110 | 0.768 | 0.818 |
| 5 | 1602, 1616, 1626, 1634, 1643 | 1506 | 0.110 | 0.855 | 0.894 |
| 6 | 1601, 1607, 1621, 1629, 1631 | 1530 | 0.111 | 0.718 | 0.769 |
| 7 | 1600, 1603, 1637, 1648, 1650 | 1474 | 0.112 | 0.691 | 0.710 |
| 8 | 1630, 1636, 1638, 1645, 1647 | 1480 | 0.110 | 0.670 | 0.753 |
| 9 | 1611, 1612, 1620, 1623, 1632 | 1530 | 0.111 | 0.820 | 0.861 |

All values in the last three columns are macro-F1. The MLP beats logistic regression in
every fold.

### Per subject

Each subject's macro-F1 is computed over the classes that subject actually has windows for.
Subjects 1637 and 1642 have no stairs windows; 1640 has neither stairs nor standing.

- MLP: mean **0.795 ± 0.160**, from a minimum of 0.154 to a maximum of 0.988.
- The MLP beats logistic regression for 42 of 51 subjects.
- **5 of 51 subjects are below 0.6:**
  - 1637 (0.154)
  - 1639 (0.415)
  - 1640 (0.458)
  - 1607 (0.473)
  - 1638 (0.542)
- Subject 1637's jogging recording has the highest median acceleration magnitude of any
  subject (23.7 m/s², against a median across subjects of 13.1). The watch may have been
  worn or set up differently. This was not investigated further.
- Per-person spread like this is exactly why the IRA watch needs its own validation (see
  the limitations).

| Subject | Fold | Windows | Classes performed | Logistic regression macro-F1 | MLP macro-F1 | MLP accuracy |
| --- | --- | --- | --- | --- | --- | --- |
| 1600 | 7 | 306 | 6 | 0.928 | 0.969 | 0.977 |
| 1601 | 6 | 306 | 6 | 0.757 | 0.868 | 0.908 |
| 1602 | 5 | 304 | 6 | 0.877 | 0.904 | 0.941 |
| 1603 | 7 | 294 | 6 | 0.743 | 0.715 | 0.837 |
| 1604 | 1 | 305 | 6 | 0.739 | 0.864 | 0.915 |
| 1605 | 4 | 312 | 6 | 0.791 | 0.849 | 0.933 |
| 1606 | 3 | 304 | 6 | 0.846 | 0.921 | 0.947 |
| 1607 | 6 | 308 | 6 | 0.438 | 0.473 | 0.643 |
| 1608 | 3 | 306 | 6 | 0.786 | 0.857 | 0.882 |
| 1609 | 2 | 306 | 6 | 0.660 | 0.765 | 0.830 |
| 1610 | 4 | 306 | 6 | 0.882 | 0.917 | 0.964 |
| 1611 | 9 | 306 | 6 | 0.892 | 0.926 | 0.951 |
| 1612 | 9 | 306 | 6 | 0.890 | 0.825 | 0.860 |
| 1613 | 0 | 308 | 6 | 0.688 | 0.709 | 0.880 |
| 1614 | 3 | 290 | 6 | 0.884 | 0.886 | 0.938 |
| 1615 | 1 | 305 | 6 | 0.834 | 0.923 | 0.951 |
| 1616 | 5 | 289 | 6 | 0.891 | 0.901 | 0.927 |
| 1617 | 3 | 295 | 6 | 0.704 | 0.788 | 0.824 |
| 1618 | 4 | 289 | 6 | 0.822 | 0.791 | 0.872 |
| 1619 | 2 | 291 | 6 | 0.861 | 0.846 | 0.928 |
| 1620 | 9 | 306 | 6 | 0.640 | 0.647 | 0.833 |
| 1621 | 6 | 304 | 6 | 0.733 | 0.788 | 0.901 |
| 1622 | 2 | 306 | 6 | 0.657 | 0.856 | 0.905 |
| 1623 | 9 | 306 | 6 | 0.880 | 0.988 | 0.984 |
| 1624 | 0 | 306 | 6 | 0.612 | 0.639 | 0.801 |
| 1625 | 3 | 305 | 6 | 0.860 | 0.868 | 0.928 |
| 1626 | 5 | 306 | 6 | 0.873 | 0.959 | 0.974 |
| 1627 | 0 | 306 | 6 | 0.848 | 0.903 | 0.938 |
| 1628 | 4 | 304 | 6 | 0.532 | 0.610 | 0.757 |
| 1629 | 6 | 306 | 6 | 0.797 | 0.809 | 0.915 |
| 1630 | 8 | 306 | 6 | 0.845 | 0.920 | 0.954 |
| 1631 | 6 | 306 | 6 | 0.901 | 0.918 | 0.958 |
| 1632 | 9 | 306 | 6 | 0.756 | 0.878 | 0.948 |
| 1633 | 0 | 306 | 6 | 0.704 | 0.706 | 0.807 |
| 1634 | 5 | 306 | 6 | 0.828 | 0.815 | 0.899 |
| 1635 | 1 | 306 | 6 | 0.852 | 0.889 | 0.944 |
| 1636 | 8 | 306 | 6 | 0.723 | 0.769 | 0.902 |
| 1637 | 7 | 272 | 5 | 0.214 | 0.153 | 0.338 |
| 1638 | 8 | 272 | 6 | 0.397 | 0.542 | 0.500 |
| 1639 | 2 | 289 | 6 | 0.516 | 0.415 | 0.557 |
| 1640 | 1 | 272 | 4 | 0.518 | 0.458 | 0.474 |
| 1641 | 4 | 306 | 6 | 0.849 | 0.909 | 0.951 |
| 1642 | 0 | 258 | 5 | 0.762 | 0.830 | 0.822 |
| 1643 | 5 | 301 | 6 | 0.810 | 0.886 | 0.944 |
| 1644 | 0 | 294 | 6 | 0.757 | 0.692 | 0.833 |
| 1645 | 8 | 290 | 6 | 0.816 | 0.860 | 0.921 |
| 1646 | 1 | 306 | 6 | 0.751 | 0.835 | 0.915 |
| 1647 | 8 | 306 | 6 | 0.621 | 0.717 | 0.771 |
| 1648 | 7 | 300 | 6 | 0.657 | 0.736 | 0.817 |
| 1649 | 2 | 303 | 6 | 0.905 | 0.925 | 0.960 |
| 1650 | 7 | 302 | 6 | 0.864 | 0.920 | 0.940 |

### Where each of the 18 original activities goes (MLP, pooled out-of-fold)

Each cell is the share of that activity's windows given each care class. Bold marks the
correct care class.

| WISDM activity | Care class | Windows | walking | stairs | sitting | standing | eating_drinking | other |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| walking | walking | 870 | **0.862** | 0.054 | 0.001 | 0.001 | 0.001 | 0.080 |
| stairs | stairs | 815 | 0.081 | **0.815** | 0.001 | 0.001 | 0.006 | 0.096 |
| sitting | sitting | 868 | 0.000 | 0.002 | **0.629** | 0.070 | 0.159 | 0.139 |
| standing | standing | 850 | 0.000 | 0.004 | 0.067 | **0.781** | 0.082 | 0.066 |
| eating soup | eating_drinking | 851 | 0.000 | 0.001 | 0.031 | 0.041 | **0.907** | 0.020 |
| eating chips | eating_drinking | 867 | 0.001 | 0.000 | 0.066 | 0.047 | **0.857** | 0.029 |
| eating pasta | eating_drinking | 851 | 0.000 | 0.000 | 0.063 | 0.015 | **0.844** | 0.078 |
| drinking | eating_drinking | 867 | 0.000 | 0.000 | 0.089 | 0.054 | **0.837** | 0.020 |
| eating sandwich | eating_drinking | 850 | 0.001 | 0.012 | 0.060 | 0.082 | **0.819** | 0.026 |
| jogging | other | 741 | 0.011 | 0.018 | 0.000 | 0.000 | 0.001 | **0.970** |
| typing | other | 850 | 0.000 | 0.000 | 0.078 | 0.019 | 0.049 | **0.854** |
| brushing teeth | other | 867 | 0.006 | 0.001 | 0.022 | 0.036 | 0.036 | **0.900** |
| kicking | other | 868 | 0.013 | 0.032 | 0.002 | 0.010 | 0.003 | **0.939** |
| catch | other | 824 | 0.010 | 0.011 | 0.000 | 0.001 | 0.002 | **0.976** |
| dribbling | other | 863 | 0.021 | 0.023 | 0.003 | 0.007 | 0.012 | **0.934** |
| writing | other | 867 | 0.000 | 0.000 | 0.067 | 0.029 | 0.077 | **0.827** |
| clapping | other | 867 | 0.005 | 0.007 | 0.021 | 0.030 | 0.018 | **0.919** |
| folding clothes | other | 868 | 0.006 | 0.014 | 0.013 | 0.013 | 0.026 | **0.929** |

Jogging has fewer windows (741) because the input checks refused clipped windows (see
"Data handling").

### Confidence against coverage (MLP, pooled out-of-fold)

When the top probability is below the threshold, the answer is "unknown".

| Threshold | Share answered | Accuracy when answered | Macro-F1 when answered |
| --- | --- | --- | --- |
| 0.0 | 100% | 0.8657 | 0.7901 |
| 0.4 | 99.97% | 0.8659 | 0.7902 |
| **0.5 (shipped)** | **99.87%** | **0.8665** | **0.7908** |
| 0.6 | 98.84% | 0.8717 | 0.7971 |
| 0.7 | 97.76% | 0.8764 | 0.8024 |
| 0.8 | 96.55% | 0.8815 | 0.8078 |
| 0.9 | 94.60% | 0.8898 | 0.8175 |

**The MLP is overconfident.** Even at 0.9 it answers 94.6% of windows, and accuracy only
rises from 0.8657 to 0.8898. The threshold therefore filters little. "Unknown" is a
safeguard, not a calibrated uncertainty. Calibration on IRA watch data is listed below as
still to do.

### Runtime and size

| | Logistic regression (baseline, not shipped) | **MLP (shipped)** |
| --- | --- | --- |
| Parameters | 6 × 121 + 6, plus the scaler | 10,328, including the scaler |
| Model file | not exported | `activity_model.json`: 242,171 bytes of JSON, including 12 reference windows (the repository's other small model file, `classifier_heads.json`, is 112,493 bytes) |
| Inference | not timed | 1.13 ms per 10 s window for the input checks, features and MLP together; 0.041 ms for the MLP alone |
| Training | 1.9 to 2.5 s per fold | 17.2 to 21.9 s per fold; 22.4 s for the final model |

- The inference times come from the committed run: one process, numpy only, 300 synthetic
  windows after 20 warm-up calls, on a shared 4-CPU build container.
- The same code measured **0.59 ms** per window in the other final run. Treat the figure
  as "about 1 ms on a server CPU". Nothing has been measured on the watch.

### Development changes (read before quoting the numbers)

These numbers come from the third version of the pipeline. Two changes were made after
seeing results on the full data. Neither touched the model, the features or the
hyperparameters.

1. **The gravity plausibility bound was widened from 4–16 to 4–50 m/s².**
   - The first full run refused 169 genuine windows as `implausible_gravity`, almost all
     of them jogging, because vigorous motion raises the mean magnitude up to 38 m/s².
   - With the wider bound, the pooled MLP macro-F1 moved from 0.7875 to 0.7901. Logistic
     regression moved from 0.7397 to 0.7396.
2. **The per-subject macro-F1 now counts only the classes a subject has.** At first it
   averaged over all six classes, which scored a missing class as 0 and made subjects
   1637, 1640 and 1642 look worse than they were.

Everything else was set once, before the first run, and not tuned:

- the class mapping;
- the features;
- the MLP and logistic-regression settings;
- the 0.5 threshold;
- the folds.

## Limitations

- **Population.** These are adult volunteers in a university study; the dataset gives no
  ages. There are **no older adults**. IRA CARE serves older people, who:
  - walk more slowly, with shorter and less regular steps, and may use a walking frame or
    stick, which changes or removes the wrist's arm swing;
  - may have tremor, which adds wrist motion in exactly the bands the features use;
  - may eat with help or with adapted cutlery;
  - may sit for long periods with small hand movements.

  None of that is represented here, so these numbers **cannot be taken as accuracy for
  IRA CARE's users**.
- **Device.** The data comes from an LG G Watch (2014, Android Wear 1.5) on the dominant
  wrist.
  - Its accelerometer clips at about ±2 g in many recordings.
  - **The IRA watch differs**, and may be worn on the non-dominant wrist. Its sensor,
    range, sampling rate, axis orientation and placement are all different.
  - The module resamples to 20 Hz and checks units, but axis conventions and wrist side
    are not corrected for. The gravity-direction features depend on them.
- **No physical validation on the IRA watch.** None of this has been run on IRA watch
  signals.
- **Protocol.** The activities were scripted, 3 minutes each, with clean boundaries. Real
  days have transitions, mixed activities and long idle periods. Transitions are not in
  the data, so windows that straddle two activities are untested.
- **Classes.** Sitting against eating is the weakest pair: sitting F1 is 0.570, and 265
  eating windows were read as sitting. "Eating" can only ever be a hint, never a record of
  a meal.
- **Calibration.** As shown above, the "unknown" output rarely fires on this data.
- **Per-person spread.** Macro-F1 ranges from 0.154 to 0.988 across subjects. A single
  person's experience may be far from the average.
- **Runtime environment.** Training and tests ran here with numpy 2.5.5. The backend pins
  numpy 1.26.4. The module uses only long-standing numpy functions, but it has not been run
  under 1.26.4 in this change. CI will run the tests there.

## Physical validation still needed (none of it done)

These results are **not** validation on the IRA watch, and must not be presented as such.
Before this signal is shown to anyone:

1. Record IRA watch accelerometer and gyroscope data, at the watch's real rate, range and
   axis orientation, on the wrist it will be worn on. Use a reference annotation (video, or
   an observer with a time-stamped log). Cover walking (with and without walking aids),
   stairs, sitting, standing, eating and drinking, and ordinary household activity,
   including the transitions between them.
2. Do this with **older adults**, the product's users, including people with tremor and
   people who use walking aids, under an ethics-approved protocol with consent.
3. Report:
   - per-person and per-class precision, recall and F1;
   - the confusion matrix;
   - the share of "unknown" and of refusals.
4. Check the input refusals (flat, saturated, gravity range) against the watch's real
   off-wrist, charging and clipping behaviour.
5. Calibrate the probabilities (for example by temperature scaling) on that data, and set
   the "unknown" threshold there, not on WISDM.
6. Confirm the wellbeing-only labelling and that no alert path consumes this value, as
   part of the regulatory review.
