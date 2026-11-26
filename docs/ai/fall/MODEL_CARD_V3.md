# IRA Care Fall v3: model card

**Not a medical device.** A suspected fall starts a 30-second countdown on the watch that asks
the person "are you OK?" first; nobody is called unless they do not answer. Measured on young
volunteers simulating falls with a research wrist band, not on older adults and not on the IRA
watch. Real use needs the owner's and a clinical reviewer's approval (D-080).

## What it is

The same small network as v2 (three 1-D convolutions, 6,705 parameters, an 18,216-byte TFLite
file) over a 4 s window of x, y, z and |a| in g at 50 Hz. What changed is how the watch uses it:

| | v1 / v2 ("veto") | v3 ("recover") |
|---|---|---|
| Opens a candidate | the rule's impact, 2.5 g | an impact of 2.0 g |
| The model's window | 2 s after the rule's impact | the 1.5 s settle period's peak at index 100, scored 2 s after it |
| The model can | only clear an impact the rule saw | raise a suspected fall the rule missed |
| Stillness | the rule's 20 s still window always required | not required on the model's path; the countdown asks instead |
| The rules | the only way to raise a fall | unchanged, and still raise every fall they raise today (the backstop) |

Code: `watch/app/src/main/java/care/ira/watch/sensors/RecoveringFallDetector.kt`, chosen when the
spec's `mode` is `recover`. Training: `scripts/training/fall_model_v3.py`.

## Data (licences read at the source, 25 November 2026)

- **BITS-2 Dataset for Fall Detection**, Zenodo 10013090, CC BY 4.0, the authors' own record
  (P. Nandi and K.R. Anupama, BITS Pilani Goa): a left-wrist MPU6500, 40 volunteers used (user1
  excluded, recorded at about 6 Hz), 16 daily activities and 8 simulated falls.
- **UCI HAR**, UCI 240, CC BY 4.0: daily-activity negatives only.

No SisFall, KFall or FallAllD data. Data downloaded outside the repository, never committed.

## The gate, and how it was kept honest

The design (2.0 g candidate, peak alignment, no stillness on the model's path, threshold rule)
was fixed before any held-out result was seen. Five folds grouped by subject; in each, the
threshold was chosen on validation subjects drawn from that fold's training subjects: the highest
threshold (at most 0.95) at which validation falls are caught at least as often as the rule
catches them there, plus 0.05. Four of the five folds reached the 0.95 ceiling; one chose 0.93.

The gate, on held-out subjects, against the rule detector on the **same 316 falls** and the
**same 2.233 hours** of daily-activity recordings: sensitivity at least the rule's, and fewer
false alarms per hour.

| Held out (`results_v3.json`) | Rule detector | v3 model path | Shipped: v3 or the rule's complete 20 s still window |
|---|---|---|---|
| Falls caught (of 316) | 195 | 241 | 246 |
| Sensitivity | 0.6171 | **0.7627** | **0.7785** |
| Alarms on daily activity | 118 of 1,075 impacts | 22 of 955 candidates | at most 23 |
| Alarms per hour of daily activity | 52.84 | **9.85** | **at most 10.30** |
| Specificity on daily-activity events | 0.890 | 0.977 | — |
| Precision | 0.623 | 0.916 | — |
| Gate | — | **passed** | **passed** |

- **Cross-dataset negatives:** 0 of 5,847 UCI HAR windows (another dataset, another body site)
  reached the threshold.
- **Which falls:** both caught 175; v3 alone 66; the rule alone 20; neither 55.
- **The 20 the rule caught that the model path missed:** the rule's backstop still runs on the
  watch, so a person who stays still is raised exactly as today. Of those 20, 15 count for the rule
  only because the recording ended before 20 s without movement (the convention the rule is scored
  with: an unfinished still window counts as an alarm). On a watch the rule raises only a
  complete 20 s still window, which the backstop keeps; scored that way the shipped pipeline
  catches 246 of 316.
- **Missed by both: 55**, 45 of them because the wrist never reached 2.0 g.
- **The false-alarm rate is per hour of BITS-2 daily activity**, a mix far denser in jumps, sits and
  lies than an older person's day. It is a comparison between detectors on the same recordings,
  not a prediction of alarms in a real home.

Reproducibility: two full runs gave the same thresholds, the same held-out numbers and a
byte-identical TFLite file (SHA-256 `2cab7c9b…3278`, pinned in `FallModelReleaseTest`).
TFLite agrees with Keras on 200 of 200 windows.

## Limits

- Young adult volunteers simulating falls on mats, not older adults falling at home.
- A research band on the left wrist, not the IRA watch; its sampling rate is inferred (50 Hz).
- 40 subjects; per-fall-type results are in `results_v3.json` (fall7 is the weakest: 16 of 40 for
  both the rule and v3).
- Latency on a watch CPU is unmeasured (one TFLite invoke takes about 0.011 ms on the x86 build
  machine, `cross_dataset_weda.json`).

## Cross-dataset evaluation (F4.f)

Done on 26 November 2026 on **WEDA-FALL** (ODC-By 1.0 in the clone used, with a provenance caveat of 26 November 2026 in `CROSS_DATASET_WEDA.md`; a Fitbit Sense on the wrist), with the
shipped file, threshold and rules unchanged and nothing tuned: `CROSS_DATASET_WEDA.md`,
`cross_dataset_weda.json`.

- **Falls:** the shipped pipeline caught 326 of 350 falls by 14 young volunteers onto a mattress
  (0.931). The model path caught the same 326; the rule caught 113, counting impacts still
  waiting for their 20 s when the recording ended.
- **Missed falls:** 20 of the 24 were detected while an earlier alarm's countdown was running, and
  the countdown dropped the fall's own detection.
- **False alarms:** on 157 recordings of daily activities by 11 older adults aged 77 to 95, the
  model path raised the countdown on 25 (15.9%). Most came from hitting a table (11 of 31) and
  clapping (10 of 32).
- **Model candidates:** specificity 0.840 there and 0.862 for the young volunteers, against
  0.977 on BITS-2.
- **Open findings for the owner and a clinical reviewer** (nothing was changed because of them):
  - false alarms from everyday hand impacts;
  - a detection during a running countdown is ignored.
- **Not shown:** older adults' falls, or the IRA watch.
