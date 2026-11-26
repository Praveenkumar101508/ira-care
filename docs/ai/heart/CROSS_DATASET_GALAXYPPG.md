# IRA Care Heart on GalaxyPPG: an independent evaluation of the research pipeline

**Research only, and not a measurement on the people IRA CARE serves.** 24 young adults wore an
Empatica E4 and a Galaxy Watch 5, with a Polar H10 chest strap as the reference. The heart
research pipeline (`supracloud-jarvis/ira/care/heart_research.py`, D-086) ran unchanged: the
classical estimator and the shipped learned selector (`heart_selector@3b0863a9a8cf`, trained on
all 15 PPG-DaLiA subjects). Nothing was retrained and no setting was changed. GalaxyPPG is wholly
held out from the selector.

- Evidence: `docs/ai/heart/cross_dataset_galaxyppg.json`
- Script: `scripts/eval/heart_cross_galaxyppg.py`
- Tests of its conversions and reference:
  `supracloud-jarvis/ira/tests/readiness/test_cross_dataset_eval.py`

> GalaxyPPG by S. Park, D. Zheng and U. Lee (KAIST), Zenodo, doi:10.5281/zenodo.14635823,
> licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Data descriptor:
> *Scientific Data* 2025, doi:10.1038/s41597-025-05152-z. Only summary results are kept here; no
> GalaxyPPG data is in the repository, and nothing derived from it ships.

## The data

- **Record:** Zenodo 14635823, version 1.0.0. The licence field, read through the Zenodo API on
  26 November 2026, is `cc-by-4.0`. Archive MD5 `d9a48bfc…2e7d4`; the script checks it.
- **People:** 24 adults aged 20 to 29. P01 has no Galaxy Watch data and no event log.
- **Sessions:** adaptation, baseline, the TSST (preparation and speech), meditation, screen
  reading, the SSST (preparation and singing), keyboard and phone typing, standing, walking,
  jogging and running, each followed by a rest.
- **E4:** BVP at 64 Hz and accelerometer at 32 Hz in 1/64 g, the same device family as
  PPG-DaLiA.
- **Galaxy Watch 5:** green PPG at 25 Hz (ADC counts) and accelerometer at 25 Hz in m/s².
- **Polar H10 (reference):** ECG at 130 Hz, with the strap's own heart rate.

## How the reference and the windows were made (fixed before any result was looked at)

- **Windows:** the pipeline's own, 8 s every 2 s from the later of the PPG and accelerometer
  starts, over each participant's whole session (the per-call length limit lifted, as the
  pipeline allows for offline evaluation).
- **Units:**
  - E4 accelerometer as `e4_raw` (1/64 g); Galaxy Watch as `m/s2`.
  - Each is checked first: its median magnitude must be 0.5 to 2.0 g in the declared unit.
    A stream declared in the wrong unit is refused, not misread (tested in both directions).
  - Galaxy PPG samples whose status is not 0 are passed as missing (300 to 600 samples for 8
    participants).
- **Clocks:**
  - The README gives Polar timestamps in UTC+9, so 9 h is subtracted.
  - The strap and each wrist device keep separate clocks. The lag between them was measured from
    movement alone: the 1 s moving standard deviation of the acceleration magnitude, wrist against
    chest, at 10 Hz, best correlation within ±10 s.
  - The ECG reference is shifted by that lag.
  - A device more than 5 s out, or below a correlation of 0.5, is refused as misaligned.
  - Measured lags were −3.5 to +0.9 s for the E4 (correlation 0.88 to 0.98) and −2.8 to
    +1.9 s for the Galaxy Watch (0.97 to 0.99).
  - **P15's Galaxy Watch was refused**: its clock was 8.5 s from the strap's. The correlation
    was high (0.976), so the lag itself is probably right, but the rule was fixed beforehand and
    was kept.
- **Reference heart rate:**
  - R peaks by the two-moving-average QRS detector of Elgendi, Jonkman and De Boer (2010): 8 to
    20 Hz band-pass, squared, 97 ms and 611 ms moving averages, offset 0.08 × the mean energy over
    a centred 30 s, 250 ms refractory period.
  - Per window, 60 / mean RR of the beats inside it.
  - No reference when the window has an ECG gap, fewer than 4 RR intervals, an RR outside 0.27
    to 2.0 s, or an RR more than 40% from the window's median (a missed or extra beat).
  - A Pan-Tompkins implementation was tried first. Checked against the strap's own beat count,
    before any estimate was looked at, it lost track after artefacts (2 beats in an hour for P17),
    so it was replaced.
- **Check on the reference:** against the strap's own heart rate, averaged over the same window,
  the reference differs by 2.50 bpm on average (E4 windows; 2.52 for the Galaxy Watch's), and is
  within 5 bpm in 86.6% of windows. The strap's own value is a smoothed 1 Hz output, so some
  difference is expected.
- **Activity:** the session wholly containing the window, from `Event.csv`; otherwise
  "transition".

## Results: the E4 (24 participants, 45,704 windows, 40,903 with a reference)

All windows with a reference, as on PPG-DaLiA (every window, before any rejection), and what the
pipeline actually reports:

| | MAE (bpm), mean ± sd of participants | MAE, pooled | RMSE, pooled | Windows |
| --- | --- | --- | --- | --- |
| Classical candidate, every window | **9.62 ± 4.03** | 9.54 | 23.87 | 40,903 |
| Learned candidate, every window | **6.18 ± 2.92** | 6.08 | 16.26 | 40,903 |
| Reported by the pipeline | 3.70 ± 1.60 | 3.56 | 10.22 | 35,321 (86.4%) |
| of which learned (probability ≥ 0.5) | — | 2.19 | 5.82 | 33,537 |
| of which classical fallback | — | **29.27** | 37.83 | 1,784 |

**Coverage, all 45,704 windows:**

- Reported: 38,939 (85.2%): 36,913 learned and 2,026 classical fallback.
- Unavailable: 3,462 for motion artefact and 3,303 for low confidence.
- No reference: 4,342 irregular RR, 309 implausible RR, 150 too few beats.

## Results: the Galaxy Watch at 25 Hz (22 participants, 42,260 windows, 37,303 with a reference)

The pipeline's data contract accepts PPG from 20 to 512 Hz, so 25 Hz is **accepted**, not
refused. That is correct by the contract. It is, though, a rate the learned selector never saw in
training (PPG-DaLiA is 64 Hz). It is also a different sensor (raw green-LED ADC counts, not the
E4's BVP).

| | MAE (bpm), mean ± sd of participants | MAE, pooled | RMSE, pooled | Windows |
| --- | --- | --- | --- | --- |
| Classical candidate, every window it ran | 17.86 ± 7.16 | 17.67 | 34.50 | 36,013 |
| Learned candidate, every window it ran | 7.83 ± 4.31 | 7.68 | 19.14 | 36,013 |
| Reported by the pipeline | 8.19 ± 4.98 | 7.75 | 19.41 | 29,395 (78.8%) |
| of which learned (probability ≥ 0.5) | — | **1.63** | 3.64 | 24,638 |
| of which classical fallback | — | **39.44** | 47.54 | 4,757 |

**Coverage, all 42,260 windows:**

- Reported: 33,028 (78.2%): 27,788 learned and 5,240 classical fallback.
- Unavailable: 4,681 for low confidence, 3,065 for motion artefact, 1,390 for gaps in the
  watch's stream and 96 for missing samples.
- No reference: 4,085 irregular RR, 647 too few beats, 225 implausible RR.

## By activity (pooled MAE in bpm over windows with a reference; share = reported / with a reference)

| Session | E4: classical | E4: learned | E4: reported (share) | Galaxy: classical | Galaxy: learned | Galaxy: reported (share) |
| --- | --- | --- | --- | --- | --- | --- |
| adaptation | 1.32 | 1.28 | 1.23 (99%) | 2.12 | 1.37 | 1.49 (95%) |
| baseline | 1.06 | 1.08 | 1.09 (100%) | 4.98 | 1.47 | 2.37 (97%) |
| TSST preparation | 1.83 | 1.70 | 1.48 (98%) | 12.26 | 4.01 | 7.98 (89%) |
| TSST speech | 7.01 | 4.30 | 3.85 (90%) | 20.34 | 11.46 | 14.11 (76%) |
| meditation | 2.00 | 1.91 | 1.82 (99%) | 4.22 | 2.20 | 2.60 (93%) |
| screen reading | 1.54 | 1.37 | 1.23 (98%) | 5.55 | 1.76 | 3.17 (95%) |
| SSST preparation | 6.34 | 4.92 | 3.23 (90%) | 25.81 | 11.00 | 16.99 (76%) |
| SSST singing | 3.40 | 2.79 | 2.69 (92%) | 23.92 | 13.83 | 18.01 (87%) |
| keyboard typing | 11.72 | 6.28 | 6.60 (80%) | 26.73 | 9.63 | 20.34 (69%) |
| phone typing | 2.23 | 1.53 | 1.39 (97%) | 5.43 | 1.81 | 2.66 (90%) |
| standing | 3.93 | 2.78 | 2.28 (93%) | 15.28 | 4.49 | 7.38 (90%) |
| walking | 31.47 | 13.03 | 26.67 (80%) | 46.11 | 12.00 | 31.26 (77%) |
| jogging | 64.46 | 32.11 | none (0%) | 69.63 | 27.56 | none (0%) |
| running | 75.20 | 51.50 | none (0%) | 94.62 | 45.56 | none (0%) |
| rests (1 to 5) | 5.99 | 5.26 | 3.11 (91%) | 10.33 | 5.66 | 5.11 (82%) |
| transition | 12.41 | 7.70 | 5.47 (79%) | 24.32 | 11.36 | 13.25 (69%) |
| P01 (no event log) | 4.49 | 3.61 | 2.15 (90%) | — | — | — |

RMSE per session is in the JSON. Jogging and running are rejected entirely by the motion gate
(0.5 g): the pipeline reports nothing there, which is the intended behaviour.

## Against PPG-DaLiA, honestly

| | PPG-DaLiA (leave-one-subject-out, `MODEL_CARD.md`) | GalaxyPPG, E4 (held out) | GalaxyPPG, Galaxy Watch 25 Hz (held out) |
| --- | --- | --- | --- |
| Classical, every window | 13.28 ± 5.01 | 9.62 ± 4.03 | 17.86 ± 7.16 |
| Learned, every window | 7.75 ± 3.23 | 6.18 ± 2.92 | 7.83 ± 4.31 |

- **The E4's lower numbers are not better accuracy.** They come from an easier mix.
  - About 60% of GalaxyPPG's windows are seated or standing still (rests, meditation, baseline,
    reading, typing, standing). In the seated sessions without keyboard typing (baseline,
    meditation, reading, phone typing, TSST preparation), both estimators are within 1 to 2 bpm
    on the E4.
  - PPG-DaLiA is mostly daily life: lunch, driving, cycling, stairs, table football.
  - On the activity both share, walking, GalaxyPPG is slightly worse: classical 31.47 against
    23.99, learned 13.03 against 11.31.
  - So the learned selector carries over to new people on the same device family at about the
    accuracy measured on PPG-DaLiA, and no better.
- **On the Galaxy Watch, the learned selector holds up and the classical estimator does not.**
  - The learned candidate, 7.83 ± 4.31, is about where it is on PPG-DaLiA.
  - Its confident answers (probability ≥ 0.5) are 1.63 bpm on 24,638 windows.
  - The classical estimator roughly doubles its error at 25 Hz: 17.86 ± 7.16, and is worse than
    on the E4 in every session.
- **The fallback finding from PPG-DaLiA S1 is confirmed on independent data.** The pipeline
  reports the classical answer when the learned probability is low. Those answers were 16.23 bpm
  on S1; here they are **29.27 bpm (E4)** and **39.44 bpm (Galaxy Watch)**, against 2.19 and
  1.63 for the learned answers. On the Galaxy Watch, the fallback windows (16% of those
  reported) make the overall reported error (7.75) worse than the learned candidate on every
  window (7.68). The pipeline reported the classical answer when the learned probability was
  low; the evaluation above is of that rule. It has since been changed (D-094, below).

## After D-094: an unsure learned selector gives "unavailable"

Since D-094 (26 November 2026), a window where the learned selector ran and its probability was
below 0.5 is reported as unavailable (`learned_low_confidence`), not with the classical answer.
The rule was chosen on PPG-DaLiA leave-one-subject-out data from 14 subjects
(`docs/ai/heart/fallback_rule_analysis.json`), **not on GalaxyPPG**, which stays an independent
test; an alternative that kept the classical answer when it agreed with the learned candidate
within 5 bpm was rejected there.

**GalaxyPPG was not downloaded or run again.** The change relabels windows and changes nothing the
estimators compute (the selector's tracking does not depend on what is reported), so the new rule's
answers are exactly the "of which learned" rows of the run above, and its coverage is the learned
share. The numbers below are those rows, not a new run, and `cross_dataset_galaxyppg.json` is
left as the record of the run under the previous rule. Mean ± sd of participants is not available
for the learned rows (the run kept only the pooled figure for them).

| Pooled, windows with a reference | Before D-094: MAE (RMSE), coverage | After D-094: MAE (RMSE), coverage |
| --- | --- | --- |
| E4 | 3.56 (10.22) bpm, 35,321 of 40,903 (86.4%) | **2.19 (5.82) bpm**, 33,537 (82.0%) |
| Galaxy Watch 5 at 25 Hz | 7.75 (19.41) bpm, 29,395 of 37,303 (78.8%) | **1.63 (3.64) bpm**, 24,638 (66.0%) |

Over all windows (with or without a reference), coverage goes from 85.2% to 80.8% on the E4
(38,939 to 36,913 windows) and from 78.2% to 65.8% on the Galaxy Watch (33,028 to 27,788). The
price on the Galaxy Watch is large: about one window in six that showed a number now says the selector
was not sure. At 25 Hz, and on a sensor the selector never saw, that is the honest answer.

## Processing

- The learned path took 13.5 ms per window on the E4 and 12.4 ms on the Galaxy Watch (the mean
  of the participants' means; the slowest participant's mean was 19.0 ms). This was on the shared 4-CPU build machine with three
  participants processed at once. Windows arrive every 2 s.
- The whole evaluation took 408 s (`--workers 3`).

## What this does and does not change

- **Requirements:** no F3 requirement covers an independent evaluation, so none was added or
  promoted. The result is recorded as further evidence for F3.g, the research pipeline on
  recorded signals.
- **Still blocked:**
  - F3.d: the learned selector in the product needs a watch that gives the app raw PPG.
  - F3.e: accuracy on the IRA watch and on older adults against an ECG reference.
- **Galaxy Watch 5 is not the IRA watch.** That it works at 25 Hz says the selector is not tied to
  the E4's rate. It says nothing about the IRA watch's sensor.
- **The participants are 20 to 29.** No older adults, no arrhythmia, no beta-blockers.

## Limits

- Young adults in a laboratory protocol; much of the time seated.
- The reference is computed here from the strap's ECG, not supplied with the dataset. Windows
  with irregular RR intervals (about 10% of E4 windows) have no reference. That leaves out ectopic beats
  as well as detector errors, so the result says nothing about irregular rhythms.
- The clock alignment is measured from movement, to about a second. Heart rate changes little in
  a second, except at the start of exercise.
- One pass: nothing here was tuned, so nothing here measures how a retrained selector would do.

## Command

```bash
curl -L -o "$SCRATCH/galaxy/GalaxyPPG.zip" \
    "https://zenodo.org/api/records/14635823/files/GalaxyPPG.zip/content"
python3 -I scripts/eval/heart_cross_galaxyppg.py --zip "$SCRATCH/galaxy/GalaxyPPG.zip" \
    --out docs/ai/heart/cross_dataset_galaxyppg.json --workers 3
```

The archive is read in place (nothing is unpacked) and was deleted after the run.
