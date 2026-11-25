# IRA CARE: the film's narration

Four to six minutes at 1920 × 1080. The voice-over is spoken by an open-source voice (Piper,
with the `en_GB-cori-high` voice whose training data is public domain; see `RECORDING.md` for
the versions and licences). Every recorded
scene is a real screen from the staging stack, with the made-up people of the demonstration.
What IRA says on the watch is never written here: it is IRA's own words, composed by the open
model on the staging machine during the recording, caught from the watch page's speech call and
spoken by the same open voice a little higher, with an "IRA" caption. Nothing dials emergency
services.

| # | Scene | Seconds | Words |
| --- | --- | --- | --- |
| 1 | Title | 6 | IRA CARE. A watch for someone living alone, and a care team around them. |
| 2 | The watch, turning (the Blender model) | 20 | This is the watch. A graphite case, a coral help button, a voice called IRA, and a dock for the bedside. Everything you will see next is real software, running on one ordinary machine. |
| 3 | The watch asks | 40 | Through the day, IRA checks in. The question is chosen for this person on the care team's own server: written by an open model when it answers in time, or one of IRA's set questions when it does not, and spoken aloud. They answer by voice, or with one tap. Ada answers in her own words; the care record shows a short summary, and her words stay private unless she agrees. {answered} A little later, she tells IRA about her afternoon. {talk} IRA's first words come at once, and the rest is the open model's own, from the same machine. |
| 4 | A reading outside the range: IRA asks first | 35 | When the heart rate stays outside the range the carer set, IRA does not raise the alarm straight away. It asks first. A tap on I'm OK is recorded; silence, or a no, tells the people who should know. |
| 5 | Help reaches Sravani's phone | 60 | One press of the help button. On the left, the watch; on the right, Sravani's phone. The watch tells Ada at once that her request was received, and the care server tells the people in her circle. {phone-alert} The alert reaches Sravani's phone, with where Ada is and how to get in. {on-my-way} She taps I'm on my way, and the wrist is told within seconds. {arrived} When she arrives, she says so, and closes the alert with what she found. |
| 6 | The coordinator's dashboard | 35 | The care coordinator sees everyone at a glance: who needs attention and why, the readings with their times, every alert with its outcome, and the timeline of the day. Each person's record reads in the order a carer needs it, and every reading shows when it was taken and when it arrived. Each person has a care plan, kept as versions so nothing is overwritten, and the team can search across the people and records they are allowed to see, never what anyone said. When the system cannot vouch for a person, because monitoring has paused, messages are not getting out or the page is out of date, the record does not say all well; it says why. |
| 7 | The carer arrives, on the watch | 35 | Visits are planned on the record, and the watch knows who is coming and when. {carer-arrives} The carer records that they have arrived. {watch-here} Within a minute the watch says so, and the check-ins pause while someone is there. |
| 8 | The morning report | 35 | After the visit, the handover is written down. Each morning the server writes the day's report for the carers: check-ins, readings, visits and alerts, in summaries, never the person's own words. {report} A coordinator can also make one at any time, as here. |
| 9 | Private by design | 25 | No conversation leaves the care team's machine, and no hosted model service exists in this product. The person decides who may read what they say, and can change what IRA remembers. The demonstration pages used to make this film are switched off in a production build. And the whole care journey, twenty-two steps from setting up a care team to recovering after a lost connection, runs as one automated test against the real server. |
| 10 | The hardware | 30 | The watch's design is open too. Cover glass, display, frame, the case with its buttons, the logic board, the battery, the sensor board and the back cover, with the parts, the drawings and the plan to certify it. A turntable, the renders and a viewer you can turn and explode are in the repository. On the watch, falls are noticed by rules and by a small model trained only on openly licensed data; it has not yet been measured on older people or on this watch. |
| 11 | Closing | 10 | IRA CARE is not a medical device and makes no diagnosis. It is built by Praveen Kamineti and Sravani Perni. |

## How it is made

1. `scripts/staging/clean_clone_proof.sh --keep` brings the stack up from a clean clone, with
   the open models (`gemma4:e4b`, then `gemma4:e2b`) served by Ollama on the same machine.
2. `scripts/film/record_scenes.mjs` records scenes 3 to 9 into `docs/film/raw/` (not committed):
   the pages drawn at 1.5 times their size, so 1080 pixels high; the zooms on the key moments
   (the question, the ask-first question, the alert on the phone, "I'm on my way" on the wrist,
   the carer arriving, the report) are done in the page, and every line IRA speaks is saved
   with its time.
3. `scripts/film/make_film.sh` speaks the table above with Piper, places IRA's own lines where
   they were said and the voice-over between them (a sentence that starts with `{mark}` waits for
   that moment of the recording, such as `{phone-alert}`), cross-fades the title card, the turntable
   (`docs/hardware/turntable.mp4`), the scenes and the renders, captions every line, and writes
   `docs/film/ira_care.mp4` (the last run, 25 November 2026, on `main` at `c6e31dd`: 1920 × 1080 at
   30 frames a second, 5 minutes 56 seconds, 17.4 MB, H.264 and AAC; `RECORDING.md`). IRA's lines
   in it, as the staging machine said them: the check-in "Ada, it's me. Any aches or pains today,
   Ada?" (one of IRA's set questions: the open model did not write this one in time); "Thank you
   for telling me, Ada." (the instant acknowledgement from the rules, D-070), then "That sounds
   lovely, Ada. A bit of fresh air always does good." (`gemma4:e4b`, its first sentence 9 s after
   Ada spoke, on a 4-core machine with no GPU); "Ada, are you OK? Tap I'm OK, or say help." (the
   safety question); "Sending your request for help. Stay where you are." and "Help request
   received." (the watch).
