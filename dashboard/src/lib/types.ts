export interface Workspace { id: string; name: string; slug: string; role: string; is_default: boolean; status: string }
export interface WorkspaceList { workspaces: Workspace[]; count: number; tenant_id: string; single: boolean; edition: string }
export interface Person {
  id: string; display_name: string; preferred_name: string; timezone: string; status: string; notes: string;
  home_address: string; home_latitude: number | null; home_longitude: number | null; home_radius_m: number;
  emergency_summary: string; has_access_notes: boolean;
}
export interface HomeUpdate {
  home_address?: string; home_latitude?: number | null; home_longitude?: number | null; home_radius_m?: number;
  emergency_summary?: string; access_notes?: string;
}
export interface AccessNotes { allowed: boolean; access_notes: string; emergency_summary: string; home_address: string }
export interface Reading { kind: string; value: number; unit: string; recorded_at: string }
export interface Device {
  id: string; platform: string; status: string; battery_percent: number | null; on_wrist: boolean | null;
  background_sensor_permission: boolean | null; last_sync_at: string | null; pairing_code?: string;
}
/** What happened, recorded when an alert is resolved. An alert stays open until it is. */
export type AlertOutcome = 'all_well' | 'needed_help' | 'taken_to_hospital' | 'false_alarm' | 'other';
export const OUTCOME_WORDS: Record<AlertOutcome, string> = {
  all_well: 'All well', needed_help: 'Needed help', taken_to_hospital: 'Taken to hospital', false_alarm: 'False alarm', other: 'Other',
};

export interface Alert {
  id: string; person_id: string; kind: string; severity: 'notice' | 'urgent' | 'sos';
  status: 'asking' | 'open' | 'escalating' | 'acknowledged' | 'resolved' | 'cancelled'; opened_at: string;
  current_tier: number; acknowledged_at: string | null; resolved_at: string | null; details: Record<string, unknown>;
  attempts?: Attempt[]; acknowledgements?: Acknowledgement[];
  location_latitude?: number | null; location_longitude?: number | null; location_accuracy_m?: number | null;
  location_at?: string | null; at_home?: boolean | null;
  follow_up_due_at?: string | null; follow_up_asked_at?: string | null; re_escalations?: number; outcome?: AlertOutcome | '';
}
export interface Attempt { id: string; tier: number; channel: string; outcome: string; detail: string; attempted_at: string; delivery?: string; delivered_at?: string | null }
export interface Acknowledgement { id: string; kind: string; note: string; acknowledged_at: string }
export interface Memory { day: string; summary: string; exchanges: number; updated_at: string; edited_at?: string | null; edited_by?: string }
export interface CommunicationProfile {
  person_id?: string; pace: 'slow' | 'normal'; prompting_level: 'light' | 'standard' | 'full'; voice_on: boolean; touch_on: boolean; vibration_on: boolean;
  topics_avoid: string[]; familiar_topics: string[]; redirect_member_id: string | null; redirect_name?: string | null; topics_consented?: boolean;
  updated_by_role?: string; updated_at?: string | null;
}
export type ReminderStatus = 'pending' | 'asked' | 'confirming' | 'confirmed' | 'snoozed' | 'skipped' | 'missed' | 'cancelled';
export interface Reminder {
  id: string; person_id: string; kind: 'reminder' | 'medication' | 'hydration'; text: string; medication_id: string | null; due_at: string;
  status: ReminderStatus; snoozed_until: string | null; source: string; asked_count: number; done_at: string | null; created_at: string;
}
export interface Checkin {
  id: string; scheduled_at: string; asked_at: string | null; status: string; question_kind: string; question_source?: 'template' | 'model';
  question_text: string; answered_at: string | null; answer_summary: string; answer_text: string; answered_via: string;
}
export type VisitStatus = 'planned' | 'late' | 'arrived' | 'departed' | 'missed' | 'cancelled';
export type VisitOutcome = '' | 'all_well' | 'concern_noted' | 'needed_help' | 'not_at_home' | 'declined' | 'other';
export const VISIT_OUTCOME_WORDS: Record<Exclude<VisitOutcome, ''>, string> = {
  all_well: 'All well', concern_noted: 'Concern noted', needed_help: 'Needed help', not_at_home: 'Not at home', declined: 'Declined the visit', other: 'Other',
};
export interface VisitTask { id: string; visit_id: string; title: string; position: number; done: boolean; done_at: string | null; note: string }
export interface Visit {
  id: string; person_id: string; carer_member_id: string | null; carer_name: string; planned_start: string; planned_end: string;
  purpose: string; status: VisitStatus; arrived_at: string | null; departed_at: string | null; outcome: VisitOutcome;
  notes: string; handover: string; created_at: string; updated_at: string; tasks: VisitTask[];
}
export interface VisitCreate { planned_start: string; planned_end: string; carer_member_id?: string | null; carer_name?: string; purpose?: string; tasks?: string[] }
export interface VisitDepart { outcome: Exclude<VisitOutcome, ''>; notes: string; handover: string; tasks_done: string[] }
export interface OverviewAlert {
  id: string; person_id: string; person_name: string; kind: string; severity: 'notice' | 'urgent' | 'sos'; status: string; opened_at: string;
  current_tier: number; acknowledged_at: string | null; follow_up_due_at: string | null; follow_up_asked_at: string | null; re_escalations: number;
  responder: string | null; awaiting_response: boolean; follow_up_overdue: boolean; arrived: boolean;
}
export interface OverviewVisit { id: string; person_id: string; person_name: string; carer_name: string; planned_start: string; planned_end: string; status: string; arrived_at: string | null; state: 'overdue' | 'in_progress' | 'due_soon' | 'planned' }
export interface Overview {
  now: string; people: number;
  attention: { person_id: string; person_name: string; reasons: string[] }[];
  visits: OverviewVisit[]; alerts: OverviewAlert[];
  stale_watches: { device_id: string; person_id: string; person_name: string; reasons: string[]; last_sync_at: string | null; battery_percent: number | null }[];
}
export interface Routine { id: string; name: string; kind: string; time_of_day: string | null; days_of_week: number[]; active: boolean }
export interface Medication { id: string; name: string; dose: string; times: string[]; notes: string; active: boolean }
export interface MedicationEvent { id: string; medication_id: string; due_at: string; status: string; confirmed_via: string }
export interface CircleMember {
  id: string; display_name: string; relationship: string; role: string; tier: number; priority: number;
  phone: string; email: string; active: boolean; user_id: string | null;
}
export interface Consent { id: string; kind: string; granted: boolean; granted_by: string; granted_by_name: string; recorded_at: string; revoked_at: string | null }
export interface ConsentList { consents: Consent[]; effective: Record<string, boolean>; retention_months: number }
export interface Transcript { id: string; checkin_id: string | null; speaker: 'person' | 'companion'; text: string; created_at: string }
export interface SafetySettings {
  quiet_start: string; quiet_end: string; checkin_min_minutes: number; checkin_max_minutes: number;
  repeat_after_s: number; sensor_check_after_s: number; heart_rate_min: number | null; heart_rate_max: number | null;
  no_movement_window_s: number; movement_floor_steps: number; off_wrist_alert_minutes: number;
  battery_alert_percent: number; sync_alert_minutes: number; missed_checkins_before_escalation: number; ack_wait_minutes: number;
  ask_first_wait_s: number; heart_rate_confirm_readings: number; heart_rate_confirm_seconds: number;
  night_off_wrist_minutes: number; usual_wake_time: string; wake_grace_minutes: number; visit_late_minutes?: number; hydration_every_minutes?: number; reading_upload_interval_s?: number; follow_up_minutes?: number; follow_up_grace_minutes?: number;
  fall_detection_enabled: boolean; fall_countdown_s: number; fall_impact_g: number; fall_still_seconds: number; fall_still_threshold_g: number;
}
export interface OpsStatus { monitoring_paused: boolean; stale_after_s: number; checked_at: string; jobs: Record<string, { last_run_at: string | null; age_s: number | null; stale: boolean }>;
  /** Alert messages still queued after five minutes, or reported failed in the last hour. */
  delivery?: { queued_overdue: number; failed_recent: number; delivery_problem: boolean } }
export interface PersonStatus { person: Person; devices: Device[]; latest_readings: Reading[]; open_alerts: Alert[]; last_checkin: Checkin | null }
export interface AccessEntry { id: string; actor_user_id: string | null; actor_label: string; resource_kind: string; action: string; at: string; detail: Record<string, unknown> }
export interface Report { id: string; kind: 'daily_carer' | 'weekly_family'; period_start: string; period_end: string; generated_at: string; title: string; body_text: string; delivered_channels: string[] }
export interface SearchResult { kind: 'checkin' | 'alert' | 'transcript'; id: string; at: string; text: string }
export interface SearchResponse { query: string; results: SearchResult[]; transcripts_included: boolean }

export interface PlanSummary {
  subscription: { plan_id: string; status: string; current_period_end: string | null; display_name: string } | null;
  entitled: boolean; limits: Record<string, number | null>; usage: { care_people: number; care_circle_members: number };
  plans: { plan_id: string; display_name: string; description: string; limits: Record<string, number | null> }[];
  payment_provider: string; note: string;
}
export interface InvitationOut { invitation_id: string; member_id: string; email: string; role: string; expires_at: string; link: string; consents_recorded: string[]; mail: { delivered: boolean; transport: string } }
export interface InvitationRow { member_id: string; display_name: string; role: string; invited_email: string; accepted: boolean; expires_at: string | null; accepted_at: string | null; revoked_at: string | null }

/** What IRA CARE measures and what it does not (GET /api/v1/care/capabilities). */
export interface Capability {
  key: string; label: string; status: 'available' | 'not_available';
  reading?: string; how?: string; reason?: string;
}

/** A care organisation's staff role and what it may do (GET /care/me/permissions, care/authz.py). */
export type StaffAction = 'view_care_plan' | 'edit_care_plan' | 'acknowledge_alerts' | 'record_visits' | 'record_medication'
  | 'set_reminders' | 'manage_people' | 'manage_circle' | 'manage_watches' | 'manage_safety_settings'
  | 'manage_medication_list' | 'manage_consents' | 'search' | 'view_caseload' | 'view_visit_plan'
  | 'designate_coordinators' | 'run_heart_research';
export interface Permissions {
  role: string | null; coordinator?: boolean; label: string; who: string; sees_everyone: boolean;
  can_read: boolean; can_act: boolean; can_manage: boolean; actions: Partial<Record<StaffAction, boolean>>;
}

/** One version of a person's care plan (migration 116). Versions are never rewritten. */
export type PlanWho = 'carer' | 'family' | 'person';
export interface PlanRoutineItem { what: string; when: string; who: PlanWho }
export interface CarePlan {
  id: string; person_id: string; version: number; goals: string; support: string; routine: PlanRoutineItem[];
  risks: string; emergency: string; review_on: string | null; reason: string; author_user_id: string | null;
  author_name: string; created_at: string;
}
export interface CarePlanDraft {
  based_on: number; goals: string; support: string; routine: PlanRoutineItem[]; risks: string; emergency: string;
  review_on: string | null; reason: string;
}

/** Workspace-wide search (D3.g): people by name, visits, alerts. Never what anyone said. */
export type SearchKind = 'person' | 'visit' | 'alert';
export interface SearchHit { kind: SearchKind; id: string; person_id: string; person_name: string; text: string; at: string | null }

/** The care team's views (D-081). */
export interface CaseloadPerson { person_id: string; name: string; member_id: string; next_visit: string | null }
export interface CaseloadCarer { user_id: string; name: string; people: CaseloadPerson[] }
export interface Caseload { everyone: boolean; carers: CaseloadCarer[] }
export interface PlannedVisit {
  id: string; person_id: string; person_name: string; planned_start: string; planned_end: string; status: VisitStatus;
  purpose: string; carer_name: string; carer_member_id: string | null;
}
export interface StaffMember { user_id: string; name: string; roles: string[]; people: number; coordinator: boolean }
