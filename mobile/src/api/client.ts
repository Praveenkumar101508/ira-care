import type { AlertOutcome, AccessNotes, CarePlan, Medication, TalkLine, TalkReply,
  Alert, Checkin, CircleMember, CommunicationProfile, ConsentList, Device, LoginResponse, Memory, Person, PersonStatus, Reminder, Report, Visit, VisitOutcome, WorkspaceList,
} from './types';
import type { Appearance } from '../appearance/theme';
import type { Membership } from '../model/experience';

/** What went wrong, in a way a screen can show. */
export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
  }
}

export interface Session {
  baseUrl: string;
  accessToken: string;
  workspaceId: string | null;
  /** When present, an expired access token is renewed once (POST /auth/refresh) and the call retried. */
  refreshToken?: string;
}

/** What renewing the sign-in came to. */
export type RenewOutcome = 'renewed' | 'expired' | 'offline';

import { forEachLine } from './stream';

type Fetch = typeof fetch;

/**
 * The family app's view of the backend: login, workspace selection and the
 * care API. Every care call carries the bearer token and the selected
 * workspace header; the backend verifies both, never trusting the header
 * alone.
 */
export class CareClient {
  private readonly session: Session;
  /** One renewal at a time: every call that met an expired token waits for the same one. */
  private renewing: Promise<RenewOutcome> | null = null;
  /** Told the new access token, so it can be kept for next time. */
  onRenewed?: (accessToken: string) => void;

  // `fetch` is wrapped rather than passed bare: on the web, calling it with another `this` throws.
  /** `streamImpl`: a fetch whose response body can be read as it arrives (`expo/fetch`), used for
   *  Talk so IRA's sentences show as they come. Without one, Talk reads the whole reply first. */
  constructor(session: Session, private readonly fetchImpl: Fetch = (input, init) => fetch(input, init),
              private readonly streamImpl: Fetch | null = null) {
    this.session = { ...session };
  }

  /** Renew the access token with the refresh token. Shared by concurrent callers; never loops.
   *  'expired' when the server refuses it (revoked, ended, or too old): the person signs in again.
   *  'offline' when the server could not be reached: nothing is signed out for that. */
  renew(): Promise<RenewOutcome> {
    if (!this.session.refreshToken) return Promise.resolve('expired');
    if (!this.renewing) {
      this.renewing = (async (): Promise<RenewOutcome> => {
        let response: Response;
        try {
          response = await this.fetchImpl(`${this.session.baseUrl}/auth/refresh`, {
            method: 'POST', headers: { Authorization: `Bearer ${this.session.refreshToken}`, Accept: 'application/json' },
          });
        } catch {
          return 'offline';
        }
        if (!response.ok) return response.status >= 500 ? 'offline' : 'expired';
        try {
          const data = (await response.json()) as { access_token?: string };
          if (!data.access_token) return 'expired';
          this.session.accessToken = data.access_token;
          this.onRenewed?.(data.access_token);
          return 'renewed';
        } catch {
          return 'offline';
        }
      })().finally(() => { this.renewing = null; });
    }
    return this.renewing;
  }

  /** Send once; on 401 renew once and send again; a second 401 is final. A request refused with
   *  401 was refused before anything happened, so sending it again cannot do anything twice. */
  private async send(method: string, path: string, headersFor: () => Record<string, string>, body?: unknown,
                     via: Fetch = this.fetchImpl): Promise<Response> {
    const once = async () => {
      try {
        return await via(this.session.baseUrl + path, { method, headers: headersFor(), body: body === undefined ? undefined : JSON.stringify(body) });
      } catch {
        throw new ApiError(0, 'No connection. Check the network and try again.');
      }
    };
    let response = await once();
    if (response.status === 401 && this.session.refreshToken) {
      const renewed = await this.renew();
      if (renewed === 'offline') throw new ApiError(0, 'No connection. Your sign-in will be renewed when the network is back.');
      if (renewed === 'renewed') response = await once();
    }
    if (response.status === 401) throw new ApiError(401, 'Your sign-in has expired. Please sign in again.');
    return response;
  }

  /** Sign in. A cloud deployment signs in by email (/auth/cloud/login); a private
   *  one has no such route (404) and uses the owner account at /auth/token. */
  static async login(baseUrl: string, username: string, password: string, fetchImpl: Fetch = (input, init) => fetch(input, init)): Promise<LoginResponse> {
    const headers = { 'Content-Type': 'application/x-www-form-urlencoded' };
    let response = await fetchImpl(`${baseUrl}/auth/cloud/login`, {
      method: 'POST', headers, body: new URLSearchParams({ email: username, password }).toString(),
    });
    if (response.status === 404) {
      response = await fetchImpl(`${baseUrl}/auth/token`, {
        method: 'POST', headers, body: new URLSearchParams({ username, password, grant_type: 'password' }).toString(),
      });
    }
    if (!response.ok) {
      throw new ApiError(response.status, response.status === 401 ? 'Wrong username or password.' : await readDetail(response));
    }
    return (await response.json()) as LoginResponse;
  }

  /** Redeem an invitation link as the signed-in, invited account. */
  static async acceptInvitation(baseUrl: string, accessToken: string, token: string, fetchImpl: Fetch = (input, init) => fetch(input, init)): Promise<{ tenant_id: string; workspace_id: string | null; person_id: string | null }> {
    const r = await fetchImpl(`${baseUrl}/api/v1/care/invitations/accept`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` }, body: JSON.stringify({ token }) });
    if (!r.ok) throw new ApiError(r.status, r.status === 400 ? 'This invitation link is not valid.' : await readDetail(r));
    return (await r.json()) as { tenant_id: string; workspace_id: string | null; person_id: string | null };
  }

  /** Tell the server this sign-in is over: the refresh token first (the device's sign-in; in the
   *  cloud edition its session row is ended too), then the access token. Each is best effort:
   *  a phone that cannot reach the server still signs out locally, and nothing here throws. */
  async logout(): Promise<boolean> {
    let ok = true;
    for (const token of [this.session.refreshToken, this.session.accessToken]) {
      if (!token) continue;
      try {
        const r = await this.fetchImpl(`${this.session.baseUrl}/auth/logout`, { method: 'POST', headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' } });
        if (!r.ok) ok = false;
      } catch {
        ok = false;
      }
    }
    return ok;
  }

  async workspaces(): Promise<WorkspaceList> {
    return this.request<WorkspaceList>('GET', '/api/v1/workspaces', undefined, false);
  }

  async people(): Promise<Person[]> {
    const out = await this.request<{ people: Person[] }>('GET', '/api/v1/care/people');
    return out.people;
  }

  async status(personId: string): Promise<PersonStatus> {
    return this.request<PersonStatus>('GET', `/api/v1/care/people/${personId}/status`);
  }

  async alerts(openOnly = true): Promise<Alert[]> {
    const out = await this.request<{ alerts: Alert[] }>('GET', `/api/v1/care/alerts?open_only=${openOnly}&limit=200`);
    return out.alerts;
  }

  async acknowledge(alertId: string, note = '', kind: 'on_my_way' | 'arrived' = 'on_my_way'): Promise<Alert> {
    return this.request<Alert>('POST', `/api/v1/care/alerts/${alertId}/acknowledge`, { kind, note });
  }

  /** The responder has reached the person: the watch then shows "[Name] has arrived". */
  async arrived(alertId: string): Promise<Alert> {
    return this.acknowledge(alertId, '', 'arrived');
  }

  /** Key safe, door, pets: only for tier 1 and 2 while the alert is open; the server logs every view. */
  async accessNotes(alertId: string): Promise<AccessNotes> {
    return this.request<AccessNotes>('GET', `/api/v1/care/alerts/${alertId}/access-notes`);
  }

  /** What happened. An alert stays open until an outcome is recorded. */
  async resolve(alertId: string, resolution: string, outcome: AlertOutcome = 'all_well'): Promise<Alert> {
    return this.request<Alert>('POST', `/api/v1/care/alerts/${alertId}/resolve`, { resolution, outcome });
  }

  async checkins(personId: string): Promise<{ checkins: Checkin[]; answers_included: boolean }> {
    return this.request('GET', `/api/v1/care/people/${personId}/checkins?limit=50`);
  }

  async visits(personId: string): Promise<Visit[]> {
    const out = await this.request<{ visits: Visit[] }>('GET', `/api/v1/care/people/${personId}/visits?limit=50`);
    return out.visits;
  }

  /** The carer is with the person: the watch says so and check-ins pause. */
  async arrive(visitId: string): Promise<Visit> {
    return this.request<Visit>('POST', `/api/v1/care/visits/${visitId}/arrive`, {});
  }

  /** The carer leaves: the record of what happened. */
  async depart(visitId: string, body: { outcome: VisitOutcome; notes: string; handover: string; tasks_done: string[] }): Promise<Visit> {
    return this.request<Visit>('POST', `/api/v1/care/visits/${visitId}/depart`, body);
  }

  /** How IRA speaks to this person; the person or their representative may change it. */
  async profile(personId: string): Promise<CommunicationProfile> {
    return this.request<CommunicationProfile>('GET', `/api/v1/care/people/${personId}/profile`);
  }

  async putProfile(personId: string, body: CommunicationProfile): Promise<CommunicationProfile> {
    const { person_id: _p, redirect_name: _n, topics_consented: _c, updated_by_role: _r, updated_at: _u, ...rest } = body;
    return this.request<CommunicationProfile>('PUT', `/api/v1/care/people/${personId}/profile`, rest);
  }

  /** What the person takes, as the care team recorded it. */
  async medications(personId: string): Promise<Medication[]> {
    const out = await this.request<{ medications: Medication[] }>('GET', `/api/v1/care/people/${personId}/medications`);
    return out.medications;
  }

  /** The current care plan, or null when none has been written yet. */
  async plan(personId: string): Promise<CarePlan | null> {
    const out = await this.request<{ plan: CarePlan | null }>('GET', `/api/v1/care/people/${personId}/plan`);
    return out.plan;
  }

  async reminders(personId: string, openOnly = false): Promise<Reminder[]> {
    const out = await this.request<{ reminders: Reminder[] }>('GET', `/api/v1/care/people/${personId}/reminders?open_only=${openOnly}&limit=50`);
    return out.reminders;
  }

  async addReminder(personId: string, text: string, dueAt: string): Promise<Reminder> {
    return this.request<Reminder>('POST', `/api/v1/care/people/${personId}/reminders`, { text, due_at: dueAt, kind: 'reminder' });
  }

  async actOnReminder(reminderId: string, action: 'confirm' | 'snooze' | 'skip' | 'cancel'): Promise<Reminder> {
    return this.request<Reminder>('POST', `/api/v1/care/reminders/${reminderId}/act`, { action, minutes: 10 });
  }

  /** What IRA remembers: summaries by day, which the person or their representative may change or clear. */
  async memory(personId: string): Promise<Memory[]> {
    const out = await this.request<{ memory: Memory[] }>('GET', `/api/v1/care/people/${personId}/memory`);
    return out.memory;
  }

  async editMemory(personId: string, day: string, summary: string): Promise<Memory> {
    return this.request<Memory>('PUT', `/api/v1/care/people/${personId}/memory/${day}`, { summary });
  }

  async forgetMemory(personId: string, day: string): Promise<void> {
    await this.request<void>('DELETE', `/api/v1/care/people/${personId}/memory/${day}`);
  }

  /** Which of the app's three experiences this account gets, and for whom. */
  async experience(): Promise<{ experience: 'wearer' | 'family' | 'carer'; can_manage: boolean; memberships: Membership[] }> {
    return this.request('GET', '/api/v1/care/me/experience');
  }

  /** How the app looks for this account, the same on every device. */
  async preferences(): Promise<{ appearance: Partial<Appearance>; updated_at: string | null }> {
    return this.request('GET', '/api/v1/care/me/preferences');
  }

  async putPreferences(appearance: Appearance): Promise<{ appearance: Partial<Appearance>; updated_at: string | null }> {
    return this.request('PUT', '/api/v1/care/me/preferences', appearance);
  }

  /** Help from the phone: opens the same SOS the watch would. */
  async phoneHelp(personId: string, note = ''): Promise<Alert> {
    return this.request<Alert>('POST', `/api/v1/care/people/${personId}/help`, { note });
  }

  async circle(personId: string): Promise<CircleMember[]> {
    const out = await this.request<{ members: CircleMember[] }>('GET', `/api/v1/care/people/${personId}/circle`);
    return out.members;
  }

  async consents(personId: string): Promise<ConsentList> {
    return this.request<ConsentList>('GET', `/api/v1/care/people/${personId}/consents`);
  }

  async recordConsent(personId: string, kind: string, granted: boolean, grantedBy: 'person' | 'legal_representative', name: string): Promise<void> {
    await this.request('POST', `/api/v1/care/people/${personId}/consents`, {
      kind, granted, granted_by: grantedBy, granted_by_name: name,
    });
  }

  async revokeConsent(consentId: string): Promise<void> {
    await this.request('POST', `/api/v1/care/consents/${consentId}/revoke`);
  }

  /** Talk to IRA from the person's own phone. The server streams one JSON object per line; each
   *  sentence goes to `onSentence` as soon as it arrives (all at once where streaming is not
   *  available). A connection lost part-way keeps what arrived and then fails. */
  async talk(personId: string, text: string, onSentence: (sentence: string) => void = () => {}): Promise<TalkReply> {
    if (!this.session.workspaceId) throw new ApiError(0, 'No care circle selected.');
    const response = await this.send('POST', `/api/v1/care/people/${personId}/talk`, () => ({
      Authorization: `Bearer ${this.session.accessToken}`, Accept: 'application/x-ndjson',
      'X-Workspace-ID': this.session.workspaceId!, 'Content-Type': 'application/json',
    }), { text }, this.streamImpl ?? this.fetchImpl);
    if (!response.ok) throw new ApiError(response.status, await readDetail(response));
    const out: TalkReply = { sentences: [], action: null, needsHelp: false };
    try {
      await forEachLine(response, (line) => {
        const before = out.sentences.length;
        addTalkLine(out, line);
        for (const s of out.sentences.slice(before)) onSentence(s);
      });
    } catch {
      throw new ApiError(0, 'The connection was lost before IRA finished answering.');
    }
    return out;
  }

  async askNow(personId: string): Promise<void> {
    await this.request('POST', `/api/v1/care/people/${personId}/checkins/ask-now`, { kind: 'wellbeing' });
  }

  async newDevice(personId: string): Promise<Device> {
    return this.request<Device>('POST', `/api/v1/care/people/${personId}/devices`, { platform: 'wearos' });
  }

  async reports(personId: string): Promise<Report[]> {
    const out = await this.request<{ reports: Report[] }>('GET', `/api/v1/care/people/${personId}/reports?kind=weekly_family&limit=12`);
    return out.reports;
  }

  /** Tie this phone's push token to the account's circle memberships here: a care
   *  alert is pushed only to the phones of the members it is for. */
  async registerPush(token: string, platform: string): Promise<void> {
    await this.request('POST', '/api/v1/care/push/register', { token, platform });
  }

  async unregisterPush(token: string): Promise<void> {
    await this.request('DELETE', '/api/v1/care/push/register', { token });
  }

  private async request<T>(method: string, path: string, body?: unknown, workspace = true): Promise<T> {
    if (workspace && !this.session.workspaceId) throw new ApiError(0, 'No care circle selected.');
    const response = await this.send(method, path, () => {
      const headers: Record<string, string> = { Authorization: `Bearer ${this.session.accessToken}`, Accept: 'application/json' };
      if (workspace) headers['X-Workspace-ID'] = this.session.workspaceId!;
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      return headers;
    }, body);
    if (!response.ok) throw new ApiError(response.status, await readDetail(response));
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }
}

async function readDetail(response: Response): Promise<string> {
  try {
    const data = (await response.json()) as { detail?: unknown; message?: unknown };
    const detail = data.detail ?? data.message;
    return typeof detail === 'string' ? detail : `The server answered ${response.status}.`;
  } catch {
    return `The server answered ${response.status}.`;
  }
}

/** The streamed reply, line by line: sentences first, then the closing object. */
export function parseTalk(raw: string): TalkReply {
  const out: TalkReply = { sentences: [], action: null, needsHelp: false };
  for (const line of raw.split('\n')) addTalkLine(out, line);
  return out;
}

function addTalkLine(out: TalkReply, line: string): void {
  if (!line.trim()) return;
  let obj: TalkLine;
  try { obj = JSON.parse(line) as TalkLine; } catch { return; }
  if (typeof obj.sentence === 'string') out.sentences.push(obj.sentence);
  if (obj.done) { out.action = obj.action ?? null; out.needsHelp = obj.needs_help === true; }
}
