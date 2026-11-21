export interface Workspace { id: string; name: string; slug: string; role: string; is_default: boolean; status: string }
export interface WorkspaceList { workspaces: Workspace[]; count: number; tenant_id: string; single: boolean; edition: string }
export interface Person { id: string; display_name: string; preferred_name: string; timezone: string; status: string; notes: string }
export interface Reading { kind: string; value: number; unit: string; recorded_at: string }
export interface Device {
  id: string; platform: string; status: string; battery_percent: number | null; on_wrist: boolean | null;
  background_sensor_permission: boolean | null; last_sync_at: string | null; pairing_code?: string;
}
export interface Alert {
  id: string; person_id: string; kind: string; severity: 'notice' | 'urgent' | 'sos';
  status: 'open' | 'escalating' | 'acknowledged' | 'resolved' | 'cancelled'; opened_at: string;
  current_tier: number; acknowledged_at: string | null; resolved_at: string | null; details: Record<string, unknown>;
  attempts?: Attempt[]; acknowledgements?: Acknowledgement[];
}
export interface Attempt { id: string; tier: number; channel: string; outcome: string; detail: string; attempted_at: string }
export interface Acknowledgement { id: string; kind: string; note: string; acknowledged_at: string }
export interface Checkin {
  id: string; scheduled_at: string; asked_at: string | null; status: string; question_kind: string;
  question_text: string; answered_at: string | null; answer_summary: string; answer_text: string; answered_via: string;
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
}
export interface PersonStatus { person: Person; devices: Device[]; latest_readings: Reading[]; open_alerts: Alert[]; last_checkin: Checkin | null }
export interface AccessEntry { id: string; actor_user_id: string | null; actor_label: string; resource_kind: string; action: string; at: string; detail: Record<string, unknown> }
