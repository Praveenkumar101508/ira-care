# IRA CARE: data protection impact assessment

**DRAFT FOR REVIEW BY A QUALIFIED PERSON.** Written by the build; not reviewed by a data
protection officer; claims no compliance. The processing described is what the code does.

## 1. Why a DPIA

Health-adjacent data (heart rate, movement, what a person says, location at an emergency)
about people who may be vulnerable, processed continuously, with monitoring of a person's
daily life. That is high risk on its face.

## 2. The processing

| Data | Source | Purpose | Who sees it | Retention |
| --- | --- | --- | --- | --- |
| Name, preferred name, timezone, language | Carer | Addressing the person; scheduling | Circle and staff | With the record |
| Home address, coordinates, radius, emergency summary | Carer | Responders know where to go | Circle and staff; in SOS messages | With the record |
| Access notes (key safe, door, pets) | Carer | Entry in an emergency | Tier 1 and 2 only, while an alert is open; every view logged | With the record; sealed at rest |
| Heart rate, steps, movement, worn, battery | Watch | The three event rules; reports | Staff; family under the health-data consent | Per-person retention (12 months default) |
| Check-in questions and answers | Watch | Knowing how the person is | Summaries to all; words under transcript consent | Per-person retention |
| What the person says to IRA (transcripts) | Watch, phone | Conversation; memory | Under transcript consent | Per-person retention |
| Rolling daily summary | Derived | IRA's memory | Staff and members (summaries only) | Per-person retention |
| Location at an SOS | Watch, once | Responders know where to go | Circle and staff on that alert | With the alert |
| Alerts, attempts, acknowledgements | System | The record of what happened | Circle and staff | Per-person retention once resolved |
| Access log | System | Who looked | Managers | Audit retention; not erased with the person |
| Accounts: email, hashed password, role, phone | Members | Sign-in; being told | The organisation | Account lifetime |
| Care plan versions (goals, support, routine, risks, emergency instructions, author) | Care managers | Shared plan of care | Anyone who may see the person; written by managers only | With the person; every version kept (D-078) |
| Email address of circle members; alert emails | Members; system | Being told by email when an alert escalates (off unless configured) | The member; the operator's mail provider | Delivery records per-person retention; message text never logged |
| Alert hand-over to a monitoring centre (alert kind, address, circle contact) | System | Tier 3 hand-over to an alarm-receiving centre (off unless configured; none contracted) | The centre, under the operator's contract | The centre's retention; ours with the alert |
| Encrypted database backups, copied off site | System | Recovery | The operator; the storage provider sees only encrypted files | `BACKUP_KEEP_DAYS` locally; the bucket's lifecycle off site |

## 3. Necessity and proportionality

- Readings are the minimum the three rules need; no raw accelerometer signal leaves the
  watch (D-032); audio is never stored (text only).
- Location is read once, at an SOS, never continuously (D-029).
- The person's words are shown only under their recorded consent, per role (D-016 to D-020);
  summaries never quote them (D-020).
- The daily summary is summaries of summaries; never the words.
- Retention is per person and enforced (D-022).
- No analytics, advertising or third-party sharing; providers are off by default.

## 4. Consultation

Not done. The people looked after, their families and carers should be consulted on the
check-in frequency, the night rules, the conversation feature and the family's visibility.

## 5. Risks and measures

| Risk | Measure | Residual |
| --- | --- | --- |
| Family sees more than the person wants | Consent per role; summaries without words; the Conversation tab says why words are hidden | Medium: depends on honest recording of consent |
| Staff browse records without need | Every read logged, visible to managers; roles | Medium |
| Access notes leak | Sealed per row; opened only during an alert for tier 1 and 2; logged | Low |
| Location tracked | Only at an SOS; stored with the alert; no route accepts a location otherwise (tested) | Low |
| Model provider sees the person's words | Default model is on the operator's own server or off; the operator must name any remote model here | Depends on configuration |
| A breach of the server | HTTPS, hashed credentials, rate limits, RLS, encrypted backups; no penetration test yet | Medium until tested |
| Data kept too long | Retention job; access log kept as evidence | Low |
| The person cannot exercise rights (capacity) | Consent may be given by a legal representative and is recorded as such | Medium: process is the operator's |

## 6. Lawful basis, roles and transfers

To be set by the operator with advice: likely consent and legitimate interests for the care
circle, with special-category data under the care context or explicit consent. The operator
is controller; the software's maker is not a processor unless contracted. No transfers out
of the UK by default.

## 7. Outcome

Residual risks are medium in several places until consultation, a security test and a pilot
have happened. The DPO's sign-off, and whether to consult the ICO, are for the operator.

## 8. Speech recognition (requirement C-13.d, 25 November 2026)

Speech is turned into text by the recogniser of the device the person speaks to. IRA CARE never
receives, stores or sends audio; only the recognised text reaches IRA, the same as if it were typed.

| Where | Recogniser | Where the audio may be processed | What IRA receives |
| --- | --- | --- | --- |
| Watch (`watch/app/src/main/java/care/ira/watch/voice/Listener.kt`) | Android's `SpeechRecognizer`, asked to prefer on-device recognition (`EXTRA_PREFER_OFFLINE`) | On the watch where the platform has an offline model; otherwise the platform's recognition service, which may be a cloud service of the watch's maker or Google, under their terms. The preference is a request, not a guarantee | One utterance's text |
| Phone (`mobile/src/voice/voice.ts`) | The phone's own recogniser through `expo-speech-recognition` (Apple's Speech framework on iPhone, Google's on Android) | On the device or on Apple's or Google's servers, under their terms; the app does not force on-device recognition | One utterance's text |
| Care server | None: the care deployment exposes no audio route (`tests/care/test_care_server.py`) | — | — |

Measures:

- The microphone is used only while the person is answering or has pressed Speak; one utterance
  at a time; no wake word; no recording kept.
- The permission prompts say that the phone or watch does the listening.
- What the person says is a transcript under the transcript consent (section 2); the
  recogniser's own processing is outside IRA CARE's control.

Residual risk: **medium** until the operator has read and recorded the platform terms of the
watches and phones it supports (Google's and Apple's speech processing terms, and the watch
maker's), and decided whether to require on-device recognition where the platform offers it.
That decision belongs in section 6 with the lawful basis. A forced on-device mode is not built.

## 9. Checked against current guidance (25 November 2026)

- **ICO:** the ICO's guidance pages on special category data and on DPIAs (ico.org.uk) could
  **not** be read from the build environment (the network refused the host). This DPIA has
  therefore **not** been checked against the ICO's current text; the data protection officer
  must do so. Nothing here should be read as reflecting the ICO's current guidance.
- **MHRA:** read on gov.uk on 25 November 2026 (see `INTENDED_PURPOSE.md`): if the product is
  a medical device, the strengthened post-market surveillance requirements in force in Great
  Britain since 16 June 2025 add duties on incident data, which this DPIA's retention for alerts
  and attempts must allow for.
