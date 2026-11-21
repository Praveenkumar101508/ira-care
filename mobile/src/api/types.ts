/** Shapes the backend returns. Fields the app does not use are left out. */

export interface LoginResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface Workspace {
  id: string;
  name: string;
  slug: string;
  role: string;
  is_default: boolean;
  status: string;
}

export interface WorkspaceList {
  workspaces: Workspace[];
  count: number;
  tenant_id: string;
  single: boolean;
  edition: string;
}

export interface Person {
  id: string;
  display_name: string;
  preferred_name: string;
  timezone: string;
  status: 'active' | 'paused' | 'archived';
  home_address?: string;
  home_latitude?: number | null;
  home_longitude?: number | null;
  emergency_summary?: string;
  has_access_notes?: boolean;
}

export interface Reading {
  kind: string;
  value: number;
  unit: string;
  recorded_at: string;
}

export interface Device {
  id: string;
  platform: string;
  status: string;
  battery_percent: number | null;
  on_wrist: boolean | null;
  background_sensor_permission: boolean | null;
  last_sync_at: string | null;
  last_seen_at: string | null;
  pairing_code?: string;
}

export interface Alert {
  id: string;
  person_id: string;
  kind: string;
  severity: 'notice' | 'urgent' | 'sos';
  status: 'open' | 'escalating' | 'acknowledged' | 'resolved' | 'cancelled';
  opened_at: string;
  current_tier: number;
  acknowledged_at: string | null;
  resolved_at: string | null;
  details: Record<string, unknown>;
  location_latitude?: number | null;
  location_longitude?: number | null;
  location_accuracy_m?: number | null;
  location_at?: string | null;
  at_home?: boolean | null;
}

/** What tier 1 and 2 recipients may open while an alert is open. */
export interface AccessNotes { allowed: boolean; access_notes: string; emergency_summary: string; home_address: string }

export interface Checkin {
  id: string;
  scheduled_at: string;
  status: string;
  question_kind: string;
  answered_at: string | null;
  answer_summary: string;
  answer_text: string;
}

export interface PersonStatus {
  person: Person;
  devices: Device[];
  latest_readings: Reading[];
  open_alerts: Alert[];
  last_checkin: Checkin | null;
}

export interface CircleMember {
  id: string;
  display_name: string;
  relationship: string;
  role: 'family' | 'carer' | 'clinician' | 'care_organisation';
  tier: number;
  priority: number;
  phone: string;
  email: string;
  active: boolean;
}

export interface Consent {
  id: string;
  kind: string;
  granted: boolean;
  granted_by: string;
  granted_by_name: string;
  recorded_at: string;
  revoked_at: string | null;
}

export interface ConsentList {
  consents: Consent[];
  effective: Record<string, boolean>;
  retention_months: number;
}

export interface Report {
  id: string;
  kind: 'daily_carer' | 'weekly_family';
  period_start: string;
  period_end: string;
  generated_at: string;
  title: string;
  body_text: string;
}
