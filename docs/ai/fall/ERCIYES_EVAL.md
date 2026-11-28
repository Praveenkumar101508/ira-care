# The shipped fall pipeline on a dataset it never saw: Erciyes, right wrist (28 November 2026)

**Contains data from the "Simulated Falls and Daily Living Activities" data set by A.T. Özdemir and
B. Barshan (Erciyes and Bilkent universities), UCI Machine Learning Repository,
doi:10.24432/C52028, licensed under CC BY 4.0 (<https://creativecommons.org/licenses/by/4.0/>).**
Please also cite: Özdemir, A.T.; Barshan, B. "Detecting Falls with Wearable Sensors Using Machine
Learning Techniques." *Sensors* 2014, 14, 10691-10708.

Script: `scripts/eval/fall_erciyes_eval.py`. Results: `erciyes_eval.json`. Run on the build machine
(not the owner's computer) in 7.8 s; the data (1,243,886,880 bytes, SHA-256
`1520ac019b9d5707853ab16d05787980b1b3f4a0c8bb022f4ffd1e97df863173`) was fetched and unpacked
outside the repository and is not committed.

## What ran

The shipped pipeline, unchanged: the rule detector and the v3 model (`fall_model.tflite`, LiteRT),
on every right-wrist recording (sensor 340537), converted from m/s² at 25 Hz to the watch's g at
50 Hz. **The model was never trained or tuned on this dataset**, and nothing was changed after
seeing these numbers.

## Results

| | Recordings | With an alarm | Rate |
|---|---|---|---|
| Simulated falls (20 types, 16 volunteers) | 1,722 | 1,509 caught | **87.6 % sensitivity** |
| Daily activities (16 types, 17 volunteers, 7.8 hours) | 1,414 | 44 | 3.1 % of recordings |

**Where it misses falls** (caught of recorded):

| Fall | Caught |
|---|---|
| 917 rolling out of bed onto the floor | **40 of 89 (45 %)** |
| 916 sideways to the left, quick recovery | 51 of 86 (59 %) |
| 920 fainting, sliding down a wall | 61 of 86 (71 %) |
| 903 forward onto the knees | 62 of 85 (73 %) |
| 909 backwards, ending sitting | 65 of 85 (76 %) |
| the other 15 types | 75 % to 100 %; 901, 902, 907 and 908 caught every time |

**Where it raises false alarms:** lying down on a bed (18 of 87 recordings) and getting up from
it (19 of 88); 7 more across bending to pick something up, stumbling and tripping; none while
walking, jogging, sitting or coughing.

**The confirmation stage** (switched off in the product, D-110) would have lost 98 of the 1,509
caught falls while removing 2 of the 44 alarms: its gate fails again, on data it was never fitted
to, so it stays off.

## Limits

- Simulated falls by young volunteers onto mats, not real falls of older people.
- A research IMU (Xsens MTw) on the right wrist, with its own axes, not the IRA watch.
- 25 Hz resampled to 50 Hz: impact content above 12.5 Hz is not restored, which can only lower the
  impact peaks the rule sees (pessimistic for sensitivity, optimistic for false alarms).
- Subject 107's archive has a damaged entry: only five of its daily activities unpacked, none of its
  falls; three recordings were unreadable (listed in the JSON).

## What it means for the product

The two weakest cases matter for older people: **rolling out of bed**, and **slow, wall-assisted
collapses**. Both have low or spread-out impacts. They are written into the model card's known
limits; any change to the detector to address them must be trained on other licensed data and
re-tested here as a held-out set (D-113), never tuned on these recordings.
