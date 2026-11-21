import type {
  Alert, Checkin, CircleMember, ConsentList, Device, LoginResponse, Person, PersonStatus, WorkspaceList,
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
  constructor(private readonly session: Session, private readonly fetchImpl: Fetch = fetch) {}

  static async login(baseUrl: string, username: string, password: string, fetchImpl: Fetch = fetch): Promise<LoginResponse> {
    const body = new URLSearchParams({ username, password, grant_type: 'password' });
    const response = await fetchImpl(`${baseUrl}/auth/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
    });
    if (!response.ok) {
      throw new ApiError(response.status, response.status === 401 ? 'Wrong username or password.' : await readDetail(response));
    }
    return (await response.json()) as LoginResponse;
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

  async acknowledge(alertId: string, note = ''): Promise<Alert> {
    return this.request<Alert>('POST', `/api/v1/care/alerts/${alertId}/acknowledge`, { kind: 'on_my_way', note });
  }

  async resolve(alertId: string, resolution: string): Promise<Alert> {
    return this.request<Alert>('POST', `/api/v1/care/alerts/${alertId}/resolve`, { resolution });
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

  async askNow(personId: string): Promise<void> {
    await this.request('POST', `/api/v1/care/people/${personId}/checkins/ask-now`, { kind: 'wellbeing' });
  }

  async newDevice(personId: string): Promise<Device> {
    return this.request<Device>('POST', `/api/v1/care/people/${personId}/devices`, { platform: 'wearos' });
  }

  async registerPush(token: string, platform: string): Promise<void> {
    await this.request('POST', '/mobile/devices', { token, platform }, false);
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
