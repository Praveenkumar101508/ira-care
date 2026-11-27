# Synthetic wrist scenarios for the fall pipeline (round 9)

`scripts/eval/fall_synthetic_scenarios.py` makes labelled 50 Hz recordings of x, y, z in g (gravity
included, device frame) from physical reasoning, deterministic for a seed, every parameter drawn
from a range. **Nothing in them is recorded from a person**: they are a regression corpus for the
detector and the confirmation stage, so a change that loses a fall shape or stops rejecting a
hand impact is seen at once. They say nothing about accuracy on real people; that stays with the
permitted recorded datasets (`MODEL_CARD_V3.md`).

Every recording starts with two seconds of a still, worn wrist (|a| = 1 g with 0.02 g noise), so
the detector's buffers are full before anything happens.

## Falls (label `fall`)

| Kind | Shape and why |
|---|---|
| `forward`, `backward`, `sideways` | 0.6 to 1.5 s of arm movement, then free fall (gravity reading shrinks to about 0.25 g for 0.2 to 0.45 s), an impact of 2.6 to 7 g along roughly the new gravity direction with one bounce, half a second of settling, then lying still for 25 s. The gravity vector after the fall is turned 55 to 150 degrees from before: the body changed orientation |
| `from_sitting` | the same with a weaker impact (2.3 to 4.5 g) and a smaller turn (45 to 110 degrees): less height |
| `slow_collapse` | the weakest impact (2.1 to 3.2 g), near the model's 2.0 g candidate: a person sliding down |
| `fall_then_moves` | a fall after which the person stirs for six seconds (0.25 to 0.6 g of movement) before lying still: the rule's 20 s still window is not met, so only the model path can catch it |
| `fall_wrist_under_body` | the wrist pinned under the body: gravity on one axis, 3 % over 1 g, almost no noise |
| `stairs_tumble` | two to four further impacts (2.2 to 4.5 g) with the orientation changing between them, then lying still |

## Activities that provoke impact detectors (label `not_fall`)

| Kind | Shape and why |
|---|---|
| `clapping`, `knocking`, `hammering` | bursts of 3 to 30 sharp impacts (2.2 to 8 g) at 1.5 to 4.5 a second along one direction, the wrist returning to the same orientation each time and moving between hits; then the hand carries on for four seconds |
| `table_slap` | one impact of 3 to 6 g, then five seconds of the hand moving gently (0.3 to 0.6 g), then rest |
| `drop_into_chair` | a short dip, an impact of 2.2 to 3.5 g, the wrist turned a little (5 to 25 % of the way to a random direction: the arm on an armrest), eight seconds of seated fidgeting |
| `brushing_teeth` | twenty seconds of fast, large arm movement (1.2 to 2.4 g at 3 to 7 Hz) with its own peaks above 2 g |
| `watch_put_on_table` | the watch taken off (orientation changes), a tap of 2.2 to 4 g as it lands, then perfectly still (0.004 g noise). On the watch the off-body sensor sets `onWrist = false` and the detector ignores it; the synthetic run has no off-body signal, so this kind measures what the pipeline does without it |
| `wrist_shake` | two to five seconds of violent shaking (1.6 to 3.2 g at 4 to 8 Hz), then settling |
| `washing_hands` | three to seven bouts of movement with small impacts of 2.0 to 2.8 g (the sink) |
| `stairs_down` | eight to sixteen steps, each a 1.8 to 2.6 g heel strike with the wrist pointing the same way |
| `clipping_hit` | a knock of 17 to 30 g, saturated at the sensor's ±16 g, then the hand carrying on |

## Known gaps

These shapes are what a careful engineer expects from the physics of a wrist; the real signals
have texture (tremor, wrist rotation during a fall, clothing) that is not here. Two gaps matter
for the confirmation stage and are recorded in `FALSE_ALARM_INVESTIGATION.md`: a real fall in
which the wrist happens to keep its orientation and the person moves afterwards (the stage would
suppress it if the movement is strong; none of the synthetic falls has that shape because a fall
that changes the body's orientation changes the wrist's), and real hand activity whose movement is
gentler than `BUSY_SD_G` after the impact (the stage asks, as it should when unsure).
