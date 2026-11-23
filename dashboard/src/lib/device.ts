/**
 * The watch's side of the care API, from a browser. The simulator page uses
 * exactly the routes the real watch uses, with the same device token header,
 * so what the demo shows is what a watch would do.
 */
export interface DueCheckin { id: string; question_kind: string; question_text: string; status: string; safety: boolean; question_source?: string }
export interface Reading { kind: string; value: number; unit: string; recorded_at: string }
export interface TalkReply { sentences: string[]; action: Record<string, unknown> | null; needsHelp: boolean }

export class DeviceError extends Error { constructor(public readonly status: number, message: string) { super(message); } }

type Fetch = typeof fetch;

export class DeviceClient {
  constructor(private readonly baseUrl: string, private readonly token: string, private readonly f: Fetch = (input, init) => fetch(input, init)) {}

  static async pair(baseUrl: string, pairingCode: string, f: Fetch = (input, init) => fetch(input, init)): Promise<{ device_token: string; id: string; person_id: string }> {
    const r = await f(`${baseUrl}/api/v1/care/devices/pair`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pairing_code: pairingCode, app_version: 'browser-simulator' }) });
    if (!r.ok) throw new DeviceError(r.status, 'That pairing code was not accepted.');
    return (await r.json()) as { device_token: string; id: string; person_id: string };
  }

  me() { return this.req<{ id: string; status: string; fall: { fall_detection_enabled: boolean; fall_countdown_s: number } }>('GET', '/api/v1/care/devices/me'); }
  due() { return this.req<{ checkin: DueCheckin | null }>('GET', '/api/v1/care/devices/me/checkins/due').then((o) => o.checkin); }
  answer(id: string, text: string, via: 'button' | 'voice') { return this.req('POST', `/api/v1/care/devices/me/checkins/${id}/answer`, { text, via }); }
  repeated(id: string) { return this.req('POST', `/api/v1/care/devices/me/checkins/${id}/repeated`, {}); }
  missed(id: string) { return this.req('POST', `/api/v1/care/devices/me/checkins/${id}/missed`, {}); }
  report(readings: Reading[], state: { on_wrist?: boolean; battery_percent?: number }) {
    return this.req('POST', '/api/v1/care/devices/me/report', { readings, background_sensor_permission: true, battery_percent: 70, ...state });
  }
  help(via: 'button' | 'voice' | 'fall', location?: { latitude: number; longitude: number; accuracy_m?: number }) {
    return this.req<{ id: string; kind: string; status: string; at_home: boolean | null }>('POST', '/api/v1/care/devices/me/help', { via, location: location ?? null });
  }
  fallStarted(peakG: number, stillSeconds: number) { return this.req<{ id: string }>('POST', '/api/v1/care/devices/me/fall', { peak_g: peakG, still_seconds: stillSeconds }); }
  fallOk() { return this.req('POST', '/api/v1/care/devices/me/fall/ok', {}); }

  /** Talk to IRA: the server streams one JSON object per line; each sentence is handed on as it arrives. */
  async talk(text: string, onSentence: (s: string) => void): Promise<TalkReply> {
    const r = await this.f(`${this.baseUrl}/api/v1/care/devices/me/talk`, { method: 'POST', headers: this.headers(true), body: JSON.stringify({ text }) });
    if (!r.ok) throw new DeviceError(r.status, 'IRA could not answer.');
    const raw = await r.text();
    return parseTalk(raw, onSentence);
  }

  private headers(json: boolean): Record<string, string> {
    const h: Record<string, string> = { 'X-Device-Token': this.token, Accept: 'application/json' };
    if (json) h['Content-Type'] = 'application/json';
    return h;
  }

  private async req<T = unknown>(method: string, path: string, body?: unknown): Promise<T> {
    let r: Response;
    try { r = await this.f(this.baseUrl + path, { method, headers: this.headers(body !== undefined), body: body === undefined ? undefined : JSON.stringify(body) }); }
    catch { throw new DeviceError(0, 'offline'); }
    if (r.status === 401) throw new DeviceError(401, 'The device token was not accepted.');
    if (!r.ok) throw new DeviceError(r.status, `${r.status}`);
    const text = await r.text();
    return (text ? JSON.parse(text) : null) as T;
  }
}

export function parseTalk(raw: string, onSentence?: (s: string) => void): TalkReply {
  const out: TalkReply = { sentences: [], action: null, needsHelp: false };
  for (const line of raw.split('\n')) {
    if (!line.trim()) continue;
    let obj: { sentence?: string; done?: boolean; action?: Record<string, unknown> | null; needs_help?: boolean };
    try { obj = JSON.parse(line); } catch { continue; }
    if (typeof obj.sentence === 'string') { out.sentences.push(obj.sentence); onSentence?.(obj.sentence); }
    if (obj.done) { out.action = obj.action ?? null; out.needsHelp = obj.needs_help === true; }
  }
  return out;
}

/** Readings the simulator injects, shaped as the watch would send them. */
export function heartRateRun(values: number[], now = new Date(), stepSeconds = 60): Reading[] {
  const start = now.getTime() - (values.length - 1) * stepSeconds * 1000;
  return values.map((v, i) => ({ kind: 'heart_rate', value: v, unit: 'bpm', recorded_at: new Date(start + i * stepSeconds * 1000).toISOString() }));
}

export function noMovement(now = new Date()): Reading[] {
  return [{ kind: 'steps', value: 0, unit: '', recorded_at: now.toISOString() }, { kind: 'movement', value: 0, unit: '', recorded_at: now.toISOString() }];
}

export function wristOff(now = new Date()): Reading[] {
  return [{ kind: 'on_wrist', value: 0, unit: '', recorded_at: now.toISOString() }];
}
