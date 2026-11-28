# The fall pipeline's low-impact setting (D-115, 28 November 2026)

The shipped fall pipeline missed mostly low-impact falls on Erciyes (D-113): onto the knees,
sliding down a wall, rolling out of bed. Its model only looks at an event whose peak reaches
`candidateG` (2.0 g) and raises at `threshold` (0.95); a soft fall never reaches it. This changes
those two numbers only. The model file (`fall_model.tflite`, SHA-256 unchanged), the rule detector,
the countdown and everything else stay as they were.

Script: `scripts/eval/fall_lowimpact_candidate.py`. Data fetched outside the repository with the
training scripts' pins and deleted after the runs.

## 1. Chosen on development data only

BITS-2 (Zenodo 10013090, CC BY 4.0: all fall and daily-activity recordings) and UCI HAR (UCI 240,
CC BY 4.0: every segment), 6.2 hours of daily activity. The model was
trained on all BITS-2 subjects, so its BITS-2 catch rate is in-sample; it is used here to check no
caught fall is lost and to count alarms. The rule, written in the script before Erciyes was read:
*The development rule, fixed before any held-out data is read: among settings that lose no
fall the shipped setting catches on BITS-2, and add no more than 2 alarms per hour of daily
activity over the shipped setting (BITS-2 activities and UCI HAR together), the one that catches
the most BITS-2 falls; ties go to the higher candidateG, then the higher threshold.* (`lowimpact_dev.json`).

| candidateG | threshold | BITS-2 falls caught | lost against shipped | daily-activity alarms |
|---|---|---|---|---|
| 2.0 | 0.95 | 217 of 316 | 0 | 10 (1.613 an hour) |
| 2.0 | 0.9 | 220 of 316 | 0 | 17 (2.742 an hour) |
| 2.0 | 0.8 | 225 of 316 | 0 | 23 (3.71 an hour) |
| 1.8 | 0.95 | 218 of 316 | 0 | 10 (1.613 an hour) |
| 1.8 | 0.9 | 221 of 316 | 0 | 18 (2.903 an hour) |
| 1.8 | 0.8 | 226 of 316 | 0 | 23 (3.71 an hour) |
| 1.6 | 0.95 | 220 of 316 | 0 | 11 (1.774 an hour) |
| 1.6 | 0.9 | 223 of 316 | 0 | 19 (3.065 an hour) |
| 1.6 | 0.8 | 228 of 316 | 0 | 25 (4.032 an hour) |
| 1.4 | 0.95 | 221 of 316 | 0 | 11 (1.774 an hour) |
| 1.4 | 0.9 | 223 of 316 | 0 | 19 (3.065 an hour) |
| 1.4 | 0.8 | 229 of 316 | 0 | 25 (4.032 an hour) |

**Chosen: 1.6 g and 0.9.**

## 2. Confirmed once on held-out data

Erciyes (UCI 455, CC BY 4.0; A.T. Özdemir and B. Barshan, doi:10.24432/C52028), right wrist, run once
with the shipped and the chosen setting (`lowimpact_erciyes.json`); nothing was changed afterwards.

| | Falls caught | Daily-activity recordings with an alarm |
|---|---|---|
| shipped (2.0 g, 0.95) | 1509 of 1722 (87.6%) | 44 of 1414 (3.1%) |
| **chosen (1.6 g, 0.9)** | **1563 of 1722 (90.8%)** | 59 of 1414 (4.2%) |

**No fall the shipped setting caught was lost** (0).

Where it changed (recordings with an alarm):

| Activity | Recordings | Shipped | Chosen |
|---|---|---|---|
| 809 AyakBurkulmasi | 86 | 3 | 4 |
| 815 YatagaUzanma | 87 | 18 | 25 |
| 816 YataktanKalkma | 88 | 19 | 26 |
| 903 OneDogruDizlerUzerine | 85 | 62 | 72 |
| 904 OneDogruDizlerUzerineArdindanUzanma | 88 | 81 | 84 |
| 905 OneCabukKalkma | 85 | 79 | 83 |
| 909 GeriyeDogruOturma | 85 | 65 | 71 |
| 910 GeriyeDogruUzanma | 87 | 80 | 81 |
| 911 GeriyeDogruSagli | 87 | 81 | 82 |
| 912 GeriyeDogruSollu | 86 | 83 | 84 |
| 914 YanaSagliCabukKalkma | 85 | 76 | 78 |
| 915 YanaSollu | 86 | 78 | 81 |
| 916 YanaSolluCabukKalkma | 86 | 51 | 57 |
| 917 YataktanDusme | 89 | 40 | 45 |
| 919 Bayilma | 87 | 80 | 82 |
| 920 BayilmaDuvar | 86 | 61 | 71 |

## Decision

Shipped in `watch/app/src/main/assets/fall_model.json` (the watch's unit tests pin it,
`RecoveringFallDetectorTest`). It meets the model card's rule (no caught fall lost), gains most in
the weak low-impact cases, and the cost is about one percentage point more daily-activity recordings
raising the countdown, almost all from lying down on or getting up from a bed. A countdown the person
cancels is the lesser error for people who live alone; the night-time cost around bed is written
into the hazard log's watch list for the clinical safety officer (I.j).

Limits: simulated falls by young volunteers onto mats; a research IMU on the right wrist resampled
from 25 Hz; not older people falling at home, and not the IRA watch. Erciyes has now been used for one
decision; the next change needs another held-out set.
