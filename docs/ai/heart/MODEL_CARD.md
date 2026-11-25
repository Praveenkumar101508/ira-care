# IRA Care Heart: model card

Heart rate (bpm) from one 8-second window of wrist PPG plus the wrist accelerometer.
Register item F3. Status: **evaluated on a public dataset only**. Nothing here has been run
on the IRA watch.

- Inference paths, both numpy only:
  - classical: `supracloud-jarvis/ira/care/heart_estimate.py` (signal processing, no weights);
  - learned: `supracloud-jarvis/ira/care/heart_learned.py`, reading the tree ensemble
    `supracloud-jarvis/ira/care/models/heart_selector.json` (plain JSON, format `ira-gbt-v1`).
    With no valid model file it answers with the classical estimator.
- Training and evaluation: `scripts/training/heart_ppg_dalia.py`
- Results, every number below: `docs/ai/heart/results.json`
- Tests: `supracloud-jarvis/ira/tests/care/test_heart_estimate.py` (synthetic signals) and
  `supracloud-jarvis/ira/tests/care/test_heart_learned.py` (synthetic signals, the model
  loader, and feature rows recorded at export in `tests/care/fixtures/heart_selector_rows.json`)

## Intended use

- **A wellbeing trend signal only.** Examples: "resting heart rate this week compared with
  last week", or a carer-facing chart of the day.
- **Not a medical device.** It is not for diagnosis, it is not an arrhythmia detector, and
  it is not a basis for treatment.
- **Not validated on older adults** or on the IRA watch. See Limitations.
- **Never an alarm on its own.** No alert, escalation or SOS may be raised because of this
  estimate alone. At the time of writing, nothing in the backend calls either module; a test
  (`test_nothing_raises_an_alert_on_the_learned_estimate`) fails if anything in `care/` starts
  to use the learned one.
- The function returns **no estimate** in these cases:
  - the input is too short (under 6 s), contains NaN or infinite values, is flat, or is
    saturated;
  - the accelerometer window is missing samples or is not finite;
  - classical path: the spectral confidence is below 0.3;
  - learned path: the selected candidate's probability of being within 5 bpm is below 0.5.
  Callers must show "no reading" in those cases and must not fill in a guess.

## Data and licence

- **PPG-DaLiA**, UCI Machine Learning Repository dataset 495
  (<https://archive.ics.uci.edu/dataset/495/ppg+dalia>, DOI 10.24432/C53890).
  - Licence: **Creative Commons Attribution 4.0 International (CC BY 4.0)**, as stated on
    the UCI page.
  - Archive `ppg+dalia.zip`, SHA-256
    `5772387956e34e2e2dc4c2ddbeb98cb70569d5112fa4c13ee98a17680b84a1f3`. The script checks
    this checksum.
  - The data is downloaded outside the repository and is never committed.
- Citation (the dataset readme asks for this reference):
  > A. Reiss, I. Indlekofer, P. Schmidt, K. Van Laerhoven. "Deep PPG: Large-Scale Heart
  > Rate Estimation with Convolutional Neural Networks". *Sensors* 2019, 19(14), 3079.
- Contents, confirmed from the dataset's own readme (`PPG_FieldStudy_readme.pdf`) and from
  the files themselves:
  - 15 subjects (7 male, 8 female, aged 30.6 ± 9.6 years);
  - an Empatica E4 on the non-dominant wrist, giving BVP (PPG) at 64 Hz and a 3-axis
    accelerometer at 32 Hz;
  - ground-truth heart rate from a chest ECG, as the mean instantaneous heart rate per 8 s
    window with a 2 s shift (`label`);
  - activity ids 0 to 8 at 4 Hz;
  - about 2.5 hours per subject. S6 has only about 1.5 hours because of a hardware fault.
- Windowing, exactly as the labels: window *i* is `BVP[128 i : 128 i + 512]` and
  `ACC[64 i : 64 i + 256]`. This gives 64,697 windows in total. On S1, the 4,603 labels
  match (9,212 s − 8 s) / 2 s + 1.
- Units: the readme gives the E4's raw `ACC.csv` in 1/64 g. In `SX.pkl` the wrist ACC is
  **already in g**: a resting wrist reads a magnitude of about 1.0. No rescaling was
  applied.
- Untrusted data handling:
  - each `SX.pkl` is a Python 2 pickle (latin1). The script streams it out of the
    archive, without extracting the 20 GB, through an **allowlisted unpickler**. Only numpy
    array reconstruction and builtin containers are allowed; any other global is refused.
  - only the wrist arrays are cached, as `.npz`, and they are loaded with
    `allow_pickle=False`.
  - Python is run with `-I`.

## Method

### Shipped: classical spectral estimator (`care/heart_estimate.py`)

1. **Input checks.** The function refuses input that is shorter than 6 s, not finite, flat
   (peak-to-peak about 0), or saturated (more than 10% of samples on the minimum or
   maximum value).
2. **Band-pass and spectrum.** The PPG and each accelerometer axis are band-passed to
   0.5–3.5 Hz (30–210 bpm) by **FFT masking**, using numpy only. The spectrum is
   Hann-windowed and zero-padded to a 1/128 Hz grid (about 0.47 bpm).
3. **Motion-artefact suppression.** Suppression applies when the band-limited
   accelerometer RMS is above 0.01 g.
   - The normalised accelerometer amplitude spectrum is widened by a running maximum of
     ±0.1 Hz.
   - The PPG spectrum is multiplied by `1 − acc`, with a floor of 0.02.
4. **Harmonic support.** The score is `P(f) · (1 + 0.5 · P(2f))`. This favours the
   fundamental over its first harmonic without letting a weak sub-harmonic win.
5. **Temporal tracking** (`HeartTracker`).
   - Each window applies a Gaussian prior (σ = 12 bpm) around the previous estimate.
   - If the tracked peak scores below 0.3 of the best peak in the band for 3 windows in a
     row, the tracker re-acquires without the prior.
6. **Confidence.** This is the share of the suppressed in-band spectrum within ±0.15 Hz of
   the chosen peak. Below 0.3 the answer is "no reliable estimate" (`low_confidence`), and
   the peak is still reported as `candidate_bpm`.

### Learned (shipped as `care/heart_learned.py` + `models/heart_selector.json`): candidate-peak selection

- For each window, the candidates are:
  - the top 3 local maxima of the raw PPG spectrum;
  - the top 3 of the suppressed, harmonic-scored spectrum;
  - the classical tracker's estimate;
  - with duplicates within 0.05 Hz removed.
- Each candidate gets 19 features:
  - the PPG power, raw and suppressed;
  - the harmonic score;
  - the accelerometer amplitude at f, f/2 and 2f;
  - the PPG power at f/2 and 2f;
  - the log motion RMS;
  - the ranks;
  - the distance to the classical estimate and to the previous classical estimate;
  - the classical confidence.
- The features are computed by `candidate_features()` in `care/heart_learned.py`. The
  training script imports that function, so training and inference run the same code; a
  test fails if the script re-implements it.
- A scikit-learn `HistGradientBoostingClassifier` is trained to predict "within 5 bpm of
  the reference". The settings are 200 iterations, learning rate 0.1, 31 leaves,
  L2 regularisation 1.0 and `random_state=20261125`, and they were set once and not
  tuned.
- Evaluation: trained on 14 subjects and tested on the held-out subject, for each subject.
- Selection is causal: the selected candidate maximises
  probability × Gaussian(σ = 15 bpm) around the previous selected value.
- Below a selected probability of 0.5 the answer is "no reliable estimate". 0.5 ("more
  likely within 5 bpm than not") was fixed in the code before its coverage was computed;
  the table below shows the other thresholds for comparison, not as a tuning sweep.

### What ships, and how the weights are kept safe

- **The shipped model** is the same classifier trained once on all 15 subjects (256,643
  candidate rows). Its accuracy is the leave-one-subject-out figure above, the standard
  estimate for a model trained on everyone.
- **Format:** the trees are exported to plain JSON (`ira-gbt-v1`): per tree, the split
  feature, threshold, where a missing value goes, the child indices and the leaf values;
  plus the baseline and the feature list. **No pickle, and nothing that runs code.**
  - 200 trees, 12,200 nodes, 467,177 bytes;
    SHA-256 `3b0863a9a8cf81f906f5475fe54cc3878a38fbe7f7b1cc88b3c851d85f1c35dc`.
- **Loader checks** (`load_model`): a size limit (4 MiB), the format name, the exact feature
  list, finite numbers, integer indices (not booleans), every feature index in range, every
  child after its parent and inside the tree (so evaluation always ends), leaves without
  children, at most 1,000 trees of at most 4,096 nodes. Anything else is refused, and the
  tracker then answers with the classical estimator (`method="classical"`), logged.
- **Equivalence:** at export the script evaluates all 256,643 training rows with the numpy
  evaluator and with scikit-learn; the largest probability difference was 2.2 × 10⁻¹⁶ (the
  script refuses to write a model above 10⁻⁹). 56 of those rows, 8 with missing values, are
  kept as a test fixture with the trainer's probabilities.
- **Runtime:** loading takes about 25 ms. One window takes about 17 ms (features plus 200
  trees), against 0.84 ms for the classical path, measured on a shared 4-CPU machine
  under load. Windows arrive every 2 s.
- **Not in the product yet, and why (requirement F3.d, blocked):** the watch apps run on Wear OS, which gives an app finished heart-rate values (`Sensor.TYPE_HEART_RATE`), not the raw PPG this estimator reads, so no product path carries a PPG window to it. It becomes usable when a watch exposes raw PPG to the app (the IRA watch's sensor HAL, E3.f).
- **Which one a caller uses:** both are available; neither is wired to any alert. A caller
  that wants the better trend signal uses `LearnedHeartTracker`; it falls back by itself.

### Seeds and reproducibility

- `SEED = 20261125` is set for `random`, numpy and the classifier.
- Two complete runs produced identical `methods` and confidence-coverage sections in the
  JSON.
- **Re-verified 2026-11-25** (register F3, REMEDIATION F6):
  - the archive's SHA-256 matched the recorded one;
  - the original script, run unchanged, reproduced every number in `methods`, the
    confidence coverage and the input refusals exactly: classical 13.28 ± 5.01 bpm and
    learned 7.75 ± 3.23 bpm;
  - only timings and the pickled model size of fold S1 (748,003 then, 746,983 bytes now,
    with newer numpy) differed;
  - after the features moved into `care/heart_learned.py`, the script reproduced the same
    `methods` and coverage sections again, and exported the shipped model.

## Command used

```bash
pip install numpy scipy scikit-learn       # scikit-learn 1.9.1 was used
python3 -I scripts/training/heart_ppg_dalia.py \
    --data-dir "$DATA_DIR" \
    --out docs/ai/heart/results.json \
    --model-out supracloud-jarvis/ira/care/models/heart_selector.json \
    --fixture-out supracloud-jarvis/ira/tests/care/fixtures/heart_selector_rows.json
```

`$DATA_DIR` was a temporary directory outside the repository holding the archive and the
per-subject cache. The command is otherwise exactly as run.

- First run: x86_64 with 4 CPUs, no GPU, 3 worker processes, Python 3.13.18 and
  numpy 1.26.4. Total time 110 s, plus the one-off 2.9 GB download and a per-subject cache
  build of about 7 s per subject.
- The run behind the current `results.json` and the shipped model used the same machine
  type, numpy 2.5.5 and scikit-learn 1.9.1. It took 489 s on a machine shared with other
  jobs. The backend pins numpy 1.26.4; the inference module uses only basic numpy
  operations, and its tests run in CI on the pinned version.

## Results: leave-one-subject-out, MAE in bpm

- Every subject is held out once.
- The classical methods have no trained parameters, so the subject they are tested on
  never influences them by training. See the caveat on development below.
- "Mean ± sd" is over the 15 per-subject MAEs, which is how the Deep PPG paper reports.
  "Pooled" is over all windows.
- Every method produced an estimate on every window; there were no input refusals on the
  dataset.

| Subject | Windows | Naive (control) | Classical, no ACC | Classical, no tracking | **Classical (shipped)** | **Learned** | Candidate oracle |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | 4603 | 13.06 | 12.88 | 14.82 | 11.01 | 8.09 | 3.73 |
| S2 | 4099 | 15.22 | 14.49 | 16.76 | 12.36 | 7.69 | 4.22 |
| S3 | 4367 | 19.40 | 22.78 | 14.67 | 9.82 | 4.81 | 3.26 |
| S4 | 4572 | 21.75 | 23.85 | 20.40 | 16.97 | 9.37 | 5.64 |
| S5 | 4649 | 45.91 | 50.58 | 35.71 | 25.99 | 16.38 | 4.51 |
| S6 | 2622 | 27.17 | 22.33 | 19.69 | 13.68 | 8.77 | 3.71 |
| S7 | 4668 | 9.47 | 9.13 | 8.57 | 5.30 | 3.40 | 2.63 |
| S8 | 4037 | 17.59 | 16.57 | 16.86 | 12.43 | 9.63 | 5.29 |
| S9 | 4277 | 26.51 | 29.26 | 22.41 | 19.06 | 11.22 | 6.50 |
| S10 | 5321 | 15.41 | 14.14 | 13.54 | 9.02 | 4.21 | 2.81 |
| S11 | 4521 | 26.51 | 26.37 | 20.06 | 19.04 | 9.19 | 4.27 |
| S12 | 3954 | 15.32 | 16.57 | 15.45 | 11.63 | 7.72 | 3.94 |
| S13 | 4565 | 10.49 | 6.51 | 12.51 | 7.99 | 4.68 | 2.85 |
| S14 | 4476 | 15.88 | 13.76 | 15.80 | 11.66 | 4.95 | 3.37 |
| S15 | 3966 | 15.36 | 16.29 | 15.06 | 13.20 | 6.09 | 3.38 |
| **Mean ± sd of subjects** | | 19.67 ± 8.83 | 19.70 ± 10.26 | 17.49 ± 5.92 | **13.28 ± 5.01** | **7.75 ± 3.23** | 4.01 ± 1.07 |
| Pooled windows | 64697 | 19.51 | 19.65 | 17.42 | 13.23 | 7.68 | 3.99 |

What each column is:

- **Naive (control):** the largest PPG spectral peak in the band, with no accelerometer and
  no tracking.
- **Classical, no ACC:** the shipped tracker with the accelerometer withheld.
- **Classical, no tracking:** the shipped estimator called statelessly, with the
  accelerometer.
- **Candidate oracle:** not a method. It is the best any selector could do with the learned
  model's candidate peaks, a ceiling for candidate selection.

**Published reference:** the Deep PPG CNN ensemble, **7.65 bpm** MAE, leave-one-subject-out
on PPG-DaLiA, from Reiss et al. 2019. This is **as reported, not reproduced.** It was not
re-checked against the paper:

- the publisher's site was not reachable from the build machine;
- neither the dataset readme nor the UCI page states the number.

The learned model's 7.75 bpm is in the same range, but different code under a different
protocol is not a like-for-like comparison.

### By activity (pooled windows, MAE in bpm)

| Activity | Windows | Naive | Classical (shipped) | Learned |
| --- | --- | --- | --- | --- |
| sitting | 4569 | 3.24 | 2.38 | 2.92 |
| working | 8497 | 9.75 | 4.82 | 3.70 |
| driving | 6843 | 14.45 | 7.24 | 5.34 |
| lunch | 13554 | 15.44 | 8.23 | 5.64 |
| transient (between activities) | 17515 | 23.73 | 16.66 | 9.32 |
| cycling | 3473 | 30.08 | 19.86 | 11.54 |
| walking | 4697 | 29.19 | 23.99 | 11.31 |
| table soccer | 2310 | 32.90 | 26.45 | 15.54 |
| stairs | 3239 | 38.06 | 33.54 | 14.38 |

The classical estimator is good when the wrist is still (sitting, desk work) and poor
under rhythmic arm motion, which is the known weakness of spectral methods.

### Confidence against error (shipped classical, pooled)

| Confidence ≥ | Share of windows kept | MAE (bpm) |
| --- | --- | --- |
| 0.0 | 100% | 13.23 |
| 0.2 | 81.4% | 10.48 |
| 0.3 (shipped default) | 67.3% | 8.51 |
| 0.4 | 53.6% | 6.36 |
| 0.5 | 41.6% | 4.54 |
| 0.6 | 31.1% | 3.11 |

### Probability against error (learned, pooled, leave-one-subject-out)

| Selected probability ≥ | Share of windows kept | MAE (bpm) |
| --- | --- | --- |
| 0.0 | 100% | 7.68 |
| 0.3 | 80.9% | 4.80 |
| 0.4 | 73.6% | 4.06 |
| 0.5 (shipped default) | 66.7% | 3.44 |
| 0.6 | 59.9% | 2.95 |
| 0.7 | 52.7% | 2.49 |

At about the same share of windows kept (two thirds), the learned path's error is 3.44 bpm
against 8.51 bpm for the classical path.

### Runtime and size

| | Classical (shipped) | Learned |
| --- | --- | --- |
| Trained parameters | 0 | 12,200 tree nodes (shipped model, 200 trees) |
| Size | module source, 12,891 bytes | `heart_selector.json`, 467,177 bytes |
| Inference (first run, scikit-learn) | 0.737 ms per window (one CPU) | 0.019 ms per window for the classifier and selection, plus feature extraction |
| Inference (shipped numpy path, re-run under load) | 0.84 ms per window | 17 ms per window, all included; loading the model about 25 ms |
| Training | none | 2.33 s per fold (mean) on the first run; 21.7 s under load on the re-run |

### Development caveat (read this before quoting the classical number)

The classical method was revised once after its first full run on all 15 subjects, so its
numbers are **not** a clean held-out estimate:

- **First version (additive harmonic support, `P(f) + 0.5 · P(2f)`):** the tracker locked
  onto the sub-harmonic, for example 49 bpm against a reference of 98. The tracked version
  scored 27.46 bpm, worse than the untracked one at 18.02.
- **Change:** harmonic support was made multiplicative.
- **Re-acquisition settings:** three settings were then compared on all subjects (ratio
  0.3 after 3 windows: 13.28; 0.5 after 2: 14.82; 0.5 after 3: 13.92), and the original
  0.3 / 3 was kept.
- **Confidence threshold:** the 0.3 default was chosen from the pooled coverage table
  above, also on all subjects.

The learned model's hyperparameters were not tuned. Its features include the classical
tracker's output, so it inherits that revision.

### Negative control

The tests use the same synthetic windows for both checks:

- a 72 bpm pulse plus a motion artefact three times its amplitude at 0.9, 1.6, 1.9 or
  2.6 Hz, with matching arm swing on the accelerometer.

In every case, the naive argmax without the accelerometer (`naive_spectral_peak_bpm`)
returns the swing frequency, more than 10 bpm off. The estimator denied the accelerometer
is fooled the same way. With the accelerometer, it returns 72 ± 3 bpm.

On PPG-DaLiA, the same ablation is visible in the table:

- naive: 19.67 bpm;
- classical without ACC: 19.70 bpm;
- classical with ACC: 13.28 bpm.

## Limitations

- **Population:** 15 adults aged 30.6 ± 9.6 years. **No older adults.** IRA CARE serves
  older people, who are more likely to have:
  - stiffer arteries and weaker peripheral perfusion, both of which affect PPG amplitude
    and shape;
  - arrhythmias such as atrial fibrillation, which break the single-spectral-peak
    assumption;
  - tremor;
  - medications such as beta-blockers that change heart-rate range.
  None of that is represented here.
- **Device:** an Empatica E4, a research wristband with its own optics, LED wavelengths,
  sampling and placement (non-dominant wrist). **The IRA watch differs**: its sensor,
  sampling rate, units and on-wrist fit are all different. The estimator takes the rates
  and accelerometer scale as parameters, but nothing has been measured on IRA watch
  signals.
- **Motion:** errors are large during walking, stairs, cycling and table soccer.
- **Resolution:** when the heart rate is within about the 8 s window's resolution
  (0.125 Hz, about 8 bpm) of the arm-swing or step frequency, suppression cannot separate
  the two. The tracker carries the previous value through such windows, which can drift.
- **Protocol:** activities were scripted, though performed naturally. The window labels
  are 8 s means, so the estimator cannot see fast changes within a window.
- **No clinical validation of any kind.** A dataset MAE is not accuracy on a person.

## Physical validation still needed (none of it done)

These results are **not** validation on the IRA watch, and must not be presented as such.
Before the trend signal is shown to anyone:

1. Record IRA watch PPG and accelerometer, at the watch's real rates and units, together
   with a reference chest-strap ECG or a medical-grade ECG. Cover rest, desk work,
   walking, stairs and household tasks.
2. Do this with **older adults** (the product's users), across skin tones (Fitzpatrick
   I–VI) and wrist sizes, including people with atrial fibrillation and people taking
   beta-blockers, under an ethics-approved protocol with consent.
3. Report per-person and per-activity MAE, the share of "no reliable estimate", and
   agreement plots (Bland–Altman) against the reference.
4. Check the input refusals against the watch's real flat, saturated and off-wrist
   behaviour, and set `MIN_CONFIDENCE` on that data, not on PPG-DaLiA.
5. Confirm the wellbeing-only labelling and that no alert path consumes this value, as
   part of the regulatory review.
