# Review pack: F8.a, speech engine licences reviewed

**Requirement.** F8.a "Speech engine licences reviewed (OmniVoice is CC BY-NC)" (area ai; parent F8).

**Current evidence.** `docs/ai/MODEL_WEIGHTS.md` (every speech model, its licence, how it was
read and when); `docs/security/LICENCES.md` (the PyAV exclusion, D-083; the image scan);
`docs/ai/speech/RESULTS.md` (which recogniser runs and its 4.16 % WER on LibriSpeech
test-clean); `supracloud-jarvis/ira/core/models/artefacts.py` (pinned revisions);
`supracloud-jarvis/ira/tests/architecture/test_no_pyav.py` (PyAV never ships).

**What is in use, and its licence (from `MODEL_WEIGHTS.md`).**

| Engine or model | Role | Licence | Commercial use |
|---|---|---|---|
| faster-whisper `small` (`Systran/faster-whisper-small`, pinned) | server speech to text | MIT (Whisper weights MIT) | yes |
| Silero VAD v6 (inside the faster-whisper wheel) | voice activity detection | MIT | yes |
| Android `SpeechRecognizer` and `TextToSpeech`; `expo-speech` and the platform recogniser | watch and phone speech, no shipped weights | platform terms | not a model licence; DPIA question C-13.d |
| Piper and the `en_GB-cori-high` voice | narrates the demonstration film only | MIT; voice data public domain | yes (tooling, not product) |
| OmniVoice (`k2-fsa/OmniVoice`, pinned) | voice sidecar, foundation only, **not enabled** | code Apache 2.0; **weights CC BY-NC** | **no** |
| Kokoro-82M | legacy voice, foundation only | Apache 2.0 | yes |
| Indic Parler-TTS | foundation only, gated | Apache 2.0 | yes, with gate conditions |
| ECAPA speaker embeddings | voice-print lock, foundation only | Apache 2.0 | yes; biometric data, DPIA first |
| PyAV | **excluded** (GPL FFmpeg build); IRA's WAV decoder in its place | would be GPL | excluded; `test_no_pyav.py` |

**Acceptance criterion.** Every speech engine and model the product can load has a recorded
licence read at its source with a date; anything non-commercial or copyleft is either excluded
from the commercial build with a test that proves it, or marked as not to be enabled; attribution
notices exist for what ships.

**The open point for the reviewer and the owner.** OmniVoice's weights are CC BY-NC. It is not
enabled and is listed as "must not be enabled in a commercial deployment", but the code to load it
remains in the repository (`artefacts.py`). The options are: (a) keep it as foundation code with
the prohibition recorded (as now) and add a start-up refusal when a commercial edition names it;
(b) remove the artefact entry altogether. The owner decides; the reviewer advises.

**Review questions.**
1. Does the table above match `MODEL_WEIGHTS.md` and `artefacts.py` exactly (no engine missing)?
2. Is the platform-engine question (does audio leave the device on Android or iOS?) answered in
   the DPIA (C-13.d), and is that enough for F8.a or is it a separate item?
3. Should OmniVoice be refused at start-up in commercial editions, or removed?
4. Are the MIT and Apache notices for faster-whisper, Silero and Kokoro present in
   `docs/legal/notices` and the licences screen?

**Decision to record.**

| | |
|---|---|
| Reviewer | Sravani Perni ([sravaniperni](https://github.com/sravaniperni)) |
| Date | 27 November 2026 |
| Decision (accept / changes needed) | accept (recorded by the owner, Praveen Kamineti, on Sravani Perni's behalf after her review) |
| OmniVoice option chosen (owner) | not yet chosen: OmniVoice stays off and listed as "must not be enabled in a commercial deployment" (option (a) as it stands today) until the owner decides between (a) and (b) |
