# IRA CARE: product claims register

**DRAFT FOR REVIEW BY A QUALIFIED PERSON.** Every claim the product makes, in its own words,
with the evidence behind it and how far that evidence goes. Nothing here has been assessed by
a regulator, a notified body or a Clinical Safety Officer. The product is not a medical device
and makes no diagnostic or treatment claim (`INTENDED_PURPOSE.md`).

Evidence levels:

- **software-tested**: an automated test proves the software does what the claim says, on
  synthetic or recorded data. Says nothing about real people, real watches or real networks.
- **unmeasured**: the software exists, but how well it works in the real world has not been
  measured. The product says so in its own text.
- **not claimed**: the product says plainly that it does not do this, and shows no value.

The `key` column is the key in `supracloud-jarvis/ira/care/capabilities.py`, which is what the
API, the dashboard and the watch show. `test_claims_register.py` fails if a capability is added
there without a row here, or if a row here disagrees with its status there.

## What the watch measures or reports

| key | Claim (product words) | Evidence | Level |
|---|---|---|---|
| heart_rate | Measured by the watch's optical sensor; a trend for carers, checked against a range a carer or clinician sets; not a diagnosis | `care/continuous.py` rules and `test_care_continuous_db.py` (two readings in a row, ask first); estimator accuracy on PPG-DaLiA in `docs/ai/` | software-tested; accuracy on the IRA watch and on older adults **unmeasured** |
| steps | Counted by the watch | the watch platform's counter; stored and shown as sent | software-tested (transport only) |
| movement | From the watch's motion sensor; used with check-ins to notice no answer and no movement | `care/monitoring.py::evaluate_missed_checkin`, `test_care_escalation_db.py` | software-tested; sensitivity in real homes **unmeasured** |
| on_wrist | Reported by the watch | the platform's off-body sensor; off-wrist suppresses fall alerts (`FallDetector.kt`) | software-tested (transport only) |
| battery | Reported by the watch | stored and shown as sent; low battery raises a notice | software-tested |
| fall_detection | Asks after a hard impact followed by stillness, and calls for help if there is no answer; it can miss falls and can be wrong | `FallDetectorTest.kt`, `FallCountdownTest.kt`, `test_care_fall_db.py`; research model evaluated on SisFall, debug builds only (D-077) | software-tested; real-world sensitivity and false-alarm rate **unmeasured** |
| location | Sent with a Help request or a suspected fall, when the person has agreed | `care/location.py`, `test_care_location_db.py` | software-tested; fix accuracy indoors **unmeasured** |
| food_intake | A check-in asks whether the person has eaten and records what they say; not a measurement | `care/checkins.py`, `test_care_checkins_db.py` | software-tested (what is said, not what is eaten) |
| activity_estimate | Estimated from the watch's motion sensor, not a measurement of health: minutes a day that look active, still or like other arm movement, and the time the watch could not tell or was not worn; shown only when the person has agreed to share it; never raises an alert and never says whether someone ate | `care/activity.py`, `test_care_activity_rules.py`, `test_care_activity_db.py` (consent, contract, trend, retention, erasure), `test_activity_no_alert_path.py`, `ActivitySharingTest.kt`; model evaluated on WISDM only, macro-F1 0.790 (`docs/ai/activity/MODEL_CARD.md`) | software-tested; accuracy on the IRA watch and with older adults **unmeasured** |
| hearing_access | Voice can be switched off; questions come as text with a vibration, answered by touch | communication profile, `AccessibilityTest.kt` | software-tested; usability with people with hearing loss **unmeasured** |
| sleep | IRA does not track sleep | — | not claimed |
| spo2 | No validated sensor; IRA does not estimate it | — | not claimed |
| ecg | No validated sensor; IRA does not estimate it | — | not claimed |
| hrv | Not validated on the current watch; IRA does not report it | — | not claimed |
| blood_pressure | No validated sensor; IRA does not estimate it | — | not claimed |
| skin_temperature | The current watch does not send it | — | not claimed |
| camera | No camera; IRA CARE does not use cameras in the home | — | not claimed |

## What the service does

| Claim | Evidence | Level |
|---|---|---|
| Asks the person how they are through the day and tells the circle when there is no answer and no movement | `care/checkin_cycle.py`, `test_care_checkins_db.py`, `test_care_escalation_db.py`, hazard H1, H2 | software-tested; never tested with older adults |
| A call for help, a fall with no answer or silence after "are you OK?" goes to the care circle in tiers until someone acknowledges | `care/escalation.py`, `test_care_escalation_db.py`, `test_care_delivery_db.py`, hazards H2, H11, H16 | software-tested; delivery through a live SMS or call provider **not yet tested** |
| Only a clear "OK" ends a help check; doubt or silence keeps it open | `test_care_safety_answers_db.py`, `FallCountdownTest.kt`, hazard H15 | software-tested |
| A medication is recorded as taken only on a clear yes to that exact dose | `test_care_safety_answers.py`, `test_care_safety_answers_db.py`, hazard H14 | software-tested |
| Never dials the emergency services; the last tier tells people to call 999 | `test_care_monitoring_rules.py::test_the_system_never_dials_the_emergency_services`, hazard H9 | software-tested |
| The companion gives no medical advice or diagnosis | `care/conversation.py::guard`, `test_care_conversation_rules.py`, hazard H7 | software-tested against listed phrasings; open-ended language **not exhaustively testable** |
| Works offline: Help and readings are kept on the watch and sent when the connection returns, without duplicates | `OfflineQueueTest.kt`, `OutboxTest.kt`, idempotent replay in `care/routes.py`, hazard H18 | software-tested; on real networks **unmeasured** |
| Each family sees only the person they were invited for | `care/authz.py`, `test_care_isolation_rls.py`, hazard H10 | software-tested |

## Claims the product must not make

From the activity estimate: that the person ate, drank or had a meal (the model's
`eating_drinking` class is a hand-to-mouth wrist pattern, it is shown only as "other arm movement",
and `food_intake` stays what the person says); any statement about health, frailty, mobility
decline or a fall; any alert or escalation. `test_care_activity_rules.py` and
`ActivitySharingTest.kt` refuse those words in the activity text, and `test_activity_no_alert_path.py`
keeps every alert path from reading it.

Diagnosis; treatment; "detects all falls"; "medical-grade"; any clinical accuracy figure not
measured on the IRA watch with the intended users; approval or clearance by MHRA, the FDA or any
notified body; validation on elderly speakers. `tests/readiness` already refuses several of
these words in product text; anything new goes through the owner and a qualified reviewer.
