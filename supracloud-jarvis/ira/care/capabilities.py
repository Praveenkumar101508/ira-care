"""What IRA CARE measures, and what it does not (register D4).

One list, used by the API (`GET /capabilities`), the dashboard and the tests, so a promise cannot
drift from what the product does. Every entry is either:

* available: with how it is done today, in words a family member understands; or
* not_available: with the reason. IRA never estimates, infers or shows a value for these. A
  number on a screen that the watch cannot measure is worse than no number.

`readings` lists the reading kinds the watch actually sends; the backend accepts exactly those
(`schemas.VITAL_KINDS`), and the tests hold the two together.
"""
from __future__ import annotations

AVAILABLE = "available"
NOT_AVAILABLE = "not_available"

_NO_SENSOR = ("The current watch has no validated sensor for this, and IRA CARE has no clearance to report it. "
              "IRA does not estimate it.")

CAPABILITIES: tuple[dict, ...] = (
    {"key": "heart_rate", "label": "Heart rate", "status": AVAILABLE, "reading": "heart_rate",
     "how": "Measured by the watch's optical sensor. A trend for carers, checked against a range a carer or "
            "clinician sets; not a diagnosis."},
    {"key": "steps", "label": "Steps", "status": AVAILABLE, "reading": "steps",
     "how": "Counted by the watch."},
    {"key": "movement", "label": "Movement", "status": AVAILABLE, "reading": "movement",
     "how": "From the watch's motion sensor; used with check-ins to notice no answer and no movement."},
    {"key": "on_wrist", "label": "Watch on the wrist", "status": AVAILABLE, "reading": "on_wrist",
     "how": "Reported by the watch."},
    {"key": "battery", "label": "Watch battery", "status": AVAILABLE, "reading": "battery",
     "how": "Reported by the watch."},
    {"key": "fall_detection", "label": "Suspected falls", "status": AVAILABLE,
     "how": "The watch asks after a hard impact followed by stillness, and calls for help if there is no answer. "
            "It can miss falls and can be wrong."},
    {"key": "location", "label": "Location when help is asked for", "status": AVAILABLE,
     "how": "Sent with a Help request or a suspected fall, when the person has agreed to location."},
    {"key": "food_intake", "label": "Eating", "status": AVAILABLE,
     "how": "A check-in asks whether the person has eaten, and records what they say. It is what the person "
            "reports, not a measurement of what they ate."},
    {"key": "activity_estimate", "label": "Activity (estimated)", "status": AVAILABLE,
     "how": "Estimated from the watch's motion sensor, not a measurement of health: minutes a day that look active, "
            "still or like other arm movement, and the time the watch could not tell or was not worn. Shown only when "
            "the person has agreed to share it. It never raises an alert and never says whether someone ate."},
    {"key": "hearing_access", "label": "Hearing access", "status": AVAILABLE,
     "how": "Voice can be switched off for the person; questions then come as text with a vibration, answered "
            "by touch (communication profile)."},
    {"key": "sleep", "label": "Sleep", "status": NOT_AVAILABLE,
     "reason": "IRA does not track sleep. A bedtime routine can be set as a reminder; that is all."},
    {"key": "spo2", "label": "Blood oxygen (SpO2)", "status": NOT_AVAILABLE, "reason": _NO_SENSOR},
    {"key": "ecg", "label": "ECG", "status": NOT_AVAILABLE, "reason": _NO_SENSOR},
    {"key": "hrv", "label": "Heart rate variability", "status": NOT_AVAILABLE,
     "reason": "Not validated on the current watch. IRA does not report it."},
    {"key": "blood_pressure", "label": "Blood pressure", "status": NOT_AVAILABLE, "reason": _NO_SENSOR},
    {"key": "skin_temperature", "label": "Skin temperature", "status": NOT_AVAILABLE,
     "reason": "The current watch does not send it."},
    {"key": "camera", "label": "Camera", "status": NOT_AVAILABLE,
     "reason": "The watch has no camera, and IRA CARE does not use cameras in the home."},
)


def readings() -> tuple[str, ...]:
    """The reading kinds the watch sends: exactly what the backend accepts."""
    return tuple(c["reading"] for c in CAPABILITIES if c["status"] == AVAILABLE and c.get("reading"))


def public() -> dict:
    return {"capabilities": [dict(c) for c in CAPABILITIES]}
