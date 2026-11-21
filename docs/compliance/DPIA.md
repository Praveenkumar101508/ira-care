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
