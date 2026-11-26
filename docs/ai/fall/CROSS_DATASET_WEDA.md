# Fall v3 on WEDA-FALL: a cross-dataset test of the shipped pipeline (F4.f)

> **These figures predate D-092 (26 November 2026).** They were produced while the watch's
> countdown ignored a detection during a running countdown. D-092 changed that: such a detection
> is now attached to the incident and asked about again, so the 20 "countdown already running"
> misses below would not be lost in the same way. The fix is shown on synthetic regression tests
> only. WEDA-FALL is not being used again until its licence is confirmed, so the figures have not
> been re-run; `scripts/eval/fall_cross_weda.py --countdown d092` mirrors the new behaviour for
> that re-run, and its default (`ignore`) reproduces the figures here.

**Not a medical device, and not a measurement on the people IRA CARE serves.** Young volunteers
fell onto a mattress wearing a Fitbit Sense; older adults did daily activities only. This is a
held-out test of the pipeline the watch ships, on data it never saw, with nothing retrained and no
threshold changed. Evidence: `docs/ai/fall/cross_dataset_weda.json`. Script:
`scripts/eval/fall_cross_weda.py`. Tests of its conversions:
`supracloud-jarvis/ira/tests/readiness/test_cross_dataset_eval.py`.

> Contains information from [WEDA-FALL](https://github.com/joaojtmarques/WEDA-FALL), which is made
> available under the [ODC Attribution License](https://opendatacommons.org/licenses/by/1-0/).
> J. Marques and P. Moreno, "Online Fall Detection Using Wrist Devices", *Sensors* 23(3):1146, 2023.
> Only results are kept here; no WEDA-FALL data is in the repository.

**Provenance caveat (26 November 2026).** The owner reports that a separate review of WEDA-FALL
in a browser found no clear licence. The clone this run used (commit
`9bf15151131644e26403eadd11ecd7949e917740`) does contain an ODC-By 1.0 `LICENSE` file and a README
licence statement, both added by the author on 20 November 2026; the two observations have not
been reconciled. Until the authors confirm permission:

- the results on this page are kept as historical evidence of a run already made, and are not
  re-run or extended;
- no further commercial training or evaluation uses WEDA-FALL;
- WEDA-FALL data is not redistributed, here or anywhere else.

`docs/DATASETS.md` records commercial use for WEDA-FALL as `unclear` for the same reason.

## What was tested

- **The shipped pipeline, unchanged** (D-080): the rule detector (2.5 g impact, then 20 s with at
  most 3 moving samples) and fall model v3 in "recover" mode (2.0 g candidate, peak at window index
  100, threshold 0.95). The model is the shipped file `watch/app/src/main/assets/fall_model.tflite`
  (18,216 bytes, 6,705 parameters, SHA-256 `2cab7c9b…3278`, checked by the script before it runs),
  run by the TensorFlow Lite interpreter with the shipped spec's mean and std. The watch's Kotlin
  `FallDetector`, `SampleWindow` and `RecoveringFallDetector` are ported line for line; the port
  is checked against the cases in `RecoveringFallDetectorTest.kt`.
- **Trained on:** BITS-2 and UCI HAR only. WEDA-FALL is a different device (Fitbit Sense), a
  different accelerometer range (±4 g per axis against BITS-2's ±8 g), different people and a
  different protocol.
- **Three configurations:** the rule alone; the model path alone (rules switched off); and the
  shipped combination, which is what the watch runs.

## The data (WEDA-FALL, commit `9bf15151131644e26403eadd11ecd7949e917740`)

- 14 young volunteers (ages 20 to 46): 350 falls of 8 types onto a mattress, and 462 recordings of
  11 daily activities.
- 11 older adults (ages 77 to 95): 157 recordings of up to 6 daily activities (walking, stairs,
  sitting down and getting up, hitting a table, clapping, opening a door). No falls, for safety.
- All 969 recordings were read; none was refused (see the unit check below).

## Every transform

| Step | What was done | Why |
| --- | --- | --- |
| Units | m/s² divided by 9.80665 | The files are in m/s²; the watch divides by `SensorManager.GRAVITY_EARTH` |
| Unit check | median magnitude must be 0.5 to 2.0 g, else the file is refused | a file in g (reads 0.1) or in mg (reads 100) is refused, not rescaled |
| Axes | unchanged (Fitbit device frame; gravity reads +9.8 on the axis pointing up, as on Android) | the rules use the magnitude only; the model was trained on randomly rotated windows |
| Order | rows sorted by timestamp; a step back of more than 3 s refuses the file | 24 files hold a block of 15 to 19 rows out of order |
| Time | sample *i* at *i* / native rate | the timestamps are delivery times: about five samples share each 0.1 s step |
| Native rate | 40 or 50 Hz, whichever is within 5% of (samples − 1) / span, else refused | 325 of 350 fall files are 40 Hz (the "50Hz" folder holds the 40 Hz files); every daily-activity file is 50 Hz |
| Resampling | linear interpolation onto 50 Hz | the model and the watch run at 50 Hz |
| Edges | 2 s of the first sample before, 3 s of the last after | a full 4 s window near either end; 304 of 1,469 model decisions touched padding, and no fall was caught only through padding |

The unit check was first set at 0.8 to 1.2 g. That run refused 7 genuine m/s² recordings (6 of
jogging and 1 of clapping, median 1.21 to 1.52 g). The band was widened to 0.5 to 2.0 g, which
still refuses a unit error by a factor of 9.8. This changed which files are read, not any setting
of the pipeline. A re-run gave identical results.

## How it was scored (fixed before any result was seen)

- **A fall is caught** when a suspected fall is raised whose impact (rule) or peak (model) lies
  within the hand-annotated fall (`fall_timestamps.csv`), widened by 1 s each side.
- **The countdown:** a second detection while the 30 s countdown is running is ignored, as
  `FallCountdown` did on the watch before D-092.
- **The rule's two conventions** (as in `MODEL_CARD_V3.md`):
  - (a) only a complete 20 s still window counts;
  - (b) an impact still waiting for its 20 s when the recording ended also counts.
- WEDA-FALL recordings last 3 to 40 s, so (a) almost never completes. (b) is the rule's fair
  score for sensitivity, and an upper bound for its false alarms.

## Falls (350, young volunteers, mattress)

| | Rule (a) | Rule (b) | Model path alone | **Shipped pipeline** |
| --- | --- | --- | --- | --- |
| Falls caught | 0 | 113 | 326 | **326** |
| Sensitivity | 0.000 | 0.323 | 0.931 | **0.931** |

The shipped pipeline is the same under (a) and (b): 326 of 350.

**Rule (b) against the model path:**

| Caught by | Falls |
| --- | --- |
| Both | 109 |
| The model only | 217 |
| The rule only | 4 |
| Neither | 20 |

The 4 "rule only" falls are among the 20 described below: the model alarmed just before the fall,
and its detection of the fall itself came while that countdown was running. All 350 falls reached
2.0 g at the wrist; 348 reached 2.5 g.

**By fall type:**

| Fall | Type | n | Rule (b) | Model | Shipped |
| --- | --- | --- | --- | --- | --- |
| F01 | forward, walking, slip | 42 | 16 | 41 | 41 |
| F02 | lateral, walking, slip | 42 | 12 | 42 | 42 |
| F03 | backward, walking, slip | 42 | 16 | 38 | 38 |
| F04 | forward, walking, trip | 42 | 15 | 42 | 42 |
| F05 | backward, trying to sit down | 42 | 8 | 41 | 41 |
| F06 | forward, sitting, faint | 42 | 9 | 35 | 35 |
| F07 | backward, sitting, faint | 42 | 15 | 38 | 38 |
| F08 | lateral, sitting, faint | 56 | 22 | 49 | 49 |

F08 by side: 25 of 28 caught when falling towards the watch's side, 24 of 28 away from it.

### The 24 falls the shipped pipeline missed

- **20: the fall was detected, but a countdown was already running.** In each, a movement 1.4 to
  5.2 s before the annotated fall raised a model alarm with p ≥ 0.95. 16 of the 20 are "faint
  while sitting" falls (F06 to F08), where the earlier alarm came in the first 3 s of the
  recording, probably the volunteer sitting down or settling (the dataset's videos were not
  watched); the other 4 are walking or sit-down falls. The fall's own detection, seconds later (p 0.996 to 1.000), arrived
  while that countdown was running, and `FallCountdown` ignores a detection while it counts. The person was being asked "are you OK?"
  when they fell, so 330 of 350 falls (0.943) had a countdown running or starting at the fall.
  Counting the ignored detections, the detector itself flagged 346 of 350 (0.989).
- **4: scored below the threshold**, p 0.71 to 0.93: F03/U08_R02, F03/U12_R03, F07/U12_R02 and
  F08/U14_R03. Their peaks at the wrist were 2.2 to 5.5 g.
- **None** missed for want of an impact: every fall reached 2.0 g. (On BITS-2, 45 of 55 missed
  falls never did.)

## False alarms on daily activities

**Per hour and per recording.** The recordings are short, each built around one activity, so
"per hour" means per hour of these activities back to back: far denser in impacts than a day. The
share of recordings with an alarm is the steadier figure.

| | Young (14 people, 462 recordings, 1.76 h) | **Older adults (11 people, 157 recordings, 0.47 h)** |
| --- | --- | --- |
| Rule (a): alarms | 0 | 0 |
| Rule (b): alarms (per hour; recordings with one) | 150 (85.3 /h; 32.5%) | 65 (137.6 /h; 41.4%) |
| Model path: alarms (per hour; recordings with one) | 104 (59.1 /h; 22.5%) | **25 (52.9 /h; 15.9%)** |
| Shipped, (a): alarms | 104 (59.1 /h; 22.5%) | **25 (52.9 /h; 15.9%)** |
| Shipped, (b), upper bound: alarms | 192 (109.2 /h; 41.6%) | 70 (148.2 /h; 44.6%) |
| Model candidates (≥ 2.0 g) scored / at or above 0.95 | 789 / 109 (specificity 0.862) | 156 / 25 (specificity 0.840) |

On BITS-2's held-out daily activities, the model path flagged 22 of 955 candidates (specificity
0.977, 9.85 per hour). The rule flagged 52.84 per hour there. Here, on another device and
other people, the model flags about one candidate in seven.

**Specificity per daily-activity type** (share of recordings with no alarm; in brackets, the
recordings with an alarm):

| Activity | Group | n | Reached 2.0 g | Rule (b) | Model path | Shipped (b) |
| --- | --- | --- | --- | --- | --- | --- |
| D01 walking | young | 42 | 13 | 0.976 (1) | 0.952 (2) | 0.929 (3) |
| D01 walking | older | 26 | 2 | 0.962 (1) | 1.000 (0) | 0.962 (1) |
| D02 jogging | young | 42 | 42 | 0.500 (21) | 1.000 (0) | 0.500 (21) |
| D03 stairs | young | 42 | 38 | 0.881 (5) | 0.905 (4) | 0.810 (8) |
| D03 stairs | older | 10 | 6 | 1.000 (0) | 1.000 (0) | 1.000 (0) |
| D04 sit down, wait, get up | young | 42 | 11 | 0.952 (2) | 1.000 (0) | 0.952 (2) |
| D04 sit down, wait, get up | older | 29 | 3 | 0.931 (2) | 0.966 (1) | 0.931 (2) |
| D05 collapse back into a chair | young | 42 | 37 | 0.548 (19) | 0.643 (15) | 0.500 (21) |
| D06 crouch, tie shoes, get up | young | 42 | 40 | 0.929 (3) | 0.833 (7) | 0.762 (10) |
| D07 stumble while walking | young | 42 | 41 | 0.714 (12) | 0.738 (11) | 0.500 (21) |
| D08 gentle jump | young | 42 | 42 | 0.405 (25) | 0.310 (29) | 0.167 (35) |
| D09 hit a table with the hand | young | 42 | 42 | 0.167 (35) | 0.286 (30) | 0.024 (41) |
| **D09 hit a table with the hand** | **older** | 31 | 30 | 0.097 (28) | **0.645 (11)** | 0.097 (28) |
| D10 clapping | young | 42 | 42 | 0.452 (23) | 0.881 (5) | 0.405 (25) |
| **D10 clapping** | **older** | 32 | 32 | 0.063 (30) | **0.688 (10)** | 0.031 (31) |
| D11 open and close a door | young | 42 | 20 | 0.905 (4) | 0.976 (1) | 0.905 (4) |
| D11 open and close a door | older | 29 | 23 | 0.862 (4) | 0.897 (3) | 0.793 (6) |

**Failure cases, older adults.** 24 of the 25 model alarms came from hitting a table (11), clapping
(10) and the door (3); the other was sitting down (D04/U30_R02, peak 3.2 g, p 0.957). Their wrist
peaks were 3.2 to 6.4 g, as high as the falls'. Each is listed in the JSON
(`failure_cases.adl_false_alarms_shipped`) by recording, peak and probability.

## Size and speed

- **Model:** 18,216 bytes, 6,705 parameters.
- **Speed:** one TensorFlow Lite invoke on a 200 × 4 window, one thread, 2,000 windows on this
  build machine (x86_64, 4 shared CPUs): median 0.011 ms, p95 0.030 ms.
- **The watch:** a decision is made at most once per candidate, two seconds after its peak, so
  the cost is negligible here. Latency on a watch CPU is still **unmeasured**.

## Findings for the owner and a clinical reviewer

Nothing was changed because of these results when they were produced: the model, its threshold
and the rules are as shipped. Since then the countdown, not the model, was changed by D-092 (see
finding 2). These need a decision by people, not a retune on a test set.

1. **Everyday hand impacts by older adults raise the model path's alarm.** Hitting a table raised it
   in 11 of 31 recordings, clapping in 10 of 32. At the wrist those reach 4 to 6 g, as falls do,
   and the model, trained on BITS-2's activities, has not learnt them as harmless. Each alarm is a
   30 s countdown asking "are you OK?", not a call for help. Often enough, though, people may
   learn to dismiss it. The rule is worse on the same recordings under (b). It would raise them
   only if the person then stayed still for 20 s, which these recordings are too short to show.
   *Still open: analysed, with candidate mitigations and no threshold changed, in
   `docs/compliance/CLINICAL_SAFETY_CASE.md` (H4).*
2. **A detection during a running countdown is dropped.** In 20 falls, movement just before the
   fall (probably sitting down, in most) started a countdown, and the fall's own detection was ignored. The
   countdown still asks and still calls for help if nobody answers. But a person who dismisses
   the first prompt after falling, believing it was about the earlier movement, gets no second
   countdown for the fall. Whether a detection during a countdown should restart or extend it is
   a clinical design question. *Addressed by D-092 (26 November 2026): the detection is attached to
   the same incident without moving its deadline, and an "I'm OK" given after it asks again with a
   fresh countdown. Shown on synthetic tests; not re-run on WEDA-FALL (licence unconfirmed).*
3. **Sensitivity on these falls is high, but they are easy:** all 350 reached 2.0 g on a mattress.
   This does not show how the pipeline does on soft, slow or partial falls of older people.

## Limits

- **Falls are young people (one aged 46) falling onto a mattress**, mostly from standing or
  sitting. They are not older adults' falls at home.
- **Older adults did daily activities only.** Their sensitivity cannot be measured here; only their
  false alarms can.
- **A Fitbit Sense, not the IRA watch.** Its accelerometer clips at ±4 g per axis: 1,403 axis
  samples sit at the limit, 802 of them in jogging, 467 in falls and 99 in table hits and clapping.
- **Timestamps are delivery times.** Even sample spacing was assumed from the sample count. Most
  falls are 40 Hz, upsampled to 50 Hz.
- **Short recordings.** The rule's 20 s stillness can rarely complete, so its numbers come in two
  conventions. Per-hour rates are per hour of dense, activity-focused recordings.
- **Hand annotations.** The fall intervals were marked by hand, and the dataset notes they may
  contain mistakes; a 1 s tolerance was fixed beforehand.
- **One pass.** Nothing here was tuned, so nothing here measures how a retrained model would do.

## Command

```bash
git clone https://github.com/joaojtmarques/WEDA-FALL.git "$SCRATCH/weda/repo"      # commit 9bf1515
python3 -I scripts/eval/fall_cross_weda.py --data "$SCRATCH/weda/repo/dataset" \
    --commit 9bf15151131644e26403eadd11ecd7949e917740 --out docs/ai/fall/cross_dataset_weda.json
```

The run took 10 s on the build machine, with TensorFlow 2.21.0. A second run gave identical numbers
(timings aside). The licence was read in the clone on 26 November 2026. `LICENSE` is the ODC-By
1.0 text (SHA-256 `11a6d837…bd3b`). The README states: "The data in this project is licensed under
the Open Data Commons Attribution License (ODC-By) v1.0. You are free to share, modify, and use the
data, including for commercial purposes, provided you give appropriate attribution by citing the
paper above."
