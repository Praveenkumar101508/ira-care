# IRA CARE: supervised pilot protocol

**DRAFT FOR REVIEW BY A QUALIFIED PERSON.** Written by the build. It is a protocol to be
reviewed, amended and approved by a Clinical Safety Officer, the operator's data protection
officer and, where it is research, an ethics committee. No pilot has been run, no participant
recruited, and nothing here authorises one.

## 1. Purpose

To measure, with real people, real watches and real networks, what the software can only claim
as unmeasured today (`CLAIMS_REGISTER.md`, level "unmeasured"):

1. how many falls the watch detects, and how many false "did you fall?" questions it asks;
2. how accurate the watch's heart-rate readings are against a reference;
3. whether people answer the check-ins, and whether silence plus no movement is escalated in time;
4. whether alerts reach the care circle through a live message provider, and how fast;
5. whether the people looked after and their families find it acceptable.

The pilot does not test a medical claim. IRA CARE makes none (`INTENDED_PURPOSE.md`).

## 2. Preconditions (all must be met before the first participant)

| Precondition | Owner | Requirement |
| --- | --- | --- |
| The hazard log reviewed and signed off by a Clinical Safety Officer | Operator | I.j |
| The regulatory position settled with an MHRA adviser: if a device, the pilot is a clinical investigation and follows that route | Owner | I.k |
| The DPIA reviewed and signed off by the data protection officer, including the speech recognisers (section 8) and the lawful basis | Operator | I.d, C-13.d |
| Ethics review, if the operator or a partner treats the pilot as research | Operator | — |
| A live SMS and call provider configured and tested end to end with test numbers | Operator | G3.e |
| Signed builds of the watch and phone apps installed from a controlled channel | Owner | D1b.c |
| A monitored deployment in a UK region with backups and the runbooks in `docs/ops/RUNBOOKS.md` rehearsed | Operator | G1.e, G4.d |
| Every participant's usual safeguards stay in place (existing alarm, carers' visits); IRA CARE is added, never substituted | Operator | — |

## 3. Participants

- **Included:** adults living alone or alone for long periods, able to wear a watch and answer a
  short question, with capacity to consent (or a legal representative who consents for them,
  recorded as such), and at least one family member or carer willing to receive alerts.
- **Excluded:** anyone for whom a missed alert would be dangerous without another safeguard;
  anyone needing continuous clinical monitoring; anyone unable to consent with no representative;
  anyone whose only means of summoning help would be IRA CARE.
- **Size:** set by the Clinical Safety Officer and, for fall detection, by a power calculation.
  Real falls are rare, so fall sensitivity will rest on **simulated falls on crash mats under
  supervision** (part A) plus any real events during the home phase (part B).

## 4. Consent

- Written, informed consent from the participant (or representative) and from each circle member,
  covering: the readings collected, location at an SOS only, transcripts only under the transcript
  consent, the speech recogniser of the watch or phone, retention, and withdrawal at any time with
  erasure (`core/privacy/erasure.py`).
- The consent choices are recorded in the product's own consent records (`care/consent.py`), so
  the software enforces them.
- An easy-read information sheet. Consent is re-confirmed at each visit, not assumed.

## 5. Design

**Part A, supervised bench sessions (one visit, about two hours):**

- **Simulated falls:** forward, backward, sideways and from a chair, onto crash mats, performed
  only by participants a physiotherapist judges able to do so safely. Otherwise by younger
  volunteers wearing the same watch, recorded separately.
- **Activities of daily living:** sitting down hard, lying down, bending, stairs, dropping the
  watch on a table, taking it off.
- **Heart rate:** at rest, after light exercise, and during arm movement, against a chest-strap
  ECG reference (a validated consumer reference or a clinical device, as the CSO chooses),
  time-aligned.

**Part B, home phase (four weeks):**

- Normal use. Check-ins at the configured times; alerts go to the real circle **and**
  simultaneously to a supervising carer for the pilot.
- Every alert and every missed one is reviewed weekly.

## 6. What is measured, and how it is computed

| Measure | Definition | Source in the product |
| --- | --- | --- |
| Fall sensitivity (part A) | Falls that led to a "did you fall?" question ÷ falls performed | `care_alerts` (kind `fall`) against the session log |
| False fall questions | Questions with no fall ÷ hours of activity (part A) and per day (part B) | `care_alerts`, the participants' diary |
| Heart-rate error | Mean absolute error and limits of agreement (Bland–Altman) against the reference, per condition | `care_vitals` against the reference export |
| Check-in adherence | Answered ÷ asked, outside quiet hours | `care_checkins` |
| Escalation timeliness | Time from the trigger to the first message accepted by the provider, and to a human acknowledgement | `care_alert_attempt_events`, `care_acknowledgements` |
| Delivery | Messages reported delivered ÷ sent, per channel | `care_alert_attempt_events` (provider callbacks) |
| Acceptability | Structured interview at week 4; the System Usability Scale for circle members | Questionnaire |

The pilot pre-registers these definitions and its acceptance thresholds (set by the CSO) before
part A. The fall thresholds of model v3 stay fixed for the pilot; no retraining during it.

## 7. Stopping rules

The pilot stops for a participant, or for everyone, when:

- an escalation that should have happened did not, and a person was harmed or could have been;
- the "monitoring paused" state lasts longer than the CSO's threshold (proposed: 30 minutes);
- false fall questions exceed the CSO's threshold and distress participants;
- a data protection incident occurs (`INCIDENT_PROCESS.md`);
- a participant or their representative asks, at any time, with no reason needed.

Every stop is reviewed by the CSO before anyone restarts.

## 8. Data protection

- The DPIA (`DPIA.md`) governs. Pilot data are the product's own records plus the reference
  recordings and questionnaires. These are stored separately, pseudonymised, and deleted at the
  end of the analysis unless consent says otherwise.
- No pilot data are used to train models without separate, explicit consent; if they are, a new
  dataset row in `docs/DATASETS.md` records it.
- Participants' identities never appear in reports or the repository.

## 9. Outputs

A pilot report with the measures above against their pre-registered thresholds, every incident
and stop, and the updated hazard log. It feeds the requirements that are blocked on real-world
evidence:

- F3.e: heart-rate accuracy on the IRA watch and older adults;
- F4.g: fall detection on a physical watch;
- F8.d and F8.e: recognition accuracy on consented recordings and synthesis intelligibility;
- C-23.e: the screen-reader pass;
- J.f: the care journey on a physical watch and phone.
