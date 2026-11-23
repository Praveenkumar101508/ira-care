"""Request and response bodies for `/api/v1/care`."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

PERSON_STATUSES = ("active", "paused", "archived")
CIRCLE_ROLES = ("family", "carer", "clinician", "care_organisation")
ACK_KINDS = ("on_my_way", "arrived", "checked_in_person", "spoke_to_person", "false_alarm", "handed_over")
VITAL_KINDS = ("heart_rate", "steps", "movement", "on_wrist", "battery", "skin_temperature")
ROUTINE_KINDS = ("meal", "medication", "activity", "sleep", "social", "other")
MED_EVENT_STATUSES = ("due", "taken", "missed", "skipped", "unknown")
CONSENT_KINDS = ("transcripts_family", "transcripts_carer", "transcripts_organisation",
                 "health_data_family", "data_retention")
PLATFORMS = ("wearos", "watchos", "other")


class HomeFields(BaseModel):
    """Where responders go, and what they need to know on arrival. Free text the
    carer controls; no fields of our own for conditions. `access_notes` is
    write-only here: it is sealed at rest and read through its own route."""
    home_address: Optional[str] = Field(None, max_length=400)
    home_latitude: Optional[float] = Field(None, ge=-90, le=90)
    home_longitude: Optional[float] = Field(None, ge=-180, le=180)
    home_radius_m: Optional[int] = Field(None, ge=20, le=5000)
    emergency_summary: Optional[str] = Field(None, max_length=2000)
    access_notes: Optional[str] = Field(None, max_length=2000)

    @model_validator(mode="after")
    def _coordinates_pair(self):
        if (self.home_latitude is None) != (self.home_longitude is None):
            raise ValueError("home_latitude and home_longitude go together")
        return self


class PersonCreate(HomeFields):
    display_name: str = Field(..., min_length=1, max_length=80)
    preferred_name: str = Field("", max_length=40)
    date_of_birth: Optional[dt.date] = None
    timezone: str = Field("Europe/London", min_length=1, max_length=64)
    language: str = Field("en", pattern=r"^[a-z]{2}(-[A-Za-z]{2,8})?$")
    notes: str = Field("", max_length=4000)


class PersonUpdate(HomeFields):
    display_name: Optional[str] = Field(None, min_length=1, max_length=80)
    preferred_name: Optional[str] = Field(None, max_length=40)
    date_of_birth: Optional[dt.date] = None
    timezone: Optional[str] = Field(None, min_length=1, max_length=64)
    language: Optional[str] = Field(None, pattern=r"^[a-z]{2}(-[A-Za-z]{2,8})?$")
    notes: Optional[str] = Field(None, max_length=4000)
    status: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v is not None and v not in PERSON_STATUSES:
            raise ValueError(f"status must be one of {PERSON_STATUSES}")
        return v


class CircleMemberCreate(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=80)
    relationship: str = Field("", max_length=40)
    role: str
    tier: int = Field(1, ge=1, le=3)
    priority: int = Field(100, ge=0, le=100000)
    phone: str = Field("", pattern=r"^$|^\+[1-9][0-9]{6,14}$")
    email: str = Field("", max_length=254)
    user_id: Optional[uuid.UUID] = None
    push_enabled: bool = True
    sms_enabled: bool = True
    call_enabled: bool = True

    @field_validator("role")
    @classmethod
    def _role(cls, v):
        if v not in CIRCLE_ROLES:
            raise ValueError(f"role must be one of {CIRCLE_ROLES}")
        return v


class CircleMemberUpdate(BaseModel):
    display_name: Optional[str] = Field(None, min_length=1, max_length=80)
    relationship: Optional[str] = Field(None, max_length=40)
    role: Optional[str] = None
    tier: Optional[int] = Field(None, ge=1, le=3)
    priority: Optional[int] = Field(None, ge=0, le=100000)
    phone: Optional[str] = Field(None, pattern=r"^$|^\+[1-9][0-9]{6,14}$")
    email: Optional[str] = Field(None, max_length=254)
    push_enabled: Optional[bool] = None
    sms_enabled: Optional[bool] = None
    call_enabled: Optional[bool] = None
    active: Optional[bool] = None

    @field_validator("role")
    @classmethod
    def _role(cls, v):
        if v is not None and v not in CIRCLE_ROLES:
            raise ValueError(f"role must be one of {CIRCLE_ROLES}")
        return v


class SafetySettings(BaseModel):
    """The whole settings row. Heart-rate bounds are optional on purpose: unset
    means no heart-rate rule. There is no default range."""
    quiet_start: dt.time = dt.time(22, 0)
    quiet_end: dt.time = dt.time(7, 0)
    checkin_min_minutes: int = Field(15, ge=5, le=240)
    checkin_max_minutes: int = Field(20, ge=5, le=240)
    repeat_after_s: int = Field(60, ge=10, le=600)
    sensor_check_after_s: int = Field(120, ge=10, le=900)
    heart_rate_min: Optional[int] = Field(None, ge=20, le=250)
    heart_rate_max: Optional[int] = Field(None, ge=20, le=250)
    # Suspected falls: on by default, a carer can turn it off; thresholds, not medical claims.
    fall_detection_enabled: bool = True
    fall_countdown_s: int = Field(30, ge=10, le=120)
    fall_impact_g: float = Field(2.5, ge=1.5, le=8.0)
    fall_still_seconds: int = Field(20, ge=5, le=120)
    fall_still_threshold_g: float = Field(0.3, ge=0.05, le=1.0)
    no_movement_window_s: int = Field(180, ge=30, le=3600)
    movement_floor_steps: int = Field(1, ge=0, le=10000)
    off_wrist_alert_minutes: int = Field(30, ge=1, le=1440)
    battery_alert_percent: int = Field(15, ge=1, le=99)
    sync_alert_minutes: int = Field(30, ge=1, le=1440)
    missed_checkins_before_escalation: int = Field(3, ge=1, le=20)
    ack_wait_minutes: int = Field(5, ge=1, le=60)
    ask_first_wait_s: int = Field(60, ge=15, le=600)
    heart_rate_confirm_readings: int = Field(2, ge=1, le=10)
    heart_rate_confirm_seconds: int = Field(120, ge=0, le=3600)
    night_off_wrist_minutes: int = Field(60, ge=5, le=720)
    usual_wake_time: dt.time = dt.time(7, 30)
    wake_grace_minutes: int = Field(60, ge=10, le=240)
    # How often the watch uploads ordinary readings while its service runs. An
    # out-of-range heart-rate sample is uploaded at once whatever this says.
    reading_upload_interval_s: int = Field(60, ge=15, le=900)

    @field_validator("checkin_max_minutes")
    @classmethod
    def _window(cls, v, info):
        lo = info.data.get("checkin_min_minutes")
        if lo is not None and v < lo:
            raise ValueError("checkin_max_minutes must be at least checkin_min_minutes")
        return v

    @field_validator("heart_rate_max")
    @classmethod
    def _range(cls, v, info):
        lo = info.data.get("heart_rate_min")
        if v is not None and lo is not None and v <= lo:
            raise ValueError("heart_rate_max must be greater than heart_rate_min")
        return v


class DeviceCreate(BaseModel):
    platform: str = "wearos"
    model: str = Field("", max_length=80)
    expected_report_interval_s: int = Field(300, ge=30, le=86400)

    @field_validator("platform")
    @classmethod
    def _platform(cls, v):
        if v not in PLATFORMS:
            raise ValueError(f"platform must be one of {PLATFORMS}")
        return v


class PairRequest(BaseModel):
    pairing_code: str = Field(..., min_length=10, max_length=200)
    app_version: str = Field("", max_length=40)


class Reading(BaseModel):
    kind: str
    value: float
    recorded_at: dt.datetime
    unit: str = Field("", max_length=16)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        if v not in VITAL_KINDS:
            raise ValueError(f"kind must be one of {VITAL_KINDS}")
        return v


class DeviceReport(BaseModel):
    """What the watch sends on every sync: readings and its own state. The
    server may answer with `safety_check` when the readings opened a question."""
    readings: list[Reading] = Field(default_factory=list, max_length=2000)
    battery_percent: Optional[int] = Field(None, ge=0, le=100)
    on_wrist: Optional[bool] = None
    background_sensor_permission: Optional[bool] = None
    app_version: Optional[str] = Field(None, max_length=40)


class LocationFix(BaseModel):
    """Where the watch was when the alert started. Sent once, never streamed."""
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    accuracy_m: Optional[float] = Field(None, ge=0, le=100_000)
    recorded_at: Optional[dt.datetime] = None


class HelpRequest(BaseModel):
    """The Help button, or the spoken word, with the watch's location if it has one."""
    via: str = Field("button", pattern=r"^(button|voice|fall)$")
    note: str = Field("", max_length=400)
    location: Optional[LocationFix] = None


class PushRegistration(BaseModel):
    """A phone's push token, tied to the account's circle memberships (one person, or all)."""
    token: str = Field(..., min_length=8, max_length=200)
    platform: str = Field("", pattern=r"^(|ios|android|web)$")
    person_id: Optional[uuid.UUID] = None


class PushUnregistration(BaseModel):
    token: str = Field(..., min_length=8, max_length=200)


class Signup(BaseModel):
    """The founder of a care organisation, before verification."""
    email: str = Field(..., min_length=5, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(..., min_length=12, max_length=72)


class OrganisationCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    plan_id: str = Field("care_organisation", pattern=r"^(care_family|care_organisation)$")


class ConsentAtInvitation(BaseModel):
    kind: str
    granted: bool = True
    granted_by: str = Field("person", pattern=r"^(person|legal_representative)$")
    granted_by_name: str = Field("", max_length=80)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        if v not in CONSENT_KINDS:
            raise ValueError(f"kind must be one of {CONSENT_KINDS}")
        return v


class InvitationCreate(BaseModel):
    email: str = Field(..., min_length=5, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    display_name: str = Field("", max_length=80)
    relationship: str = Field("", max_length=40)
    role: str = Field("family", pattern=r"^(family|carer|clinician|care_organisation)$")
    tier: int = Field(1, ge=1, le=3)
    phone: str = Field("", pattern=r"^$|^\+[1-9][0-9]{6,14}$")
    consents: list[ConsentAtInvitation] = Field(default_factory=list, max_length=8)


class InvitationAccept(BaseModel):
    token: str = Field(..., min_length=16, max_length=200)


class Talk(BaseModel):
    """Something the person said to IRA."""
    text: str = Field(..., min_length=1, max_length=2000)


class FallStart(BaseModel):
    """The watch's fall detector fired and its countdown has started."""
    peak_g: Optional[float] = Field(None, ge=0, le=50)
    still_seconds: Optional[int] = Field(None, ge=0, le=600)


class Acknowledge(BaseModel):
    kind: str = "on_my_way"
    note: str = Field("", max_length=1000)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        if v not in ACK_KINDS:
            raise ValueError(f"kind must be one of {ACK_KINDS}")
        return v


class Resolve(BaseModel):
    resolution: str = Field(..., min_length=1, max_length=1000)


class RoutineCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    kind: str
    time_of_day: Optional[dt.time] = None
    days_of_week: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7])
    active: bool = True

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        if v not in ROUTINE_KINDS:
            raise ValueError(f"kind must be one of {ROUTINE_KINDS}")
        return v

    @field_validator("days_of_week")
    @classmethod
    def _days(cls, v):
        if not v or any(d < 1 or d > 7 for d in v) or len(set(v)) != len(v):
            raise ValueError("days_of_week must be distinct values 1 (Monday) to 7 (Sunday)")
        return sorted(v)


class RoutineUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    kind: Optional[str] = None
    time_of_day: Optional[dt.time] = None
    days_of_week: Optional[list[int]] = None
    active: Optional[bool] = None

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        if v is not None and v not in ROUTINE_KINDS:
            raise ValueError(f"kind must be one of {ROUTINE_KINDS}")
        return v


class MedicationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    dose: str = Field("", max_length=120)
    times: list[dt.time] = Field(default_factory=list, max_length=12)
    notes: str = Field("", max_length=1000)
    active: bool = True


class MedicationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    dose: Optional[str] = Field(None, max_length=120)
    times: Optional[list[dt.time]] = Field(None, max_length=12)
    notes: Optional[str] = Field(None, max_length=1000)
    active: Optional[bool] = None


class MedicationEventRecord(BaseModel):
    medication_id: uuid.UUID
    due_at: dt.datetime
    status: str
    confirmed_via: str = Field("carer", pattern=r"^(checkin|family|carer|device|none)$")

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v not in MED_EVENT_STATUSES:
            raise ValueError(f"status must be one of {MED_EVENT_STATUSES}")
        return v


class ConsentRecord(BaseModel):
    kind: str
    granted: bool
    granted_by: str = Field(..., pattern=r"^(person|legal_representative)$")
    granted_by_name: str = Field("", max_length=80)
    retention_months: Optional[int] = Field(None, ge=1, le=120)
    note: str = Field("", max_length=1000)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        if v not in CONSENT_KINDS:
            raise ValueError(f"kind must be one of {CONSENT_KINDS}")
        return v


class CheckinAnswer(BaseModel):
    """The person's answer. `text` is empty for a bare "I'm OK" button press."""
    text: str = Field("", max_length=4000)
    via: str = Field("voice", pattern=r"^(voice|button|phone)$")


class AskNow(BaseModel):
    kind: str = "wellbeing"
    text: str = Field("", max_length=400)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        allowed = ("meal", "medication", "mood", "pain", "plans", "wellbeing", "custom")
        if v not in allowed:
            raise ValueError(f"kind must be one of {allowed}")
        return v


class GenerateReport(BaseModel):
    kind: str = "daily_carer"
    date: dt.date

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        if v not in ("daily_carer", "weekly_family"):
            raise ValueError("kind must be daily_carer or weekly_family")
        return v
