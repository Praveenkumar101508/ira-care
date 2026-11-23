import type { AlertOutcome, AccessNotes, TalkLine, TalkReply,
  Alert, Checkin, CircleMember, ConsentList, Device, LoginResponse, Person, PersonStatus, Report, WorkspaceList,
} from './types';

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
}

type Fetch = typeof fetch;

/**
 * The family app's view of the backend: login, workspace selection and the
 * care API. Every care call carries the bearer token and the selected
 * workspace header; the backend verifies both, never trusting the header
 * alone.
 */
export class CareClient {
  // `fetch` is wrapped rather than passed bare: on the web, calling it with another `this` throws.
  constructor(private readonly session: Session, private readonly fetchImpl: Fetch = (input, init) => fetch(input, init)) {}

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

  /** Talk to IRA from the person's own phone. The server streams one JSON object per line. */
  async talk(personId: string, text: string): Promise<TalkReply> {
    const raw = await this.requestText('POST', `/api/v1/care/people/${personId}/talk`, { text });
    return parseTalk(raw);
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

  private async requestText(method: string, path: string, body?: unknown): Promise<string> {
    const headers: Record<string, string> = { Authorization: `Bearer ${this.session.accessToken}`, Accept: 'application/x-ndjson' };
    if (!this.session.workspaceId) throw new ApiError(0, 'No care circle selected.');
    headers['X-Workspace-ID'] = this.session.workspaceId;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let response: Response;
    try {
      response = await this.fetchImpl(this.session.baseUrl + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (e) {
      throw new ApiError(0, 'No connection. Check the network and try again.');
    }
    if (response.status === 401) throw new ApiError(401, 'Please sign in again.');
    if (!response.ok) throw new ApiError(response.status, await readDetail(response));
    return response.text();
  }

  private async request<T>(method: string, path: string, body?: unknown, workspace = true): Promise<T> {
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.session.accessToken}`,
      Accept: 'application/json',
    };
    if (workspace) {
      if (!this.session.workspaceId) throw new ApiError(0, 'No care circle selected.');
      headers['X-Workspace-ID'] = this.session.workspaceId;
    }
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let response: Response;
    try {
      response = await this.fetchImpl(this.session.baseUrl + path, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      throw new ApiError(0, 'No connection. Check the network and try again.');
    }
    if (response.status === 401) throw new ApiError(401, 'Please sign in again.');
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
  for (const line of raw.split('\n')) {
    if (!line.trim()) continue;
    let obj: TalkLine;
    try { obj = JSON.parse(line) as TalkLine; } catch { continue; }
    if (typeof obj.sentence === 'string') out.sentences.push(obj.sentence);
    if (obj.done) { out.action = obj.action ?? null; out.needsHelp = obj.needs_help === true; }
  }
  return out;
}
