import type { AlertOutcome,
  AccessEntry, Alert, Checkin, CircleMember, ConsentList, Device, Medication, MedicationEvent, Person,
  AccessNotes, HomeUpdate, InvitationOut, InvitationRow, Memory, PlanSummary, OpsStatus, PersonStatus, Reading, Reminder, Report, Routine, SafetySettings, SearchResponse, Transcript, Visit, VisitCreate, VisitDepart, WorkspaceList, CommunicationProfile, Overview, Capability, ActivitySummary, Permissions, CarePlan, CarePlanDraft, SearchHit, Caseload, PlannedVisit, StaffMember,
} from './types';
import type { ResearchResult, ResearchSample } from './research';
import type { AdminMe, Instruction, StepUpResult } from './deviceAdmin';

/** `body` is the server's answer as sent, when it was readable, so a screen can read a code
 *  the message alone does not carry (a refusal that asks the person to confirm who they are). */
export class ApiError extends Error {
  constructor(public readonly status: number, message: string, public readonly body?: unknown) { super(message); }
}

/** `refreshToken`, when present, lets an expired access token be renewed (POST /auth/refresh)
 *  without sending the carer back to the sign-in page in the middle of a shift. */
export interface Session { baseUrl: string; accessToken: string; refreshToken?: string; workspaceId: string | null; role: string }

/** What renewing the sign-in came to. 'offline': the server could not be reached or could not
 *  answer, so nothing is signed out for that. 'expired': the server refused the refresh token. */
export type RenewOutcome = 'renewed' | 'expired' | 'offline';

type Fetch = typeof fetch;

/** One renewal per refresh token at a time, shared by every Api object on the page: the people
 *  list, the alerts and the live view all meeting an expired token at once renew it once. */
const renewals = new Map<string, Promise<{ outcome: RenewOutcome; accessToken?: string }>>();

/** The dashboard's view of the backend. Browser-side; the token stays in session storage. */
export class Api {
  private readonly s: Session;
  /** Told the new access token after a renewal, so the stored session can keep it. */
  onRenewed?: (accessToken: string) => void;

  // `fetch` is wrapped rather than passed bare: calling the browser's fetch with another `this` throws.
  constructor(session: Session, private readonly f: Fetch = (input, init) => fetch(input, init)) {
    this.s = { ...session };
  }

  /** Renew the access token with the refresh token. Never loops; shared by concurrent callers. */
  async renew(): Promise<RenewOutcome> {
    const refresh = this.s.refreshToken;
    if (!refresh) return 'expired';
    let pending = renewals.get(refresh);
    if (!pending) {
      pending = (async () => {
        let r: Response;
        try {
          r = await this.f(`${this.s.baseUrl}/auth/refresh`, { method: 'POST', headers: { Authorization: `Bearer ${refresh}`, Accept: 'application/json' } });
        } catch {
          return { outcome: 'offline' as const };
        }
        if (!r.ok) return { outcome: r.status >= 500 || r.status === 429 ? 'offline' as const : 'expired' as const };
        try {
          const data = (await r.json()) as { access_token?: string };
          return data.access_token ? { outcome: 'renewed' as const, accessToken: data.access_token } : { outcome: 'expired' as const };
        } catch {
          return { outcome: 'offline' as const };
        }
      })().finally(() => { renewals.delete(refresh); });
      renewals.set(refresh, pending);
    }
    const out = await pending;
    if (out.outcome === 'renewed' && out.accessToken) {
      this.s.accessToken = out.accessToken;
      this.onRenewed?.(out.accessToken);
    }
    return out.outcome;
  }

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
  activity(id: string, days = 7, weeks = 4) { return this.req<ActivitySummary>('GET', `/api/v1/care/people/${id}/activity?days=${days}&weeks=${weeks}`); }
  capabilities() { return this.req<{ capabilities: Capability[] }>('GET', '/api/v1/care/capabilities').then((o) => o.capabilities); }
  overview() { return this.req<Overview>('GET', '/api/v1/care/overview'); }
  profileAccessNotes(id: string) { return this.req<{ allowed: boolean; access_notes: string; home_address: string; emergency_summary: string }>('GET', `/api/v1/care/people/${id}/access-notes`); }
  opsStatus() { return this.req<OpsStatus>('GET', '/api/v1/care/ops/status'); }
  permissions() { return this.req<Permissions>('GET', '/api/v1/care/me/permissions'); }
  /** Research mode (D-086): recorded or simulated signals only; nothing is stored. */
  researchSamples() { return this.req<{ notice: string; samples: ResearchSample[] }>('GET', '/api/v1/care/research/heart/samples'); }
  runResearch(body: { sample: string } | { recording: unknown }) { return this.req<ResearchResult>('POST', '/api/v1/care/research/heart/run', body); }
  searchAll(q: string) { return this.req<{ query: string; results: SearchHit[] }>('GET', `/api/v1/care/search?q=${encodeURIComponent(q)}`).then((o) => o.results); }
  caseload() { return this.req<Caseload>('GET', '/api/v1/care/caseload'); }
  visitPlan() { return this.req<{ visits: PlannedVisit[] }>('GET', '/api/v1/care/visits').then((o) => o.visits); }
  staff() { return this.req<{ staff: StaffMember[] }>('GET', '/api/v1/care/staff').then((o) => o.staff); }
  setCoordinator(userId: string, coordinator: boolean) { return this.req<{ user_id: string; coordinator: boolean }>('PUT', `/api/v1/care/staff/${userId}/coordinator`, { coordinator }); }
  carePlan(id: string) { return this.req<{ plan: CarePlan | null }>('GET', `/api/v1/care/people/${id}/plan`).then((o) => o.plan); }
  carePlanHistory(id: string) { return this.req<{ versions: CarePlan[] }>('GET', `/api/v1/care/people/${id}/plan/history`).then((o) => o.versions); }
  saveCarePlan(id: string, body: CarePlanDraft) { return this.req<CarePlan>('POST', `/api/v1/care/people/${id}/plan`, body); }
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
  personDevices(id: string) { return this.req<{ devices: Device[]; count: number }>('GET', `/api/v1/care/people/${id}/devices`).then((o) => o.devices); }

  /** Deployment administration (D-085, D-089): a grant the operator writes, never a care role. */
  deploymentAdminMe() { return this.req<AdminMe>('GET', '/api/v1/care/deployment-admin/me'); }
  deviceInstructions(tenantId: string, workspaceId: string, deviceId: string) {
    const q = `tenant_id=${encodeURIComponent(tenantId)}&workspace_id=${encodeURIComponent(workspaceId)}&device_id=${encodeURIComponent(deviceId)}`;
    return this.req<{ instructions: Instruction[] }>('GET', `/api/v1/care/deployment-admin/device-instructions?${q}`).then((o) => o.instructions);
  }
  issueDeviceInstruction(body: { tenant_id: string; workspace_id: string; device_id: string; action: string; minutes: number; reason: string }) {
    return this.req<Instruction>('POST', '/api/v1/care/deployment-admin/device-instructions', body);
  }
  approveDeviceInstruction(id: string, target: { tenant_id: string; workspace_id: string }) {
    return this.req<Instruction>('POST', `/api/v1/care/deployment-admin/device-instructions/${encodeURIComponent(id)}/approve`, target);
  }
  revokeDeviceInstruction(id: string, target: { tenant_id: string; workspace_id: string }, reason: string) {
    return this.req<Instruction>('POST', `/api/v1/care/deployment-admin/device-instructions/${encodeURIComponent(id)}/revoke`, { ...target, reason });
  }

  /** Confirm who you are again (step-up). The access token the server answers with replaces
   *  the stored one through the same path a renewal takes, so the next page keeps it. The
   *  password is sent once and kept nowhere. */
  async stepUp(password: string, totpCode?: string): Promise<StepUpResult> {
    const body: { password: string; totp_code?: string } = { password };
    if (totpCode) body.totp_code = totpCode;
    let r: Response;
    try {
      r = await this.f(`${this.s.baseUrl}/auth/step-up`, { method: 'POST', headers: { Authorization: `Bearer ${this.s.accessToken}`, Accept: 'application/json', 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    } catch {
      throw new ApiError(0, 'No connection to the server.');
    }
    if (r.status === 401) throw new ApiError(401, 'Wrong password or code.', await bodyOf(r));
    if (r.status === 503) throw new ApiError(503, 'Confirming who you are is not possible right now: the sign-in service is unavailable. Try again shortly.', await bodyOf(r));
    if (!r.ok) { const b = await bodyOf(r); throw new ApiError(r.status, detailOf(b, r.status), b); }
    const out = (await r.json()) as StepUpResult;
    if (!out.access_token) throw new ApiError(0, 'The server did not answer with a new sign-in.');
    this.s.accessToken = out.access_token;
    this.onRenewed?.(out.access_token);
    return out;
  }

  private async req<T>(method: string, path: string, body?: unknown, workspace = true): Promise<T> {
    const headers: Record<string, string> = { Authorization: `Bearer ${this.s.accessToken}`, Accept: 'application/json' };
    if (workspace) {
      if (!this.s.workspaceId) throw new ApiError(0, 'No care organisation selected.');
      headers['X-Workspace-ID'] = this.s.workspaceId;
    }
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const once = async () => {
      headers.Authorization = `Bearer ${this.s.accessToken}`;
      try {
        return await this.f(this.s.baseUrl + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
      } catch {
        throw new ApiError(0, 'No connection to the server.');
      }
    };
    let r = await once();
    // A request refused with 401 was refused before anything happened, so sending it again
    // after renewing cannot do anything twice. A second 401 is final.
    if (r.status === 401 && this.s.refreshToken) {
      const renewed = await this.renew();
      if (renewed === 'offline') throw new ApiError(0, 'No connection to the server. Your sign-in will be renewed when it is back.');
      if (renewed === 'renewed') r = await once();
    }
    if (r.status === 401) throw new ApiError(401, 'Please sign in again.');
    if (!r.ok) { const b = await bodyOf(r); throw new ApiError(r.status, detailOf(b, r.status), b); }
    if (r.status === 204) return undefined as T;
    return (await r.json()) as T;
  }
}

async function detail(r: Response): Promise<string> {
  return detailOf(await bodyOf(r), r.status);
}

async function bodyOf(r: Response): Promise<unknown> {
  try { return await r.json(); } catch { return undefined; }
}

/** The server's message: `detail` as a string, or `detail.message` when it is an object. */
function detailOf(body: unknown, status: number): string {
  const d = body && typeof body === 'object' ? (body as { detail?: unknown }).detail : undefined;
  if (typeof d === 'string') return d;
  if (d && typeof d === 'object' && typeof (d as { message?: unknown }).message === 'string') return (d as { message: string }).message;
  return `The server answered ${status}.`;
}

export type { Reading } from './types';
