# Fall false alarms from hand impacts: the confirmation stage (round 9, 26 November 2026)

The cross-dataset check on WEDA-FALL (`CROSS_DATASET_WEDA.md`) recorded 52.9 alarms an hour on
older adults' daily-activity recordings, most from hand impacts the detectors read as falls. That
figure is historical, from before D-092, on a dataset whose licence for this use is unresolved; it
is not re-run here and is not the corrected pipeline's performance. The owner's question for this
round: what can be done about hand impacts without losing a fall.

## What the detectors see, and what they miss

Both paths decide from the impact alone. The rule needs 2.5 g and then 20 s of stillness; the v3
model sees a 4 s window around a 2.0 g candidate (two seconds after the peak) and raises at 0.95.
Neither looks at the two things a wrist shows after a fall and does not show after a clap: the
direction gravity reads changed (the body went from upright to the floor), and the arm is quiet or
stirring rather than carrying on with a task. The model was trained to tell impacts apart from
2 s of signal; the information that separates a slap from a fall arrives in the seconds after.

## The confirmation stage

`watch/app/src/main/java/care/ira/watch/sensors/FallConfirmation.kt`, mirrored line for line by
`scripts/eval/fall_confirmation.py` (`tests/readiness/test_fall_confirmation.py` pins every
constant of the two to each other). It sits between a detector's `SuspectedFall` and the
countdown's question (`CareForegroundService.watchForFalls`), and decides once the sample four
seconds after the impact has arrived:

| Measure | Window | What it is |
|---|---|---|
| orientation change | mean gravity vector 2.0 to 0.5 s before the impact against 1.5 to 4.0 s after | the angle between them |
| post-impact motion | 1.5 to 4.0 s after | the standard deviation of \|a\| |
| impacts | ±3 s around the impact | local maxima of \|a\| at or above 2.0 g, at least 150 ms apart |
| clipped | the whole window | any axis at or beyond 15.5 g (the sensor's range is ±16 g); a flag, never a reason |

| Decision | Rule |
|---|---|
| **confirm**, "still afterwards" | post-impact motion under 0.15 g. A still wrist is always asked about, whatever the orientation did. This rule comes first so nothing below can touch a person lying still |
| suppress, "repeated impacts, same orientation, moving" | three or more impacts in the ±3 s window and an orientation change under 30 degrees: clapping, knocking, hammering, shaking |
| suppress, "same orientation, hand carried on" | orientation change under 20 degrees and post-impact motion over 0.35 g: a slap, then the hand goes on with what it was doing |
| **confirm**, "orientation changed or arm not busy" | everything else, including every candidate between "still" and "busy": when unsure, ask |

Cost: on the model path the question comes up to two seconds later (the model decides two seconds
after the peak; the stage needs four). On the rule path, which has already waited twenty seconds,
nothing changes. Off-wrist candidates never reach the stage (the detector drops them). The
countdown's handling of a second detection (D-092) is downstream and unchanged.

**Off by default.** `FallSettingsDto.confirmBeforeAsking` (`fall_confirm_before_asking` from the
server's settings, default false): the stage has been measured on synthetic scenarios only. It is
turned on per person once it has been run on the permitted recorded falls (below). A control that
could lose a real fall does not ship on by default on synthetic evidence.

## Measured on the synthetic scenarios (`synthetic_confirmation.json`)

`scripts/eval/fall_synthetic_eval.py --seed 7 --per-kind 20`: 160 fall and 220 non-fall
recordings (`SYNTHETIC_SCENARIOS.md`), the shipped pipeline (rule + v3 model through LiteRT,
thresholds unchanged) with and without the stage. **Synthetic: a regression measure, not accuracy.**

| | Baseline (shipped) | With the confirmation stage |
|---|---|---|
| Falls caught (of 160) | 154 | **154** (no caught fall lost) |
| Alarms on non-fall recordings (1.35 h) | 88, 65.4 an hour | **62, 46.1 an hour** |

By kind:

| Kind | Scenarios | Baseline alarmed | With stage |
|---|---|---|---|
| clapping | 20 | 1 | 0 |
| knocking | 20 | 1 | 0 |
| hammering | 20 | 4 | 0 |
| wrist_shake | 20 | 18 | 4 |
| brushing_teeth | 20 | 6 | 1 |
| table_slap | 20 | 8 | 8 (the hand's movement afterwards, 0.3 to 0.6 g, is mostly under "busy": the stage asks) |
| drop_into_chair | 20 | 9 | 9 (an impact, a small turn, then still or gentle fidgeting: the stage asks, as it should when unsure) |
| watch_put_on_table | 20 | 20 | 20 (perfectly still afterwards: confirmed by design; on the watch the off-body sensor removes these before the stage) |
| clipping_hit | 20 | 20 | 20 (the hand's movement afterwards mostly under "busy"; the saturation is flagged) |
| washing_hands, stairs_down | 40 | 0 | 0 |
| falls, all eight kinds | 160 | 154 | 154 |

The six falls the shipped pipeline misses are two `fall_then_moves` and four `stairs_tumble`
recordings in which the stirring kept the rule's still window from closing and the model's 4 s
window from reaching 0.95; the stage cannot add a fall, only keep or remove one.

What the stage removed: every clap, knock and hammer burst, most shaking and most tooth brushing,
on the orientation and repetition rules. What it kept, truthfully: single impacts followed by a
calm or slightly moving wrist. Lowering `BUSY_SD_G` would remove more table slaps and would also
start removing `fall_then_moves`; the thresholds were not tuned to this set.

## What is not shown here, and the next step

- **Real falls that keep the wrist's orientation while the person moves strongly afterwards**
  would be suppressed. None of the synthetic falls has that shape; whether real ones do is exactly
  what the permitted recorded datasets must answer before the stage is turned on.
- **The permitted data was run on 27 November 2026, and the stage failed its gate.** See the
  next section.
- **The battery and latency cost** on the watch: the stage keeps thirty seconds of samples (1,500
  samples) and does a few hundred floating-point operations once per suspected fall; measured on
  the JVM only.

Evidence: `synthetic_confirmation.json` (every decision with its measures), `FallConfirmationTest`
(8 JVM tests), `tests/readiness/test_fall_confirmation.py` (9).

## On the permitted recorded datasets: the gate fails, the stage stays off (27 November 2026, D-110)

`scripts/eval/fall_recorded_confirmation.py` ran the shipped pipeline (the rule detector and the
shipped `fall_model.tflite` through LiteRT, unchanged) over every BITS-2 recording and every
continuous UCI HAR segment, then the confirmation stage on each suspected fall, exactly as the
watch would. Both datasets were fetched on the build machine (not the owner's computer) with the
SHA-256 the training scripts pin, into a folder outside the repository, and deleted afterwards;
only the results are kept (`recorded_confirmation.json`).

| | Shipped pipeline | With the confirmation stage |
|---|---|---|
| BITS-2 fall recordings with a suspected fall raised (of 316) | 217 | 215 |
| Suspected falls on BITS-2 daily activities (640 recordings, 2.233 h) | 10 | 10 |
| Suspected falls on UCI HAR (857 segments, 3.967 h) | 0 | 0 |

**The gate fails: the stage lost 2 real falls the pipeline caught** (`user4_fall3`, `user41_fall1`),
and removed **none** of the false alarms on recorded daily activities. Both lost falls are the shape
this page warned about: the wrist kept its orientation (8° and 15° turn) while the arm went on moving
afterwards (motion SD 0.50 g and 1.05 g), so the stage read them as "the hand carried on". The
recorded false alarms, by contrast, are nearly all followed by a still wrist, which the stage
always asks about, so it cannot remove them.

What this means:

- **The stage stays off** (`fall_confirm_before_asking` defaults to false on the watch, and no
  server code sets it). `tests/readiness/test_fall_confirmation_stays_off.py` fails if either
  changes while this result stands.
- **The thresholds were not retuned to pass.** Fitting them to these recordings would make the
  next measurement meaningless; a redesign has to be specified first and then tested on data it
  was not fitted to (for example the FallAllD or KFall recordings, once permission is granted).
- The synthetic result (alarms 88 to 62) shows what the stage does to hand impacts; the recorded
  result shows it does not reduce the false alarms real daily activities cause and costs real
  falls. The recorded result decides.

Limits, also in the JSON: the shipped model was trained on all BITS-2 subjects, so its own catch
rate here (217 of 316) is in-sample and is not a sensitivity claim; the confirmation
stage was not fitted to either dataset, so what it lost is an out-of-sample result. 39 decisions
saw less than the full 30 s after the impact because the recording ended. UCI HAR is a waist-worn
phone with no falls; it measures alarms only.

