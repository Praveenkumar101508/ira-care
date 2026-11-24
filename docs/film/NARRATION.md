# IRA CARE: the film's narration

Four to six minutes at 1920 × 1080. The voice-over is spoken by an open-source voice (Piper,
MIT, with the `en_GB-cori-high` voice whose training data is public domain). Every recorded
scene is a real screen from the staging stack, with the made-up people of the demonstration.
What IRA says on the watch is never written here: it is IRA's own words, composed by the open
model on the staging machine during the recording, caught from the watch page's speech call and
spoken by the same open voice a little higher, with an "IRA" caption. Nothing dials emergency
services.

| # | Scene | Seconds | Words |
| --- | --- | --- | --- |
| 1 | Title | 6 | IRA CARE. A watch for someone living alone, and a care team around them. |
| 2 | The watch, turning (the Blender model) | 20 | This is the watch. A graphite case, a coral help button, a voice called IRA, and a dock for the bedside. Everything you will see next is real software, running on one ordinary machine. |
| 3 | The watch asks | 40 | Through the day, IRA checks in. The question is composed for this person, by an open model on the care team's own server, and spoken aloud. They answer by voice, or with one tap, and IRA answers back at once. |
| 4 | A reading outside the range: IRA asks first | 35 | When the heart rate stays outside the range the carer set, IRA does not raise the alarm straight away. It asks first. A tap on I'm OK is recorded; silence, or a no, tells the people who should know. |
| 5 | Help reaches Sravani's phone | 60 | One press of the help button. On the left, the watch. On the right, Sravani's phone, where the alert arrives with where Ada is. Sravani taps I'm on my way, and the wrist is told within seconds. When she arrives, she says so, and closes the alert with what she found. |
| 6 | The coordinator's dashboard | 35 | The care coordinator sees everyone at a glance: who needs attention and why, the readings with their times, every alert with its outcome, and the timeline of the day. |
| 7 | The carer arrives, on the watch | 35 | Visits are planned on the record, and the watch knows who is coming and when. When the carer arrives, the watch says so, and the check-ins pause while someone is there. |
| 8 | The morning report | 35 | After the visit, the handover is written down. Each morning the server writes the day's report for the carers: check-ins, readings, visits and alerts, in summaries, never the person's own words. |
| 9 | Private by design | 25 | No conversation leaves the care team's machine, and no hosted model service exists in this product. The person decides who may read what they say, and can change what IRA remembers. |
| 10 | The hardware | 30 | The watch's design is open too. Cover glass, display, frame, the case with its buttons, the logic board, the battery, the sensor board and the back cover, with the parts, the drawings and the plan to certify it. |
| 11 | Closing | 6 | IRA CARE is built by Praveen Kamineti and Sravani Perni. |

## How it is made

1. `scripts/staging/clean_clone_proof.sh --keep` brings the stack up from a clean clone, with
   the open models (`gemma4:e4b`, then `gemma4:e2b`) served by Ollama on the same machine.
2. `scripts/film/record_scenes.mjs` records scenes 3 to 9 into `docs/film/raw/` (not committed):
   the pages drawn at 1.5 times their size, so 1080 pixels high; the zooms on the key moments
   (the question, the ask-first question, the alert on the phone, "I'm on my way" on the wrist,
   the carer arriving, the report) are done in the page, and every line IRA speaks is saved
   with its time.
3. `scripts/film/make_film.sh` speaks the table above with Piper, places IRA's own lines where
   they were said and the voice-over between them, cross-fades the title card, the turntable
   (`docs/hardware/turntable.mp4`), the scenes and the renders, captions every line, and writes
   `docs/film/ira_care.mp4`.
