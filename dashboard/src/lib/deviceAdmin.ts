/** Deployment administration (D-085, D-089): the words, checks and error reading the screen
 *  needs, with no browser in them so they can be tested on their own.
 *
 *  A deployment administrator is a grant the operator writes, never a care role. The screen
 *  asks the server once whether the signed-in account holds it and shows nothing else when it
 *  does not. Issuing, approving and revoking may need the person to confirm who they are
 *  again (step-up); the server says so with a 403 that names `step_up_required`.
 */

export type DeviceAction = 'MAINTENANCE_WINDOW' | 'RESTORE_RESTRICTIONS' | 'FACTORY_RESET';
export const DEVICE_ACTIONS: DeviceAction[] = ['MAINTENANCE_WINDOW', 'RESTORE_RESTRICTIONS', 'FACTORY_RESET'];

/** The three actions in a person's words. Nothing else can be chosen. */
export const ACTION_WORDS: Record<DeviceAction, string> = {
  MAINTENANCE_WINDOW: 'Open a maintenance window',
  RESTORE_RESTRICTIONS: 'Restore the restrictions now',
  FACTORY_RESET: 'Factory reset (two administrators)',
};

export const ACTION_HELP: Record<DeviceAction, string> = {
  MAINTENANCE_WINDOW: 'Lets the developer options be opened on the watch until the window ends (1 to 15 minutes). Signed at once.',
  RESTORE_RESTRICTIONS: 'Puts every restriction back on the watch now. Signed at once.',
  FACTORY_RESET: 'Wipes the watch. This is a request: a different deployment administrator must approve it within 30 minutes, or it lapses unsigned. You cannot approve your own request.',
};

export const MIN_MINUTES = 1;
export const MAX_MINUTES = 15;
/** The server asks for at least ten characters of reason (care/schemas.py). */
export const MIN_REASON = 10;

/** Where an instruction stands, as the server says it (care/deviceadmin.py `_status`). */
export type InstructionStatus = 'signed' | 'delivered' | 'applied' | 'restored' | 'refused' | 'revoked' | 'awaiting_approval' | 'lapsed' | 'withdrawn';

export const STATUS_WORDS: Record<InstructionStatus, string> = {
  signed: 'Signed, not yet collected',
  delivered: 'Collected by the watch',
  applied: 'Applied by the watch',
  restored: 'Restrictions restored',
  refused: 'Refused by the watch',
  revoked: 'Revoked',
  awaiting_approval: 'Waiting for a second administrator',
  lapsed: 'Lapsed: not approved in time',
  withdrawn: 'Withdrawn',
};

export function statusWords(status: string): string {
  return (STATUS_WORDS as Record<string, string>)[status] ?? status.replace(/_/g, ' ');
}

/** The pill colour for a status: red for a refusal, grey for anything over, blue for live. */
export function statusTone(status: string): 'ok' | 'notice' | 'sos' | 'grey' {
  if (status === 'applied' || status === 'restored') return 'ok';
  if (status === 'refused') return 'sos';
  if (status === 'revoked' || status === 'lapsed' || status === 'withdrawn') return 'grey';
  return 'notice';
}

/** Rows that can still be revoked: anything signed and not yet over, or a request still waiting. */
export function isLive(status: string): boolean {
  return status === 'signed' || status === 'delivered' || status === 'applied' || status === 'awaiting_approval';
}

export function canApprove(status: string): boolean {
  return status === 'awaiting_approval';
}

export interface InstructionEvent { event: string; actor_kind: string; actor_id: string | null; detail: string; at: string }

export interface Instruction {
  id: string; device_id: string; action: string; state: string; status: string;
  issued_at: string | null; expires_at: string | null; issued_by: string | null; reason: string;
  delivered_at: string | null; revoked_at: string | null; revoked_by: string | null;
  outcome: string | null; outcome_reason: string | null; outcome_at: string | null;
  requested_at: string | null; approval_deadline: string | null; window_minutes: number | null;
  approved_by: string | null; approved_at: string | null;
  events?: InstructionEvent[];
}

/** What the server says about the signed-in account's standing as a deployment administrator. */
export interface AdminMe {
  deployment_admin: boolean;
  auth_age_seconds: number | null;
  step_up_required: boolean;
  max_age_seconds: number;
  mfa_enrolled: boolean;
}

export interface StepUpResult { access_token: string; token_type: string; expires_in: number; auth_time: number; amr: string[] }

export const EVENT_WORDS: Record<string, string> = {
  requested: 'Requested', approved: 'Approved', approval_refused: 'Approval refused', issued: 'Issued', delivered: 'Collected by the watch',
  revoked: 'Revoked', applied: 'Applied', refused: 'Refused by the watch', restored: 'Restored',
};

export function eventWords(event: string): string {
  return EVENT_WORDS[event] ?? event.replace(/_/g, ' ');
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** An identifier the server would accept: eight-four-four-four-twelve hex digits, any case. */
export function isUuid(s: string): boolean {
  return UUID.test(s.trim());
}

export interface Target { tenantId: string; workspaceId: string; deviceId: string }

/** Which of the three fields are not identifiers yet, in a person's words. */
export function targetProblems(t: Target): string[] {
  const out: string[] = [];
  if (!isUuid(t.tenantId)) out.push('The tenant id is not complete.');
  if (!isUuid(t.workspaceId)) out.push('The workspace id is not complete.');
  if (!isUuid(t.deviceId)) out.push('The watch id is not complete.');
  return out;
}

export function targetComplete(t: Target): boolean {
  return targetProblems(t).length === 0;
}

/** Why an issue form cannot be sent yet, or nothing. */
export function issueProblems(action: string, minutes: number, reason: string): string[] {
  const out: string[] = [];
  if (!(DEVICE_ACTIONS as string[]).includes(action)) out.push('Choose one of the three actions.');
  if (!Number.isInteger(minutes) || minutes < MIN_MINUTES || minutes > MAX_MINUTES) out.push(`Minutes must be a whole number from ${MIN_MINUTES} to ${MAX_MINUTES}.`);
  if (reason.trim().length < MIN_REASON) out.push(`Say why, in at least ${MIN_REASON} characters.`);
  return out;
}

/** How a refusal is read. The server's 403 body is either `{detail: {code: "step_up_required", ...}}`
 *  or a string with the code somewhere in `error` or `code`; both, and anything else that
 *  names the code, mean "confirm it is you". Any other 403 means "not a deployment administrator". */
export type Refusal = 'step_up' | 'not_admin' | 'other';

export function classifyRefusal(status: number, body: unknown, message = ''): Refusal {
  if (status !== 403) return 'other';
  return mentionsStepUp(body) || /step_up_required/.test(message) ? 'step_up' : 'not_admin';
}

function mentionsStepUp(v: unknown, depth = 0): boolean {
  if (depth > 4 || v === null || v === undefined) return false;
  if (typeof v === 'string') return v.includes('step_up_required');
  if (Array.isArray(v)) return v.some((x) => mentionsStepUp(x, depth + 1));
  if (typeof v === 'object') return Object.values(v as Record<string, unknown>).some((x) => mentionsStepUp(x, depth + 1));
  return false;
}

/** The message a 403 body carries, for showing verbatim (the self-approval refusal, for one). */
export function refusalMessage(body: unknown, fallback: string): string {
  if (body && typeof body === 'object') {
    const d = (body as { detail?: unknown }).detail;
    if (typeof d === 'string') return d;
    if (d && typeof d === 'object' && typeof (d as { message?: unknown }).message === 'string') return (d as { message: string }).message;
  }
  return fallback;
}

/** The step-up standing as the screen states it before any action is tried. */
/**
 * What the administrator is told about confirming who they are. `elapsedSeconds` is the time since
 * `me` was read from the server: the confirmation keeps ageing while the page is open, so the line
 * counts on and, once it passes the server's limit, says the next action will ask (round 10: it had
 * said "6 s ago" a minute and more later, while the server was about to refuse).
 */
export function stepUpWords(me: AdminMe, elapsedSeconds = 0): string {
  const ask = `Before an action is carried out you will be asked to confirm who you are${me.mfa_enrolled ? ' with your password and your authenticator code' : ' with your password'}.`;
  if (me.step_up_required) return ask;
  if (me.auth_age_seconds === null) return 'You will not be asked to confirm who you are for the next action.';
  const age = me.auth_age_seconds + Math.max(0, elapsedSeconds);
  if (me.max_age_seconds > 0 && age >= me.max_age_seconds) return ask;
  return `You confirmed who you are ${agoWords(age)}; actions need a confirmation under ${Math.round(me.max_age_seconds / 60)} minutes old.`;
}

export function agoWords(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

export function when(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }) + ' UTC';
}

/** Memory, not storage: whether the signed-in account is a deployment administrator, kept
 *  for the life of the page so the navigation asks once per sign-in. */
const adminCache = new Map<string, boolean>();

export function cacheKey(baseUrl: string, accessToken: string): string { return `${baseUrl}|${accessToken}`; }
export function cachedAdmin(key: string): boolean | undefined { return adminCache.get(key); }
export function rememberAdmin(key: string, value: boolean): void { adminCache.set(key, value); }
export function forgetAdmins(): void { adminCache.clear(); }
