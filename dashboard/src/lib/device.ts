/**
 * The watch's side of the care API, from a browser. The simulator page uses
 * exactly the routes the real watch uses, with the same device token header,
 * so what the demo shows is what a watch would do.
 */
export interface DueCheckin { id: string; question_kind: string; question_text: string; status: string; safety: boolean; question_source?: string }
export interface Reading { kind: string; value: number; unit: string; recorded_at: string }
export interface TalkReply { sentences: string[]; action: Record<string, unknown> | null; needsHelp: boolean }
/** The true state of the person's SOS, as the server reports it. */
export interface SosState { state: 'idle' | 'asking' | 'received' | 'on_the_way' | 'arrived' | 'closed'; alert_id: string | null; responder_name: string | null }
export interface ReadingSettings { heart_rate_min: number | null; heart_rate_max: number | null; reading_upload_interval_s: number }
export interface DeviceMe { id: string; status: string; fall: { fall_detection_enabled: boolean; fall_countdown_s: number }; readings: ReadingSettings; sos: SosState }

export class DeviceError extends Error { constructor(public readonly status: number, message: string) { super(message); } }

type Fetch = typeof fetch;

export class DeviceClient {
  constructor(private readonly baseUrl: string, private readonly token: string, private readonly f: Fetch = (input, init) => fetch(input, init)) {}

  static async pair(baseUrl: string, pairingCode: string, f: Fetch = (input, init) => fetch(input, init)): Promise<{ device_token: string; id: string; person_id: string }> {
    const r = await f(`${baseUrl}/api/v1/care/devices/pair`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pairing_code: pairingCode, app_version: 'browser-simulator' }) });
    if (!r.ok) throw new DeviceError(r.status, 'That pairing code was not accepted.');
    return (await r.json()) as { device_token: string; id: string; person_id: string };
  }

  me() { return this.req<DeviceMe>('GET', '/api/v1/care/devices/me'); }
  /** The state the watch face shows for a Help request or a fall: never guessed, always the server's word. */
  sos() { return this.req<SosState>('GET', '/api/v1/care/devices/me/sos'); }
  due() { return this.req<{ checkin: DueCheckin | null }>('GET', '/api/v1/care/devices/me/checkins/due').then((o) => o.checkin); }
  answer(id: string, text: string, via: 'button' | 'voice') { return this.req('POST', `/api/v1/care/devices/me/checkins/${id}/answer`, { text, via }); }
  repeated(id: string) { return this.req('POST', `/api/v1/care/devices/me/checkins/${id}/repeated`, {}); }
  missed(id: string) { return this.req('POST', `/api/v1/care/devices/me/checkins/${id}/missed`, {}); }
  report(readings: Reading[], state: { on_wrist?: boolean; battery_percent?: number }) {
    return this.req('POST', '/api/v1/care/devices/me/report', { readings, background_sensor_permission: true, battery_percent: 70, ...state });
  }
  help(via: 'button' | 'voice' | 'fall', location?: { latitude: number; longitude: number; accuracy_m?: number }, idempotencyKey?: string) {
    return this.req<{ id: string; kind: string; status: string; at_home: boolean | null; sos?: SosState }>('POST', '/api/v1/care/devices/me/help', { via, location: location ?? null }, idempotencyKey);
  }
  fallStarted(peakG: number, stillSeconds: number) { return this.req<{ id: string }>('POST', '/api/v1/care/devices/me/fall', { peak_g: peakG, still_seconds: stillSeconds }); }
  fallOk() { return this.req('POST', '/api/v1/care/devices/me/fall/ok', {}); }

  /** Talk to IRA: the server streams one JSON object per line; each sentence is handed on as it arrives. */
  async talk(text: string, onSentence: (s: string) => void): Promise<TalkReply> {
    const r = await this.f(`${this.baseUrl}/api/v1/care/devices/me/talk`, { method: 'POST', headers: this.headers(true), body: JSON.stringify({ text }) });
    if (!r.ok) throw new DeviceError(r.status, 'IRA could not answer.');
    return readTalk(r, onSentence);
  }

  private headers(json: boolean, idempotencyKey?: string): Record<string, string> {
    const h: Record<string, string> = { 'X-Device-Token': this.token, Accept: 'application/json' };
    if (json) h['Content-Type'] = 'application/json';
    if (idempotencyKey) h['X-Idempotency-Key'] = idempotencyKey;
    return h;
  }

  private async req<T = unknown>(method: string, path: string, body?: unknown, idempotencyKey?: string): Promise<T> {
    let r: Response;
    try { r = await this.f(this.baseUrl + path, { method, headers: this.headers(body !== undefined, idempotencyKey), body: body === undefined ? undefined : JSON.stringify(body) }); }
    catch { throw new DeviceError(0, 'offline'); }
    if (r.status === 401) throw new DeviceError(401, 'The device token was not accepted.');
    if (!r.ok) throw new DeviceError(r.status, `${r.status}`);
    const text = await r.text();
    return (text ? JSON.parse(text) : null) as T;
  }
}

/**
 * Read the talk reply as it arrives, one JSON line at a time, so each sentence is spoken as
 * soon as the server sends it: the instant acknowledgement first, then the model's sentences.
 * Reading the whole body before speaking (as before round 4) held the first words back until
 * the model had finished.
 */
export async function readTalk(r: Response, onSentence?: (s: string) => void): Promise<TalkReply> {
  const reader = r.body?.getReader?.();
  if (!reader) return parseTalk(await r.text(), onSentence);
  const out: TalkReply = { sentences: [], action: null, needsHelp: false };
  const take = (lines: string) => {
    const part = parseTalk(lines, onSentence);
    out.sentences.push(...part.sentences);
    if (/"done"\s*:\s*true/.test(lines)) { out.action = part.action; out.needsHelp = part.needsHelp; }
  };
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const end = buffer.lastIndexOf('\n');
    if (end >= 0) { take(buffer.slice(0, end + 1)); buffer = buffer.slice(end + 1); }
  }
  buffer += decoder.decode();
  if (buffer.trim()) take(buffer);
  return out;
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

/** The words the watch face shows for an SOS state. Only the server's word says help is coming. */
export function sosWords(s: SosState | null, pending: 'sending' | 'waiting' | null): string | null {
  if (pending === 'sending') return 'Sending…';
  if (pending === 'waiting') return 'Waiting for connection. Help request saved.';
  if (!s) return null;
  switch (s.state) {
    case 'received': return 'Help request received.';
    case 'on_the_way': return `${s.responder_name ?? 'Someone'} is on the way.`;
    case 'arrived': return `${s.responder_name ?? 'Someone'} has arrived.`;
    case 'closed': return 'This is now resolved.';
    default: return null;
  }
}

/** Is this heart-rate sample outside the carer's range? No range set means no rule. */
export function outOfRange(bpm: number, r: ReadingSettings | null): boolean {
  if (!r) return false;
  return (r.heart_rate_min !== null && bpm < r.heart_rate_min) || (r.heart_rate_max !== null && bpm > r.heart_rate_max);
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
