import type { AlertOutcome,
  AccessEntry, Alert, Checkin, CircleMember, ConsentList, Device, Medication, MedicationEvent, Person,
  AccessNotes, HomeUpdate, InvitationOut, InvitationRow, Memory, PlanSummary, OpsStatus, PersonStatus, Reading, Reminder, Report, Routine, SafetySettings, SearchResponse, Transcript, Visit, VisitCreate, VisitDepart, WorkspaceList, CommunicationProfile, Overview,
} from './types';

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) { super(message); }
}

export interface Session { baseUrl: string; accessToken: string; workspaceId: string | null; role: string }

type Fetch = typeof fetch;

/** The dashboard's view of the backend. Browser-side; the token stays in session storage. */
export class Api {
  // `fetch` is wrapped rather than passed bare: calling the browser's fetch with another `this` throws.
  constructor(private readonly s: Session, private readonly f: Fetch = (input, init) => fetch(input, init)) {}

  /** Sign in. A cloud deployment signs in by email (/auth/cloud/login); a private
   *  one has no such route (404) and uses the owner account at /auth/token. */
  static async login(baseUrl: string, username: string, password: string, f: Fetch = (input, init) => fetch(input, init)) {
    const form = { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' } };
    let r = await f(`${baseUrl}/auth/cloud/login`, { ...form, body: new URLSearchParams({ email: username, password }).toString() });
    if (r.status === 404) {
      r = await f(`${baseUrl}/auth/token`, { ...form, body: new URLSearchParams({ username, password, grant_type: 'password' }).toString() });
    }
    if (!r.ok) throw new ApiError(r.status, r.status === 401 ? 'Wrong username or password.' : await detail(r));
    return (await r.json()) as { access_token: string; refresh_token: string };
  }

  workspaces() { return this.req<WorkspaceList>('GET', '/api/v1/workspaces', undefined, false); }
  people(includeArchived = false) { return this.req<{ people: Person[] }>('GET', `/api/v1/care/people?include_archived=${includeArchived}`).then((o) => o.people); }
  createPerson(body: { display_name: string; preferred_name: string; timezone: string }) { return this.req<Person>('POST', '/api/v1/care/people', body); }
  status(id: string) { return this.req<PersonStatus>('GET', `/api/v1/care/people/${id}/status`); }
  alerts(openOnly: boolean, personId?: string) {
    const q = `open_only=${openOnly}&limit=500${personId ? `&person_id=${personId}` : ''}`;
    return this.req<{ alerts: Alert[] }>('GET', `/api/v1/care/alerts?${q}`).then((o) => o.alerts);
  }
  alert(id: string) { return this.req<Alert>('GET', `/api/v1/care/alerts/${id}`); }
  acknowledge(id: string, note: string, kind: 'on_my_way' | 'arrived' = 'on_my_way') { return this.req<Alert>('POST', `/api/v1/care/alerts/${id}/acknowledge`, { kind, note }); }
  updateHome(personId: string, body: HomeUpdate) { return this.req<Person>('PATCH', `/api/v1/care/people/${personId}`, body); }
  accessNotes(alertId: string) { return this.req<AccessNotes>('GET', `/api/v1/care/alerts/${alertId}/access-notes`); }
  resolve(id: string, resolution: string, outcome: AlertOutcome = 'other') { return this.req<Alert>('POST', `/api/v1/care/alerts/${id}/resolve`, { resolution, outcome }); }
  vitals(id: string, kind?: string, limit = 500) { return this.req<{ vitals: Reading[] }>('GET', `/api/v1/care/people/${id}/vitals?limit=${limit}${kind ? `&kind=${kind}` : ''}`).then((o) => o.vitals); }
  checkins(id: string) { return this.req<{ checkins: Checkin[]; answers_included: boolean }>('GET', `/api/v1/care/people/${id}/checkins?limit=200`); }
  transcripts(id: string) { return this.req<{ transcripts: Transcript[] }>('GET', `/api/v1/care/people/${id}/transcripts?limit=500`).then((o) => o.transcripts); }
  visits(id: string) { return this.req<{ visits: Visit[] }>('GET', `/api/v1/care/people/${id}/visits?limit=200`).then((o) => o.visits); }
  planVisit(id: string, body: VisitCreate) { return this.req<Visit>('POST', `/api/v1/care/people/${id}/visits`, body); }
  arrive(visitId: string) { return this.req<Visit>('POST', `/api/v1/care/visits/${visitId}/arrive`, {}); }
  depart(visitId: string, body: VisitDepart) { return this.req<Visit>('POST', `/api/v1/care/visits/${visitId}/depart`, body); }
  cancelVisit(visitId: string, note = '') { return this.req<Visit>('POST', `/api/v1/care/visits/${visitId}/cancel`, { note }); }
  routines(id: string) { return this.req<{ routines: Routine[] }>('GET', `/api/v1/care/people/${id}/routines`).then((o) => o.routines); }
  addRoutine(id: string, body: { name: string; kind: string; time_of_day: string | null }) { return this.req<Routine>('POST', `/api/v1/care/people/${id}/routines`, body); }
  medications(id: string) { return this.req<{ medications: Medication[] }>('GET', `/api/v1/care/people/${id}/medications`).then((o) => o.medications); }
  addMedication(id: string, body: { name: string; dose: string; times: string[] }) { return this.req<Medication>('POST', `/api/v1/care/people/${id}/medications`, body); }
  medicationEvents(id: string) { return this.req<{ events: MedicationEvent[] }>('GET', `/api/v1/care/people/${id}/medication-events?limit=200`).then((o) => o.events); }
  circle(id: string) { return this.req<{ members: CircleMember[] }>('GET', `/api/v1/care/people/${id}/circle`).then((o) => o.members); }
  addMember(id: string, body: Partial<CircleMember> & { display_name: string; role: string }) { return this.req<CircleMember>('POST', `/api/v1/care/people/${id}/circle`, body); }
  consents(id: string) { return this.req<ConsentList>('GET', `/api/v1/care/people/${id}/consents`); }
  settings(id: string) { return this.req<SafetySettings>('GET', `/api/v1/care/people/${id}/settings`); }
  putSettings(id: string, body: SafetySettings) { return this.req<SafetySettings>('PUT', `/api/v1/care/people/${id}/settings`, body); }
  newDevice(id: string) { return this.req<Device>('POST', `/api/v1/care/people/${id}/devices`, { platform: 'wearos' }); }
  reports(id: string, kind?: string) { return this.req<{ reports: Report[] }>('GET', `/api/v1/care/people/${id}/reports?limit=60${kind ? `&kind=${kind}` : ''}`).then((o) => o.reports); }
  generateReport(id: string, kind: string, date: string) { return this.req<Report>('POST', `/api/v1/care/people/${id}/reports/generate`, { kind, date }); }
  search(id: string, q: string) { return this.req<SearchResponse>('GET', `/api/v1/care/people/${id}/history/search?q=${encodeURIComponent(q)}&limit=100`); }
  overview() { return this.req<Overview>('GET', '/api/v1/care/overview'); }
  profileAccessNotes(id: string) { return this.req<{ allowed: boolean; access_notes: string; home_address: string; emergency_summary: string }>('GET', `/api/v1/care/people/${id}/access-notes`); }
  opsStatus() { return this.req<OpsStatus>('GET', '/api/v1/care/ops/status'); }
  plan() { return this.req<PlanSummary>('GET', '/api/v1/care/plan'); }
  invite(personId: string, body: { email: string; display_name: string; relationship: string; role: string; tier: number; phone: string; consents: { kind: string; granted: boolean; granted_by: string; granted_by_name: string }[] }) {
    return this.req<InvitationOut>('POST', `/api/v1/care/people/${personId}/invitations`, body);
  }
  invitations(personId: string) { return this.req<{ invitations: InvitationRow[] }>('GET', `/api/v1/care/people/${personId}/invitations`).then((o) => o.invitations); }
  profile(id: string) { return this.req<CommunicationProfile>('GET', `/api/v1/care/people/${id}/profile`); }
  putProfile(id: string, body: CommunicationProfile) {
    const { person_id: _p, redirect_name: _n, topics_consented: _c, updated_by_role: _r, updated_at: _u, ...rest } = body;
    return this.req<CommunicationProfile>('PUT', `/api/v1/care/people/${id}/profile`, rest);
  }
  reminders(id: string) { return this.req<{ reminders: Reminder[] }>('GET', `/api/v1/care/people/${id}/reminders?limit=100`).then((o) => o.reminders); }
  addReminder(id: string, text: string, dueAt: string) { return this.req<Reminder>('POST', `/api/v1/care/people/${id}/reminders`, { text, due_at: dueAt, kind: 'reminder' }); }
  actOnReminder(reminderId: string, action: 'confirm' | 'snooze' | 'skip' | 'cancel') { return this.req<Reminder>('POST', `/api/v1/care/reminders/${reminderId}/act`, { action, minutes: 10 }); }
  editMemory(id: string, day: string, summary: string) { return this.req<Memory>('PUT', `/api/v1/care/people/${id}/memory/${day}`, { summary }); }
  forgetMemory(id: string, day: string) { return this.req<void>('DELETE', `/api/v1/care/people/${id}/memory/${day}`); }
  memory(id: string) { return this.req<{ memory: Memory[] }>('GET', `/api/v1/care/people/${id}/memory`).then((o) => o.memory); }
  accessLog(id: string) { return this.req<{ entries: AccessEntry[] }>('GET', `/api/v1/care/people/${id}/access-log?limit=200`).then((o) => o.entries); }

  private async req<T>(method: string, path: string, body?: unknown, workspace = true): Promise<T> {
    const headers: Record<string, string> = { Authorization: `Bearer ${this.s.accessToken}`, Accept: 'application/json' };
    if (workspace) {
      if (!this.s.workspaceId) throw new ApiError(0, 'No care organisation selected.');
      headers['X-Workspace-ID'] = this.s.workspaceId;
    }
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let r: Response;
    try {
      r = await this.f(this.s.baseUrl + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch {
      throw new ApiError(0, 'No connection to the server.');
    }
    if (r.status === 401) throw new ApiError(401, 'Please sign in again.');
    if (!r.ok) throw new ApiError(r.status, await detail(r));
    if (r.status === 204) return undefined as T;
    return (await r.json()) as T;
  }
}

async function detail(r: Response): Promise<string> {
  try {
    const d = (await r.json()) as { detail?: unknown };
    return typeof d.detail === 'string' ? d.detail : `The server answered ${r.status}.`;
  } catch { return `The server answered ${r.status}.`; }
}

export type { Reading } from './types';
