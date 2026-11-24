# IRA CARE: the film's narration

Four to six minutes. Spoken by an open-source voice (Piper, MIT, with the `en_GB-cori-high`
voice whose training data is public domain). Every scene is a real screen recorded from the
staging stack, with the made-up people of the demonstration. Nothing dials emergency
services.

| # | Scene | Seconds | Words |
| --- | --- | --- | --- |
| 1 | The watch, turning (the Blender model) | 20 | IRA CARE is a watch for someone living alone, and a care team around them. This is the watch: a round case, a red help button, a voice called IRA, and a dock for the bedside. Everything you will see is real software, running on one ordinary machine. |
| 2 | The watch asks | 35 | Through the day, IRA checks in. The question is spoken aloud, in the person's own name, at the pace they prefer. They answer by voice, or with one tap. Every answer goes to the care record, and a missed answer is noticed within minutes. |
| 3 | A reading outside the range | 40 | The watch keeps the heart rate. When three readings in a row fall outside the range the carer set, IRA does not panic. It asks first. If the person says they are fine, that is recorded. If not, or if no one answers, the people who should know are told. |
| 4 | Help from the wrist | 40 | One press of the red button, or the word help, and the family and carers are told at once, with where the person is. The watch itself says what is happening: help request received, someone is on the way, someone has arrived. The wrist is never left guessing. |
| 5 | The family's phone | 45 | On the phone, the family see the alert with the whereabouts and the entry instructions. They say I'm on my way, which is a promise with a deadline. They say they have arrived. They close it with what they found. Nothing closes without an outcome. |
| 6 | The coordinator's dashboard | 50 | The care coordinator sees everyone at a glance: who needs attention now and why, which visits are due or overdue, which alerts await a response, which watches to check. Each person has a record read in the order a carer needs it, with every reading's time and every alert's outcome. |
| 7 | A visit | 35 | Visits are planned on the record. A carer who is late is noticed; a visit that does not happen becomes an alert, and IRA asks the person an extra check-in. When the carer arrives, the watch buzzes and the check-ins pause. The handover is written down. |
| 8 | Private by design | 35 | The words come from open models running on the care team's own machine. No conversation leaves it, and no hosted model service exists in this product. The person decides who may read what they say, and can change what IRA remembers of any day. |
| 9 | The hardware | 30 | The watch's own design is open too: the parts, the drawings, the power budget, and the plan to certify it, ready for a hardware engineer. IRA CARE is built by Praveen Kamineti and Sravani Perni. |

## How it is made

1. `scripts/staging/clean_clone_proof.sh --keep` brings the stack up from a clean clone.
2. `scripts/film/record_scenes.mjs` records scenes 2 to 8 into `docs/film/raw/` (not committed).
3. `scripts/film/make_film.sh` speaks the table above with Piper, cuts the turntable
   (`docs/hardware/turntable.mp4`), the scenes and the renders to length, and writes
   `docs/film/ira_care.mp4` (1280 × 720, H.264; the last run: 5 minutes 30 seconds, 3.9 MB).
