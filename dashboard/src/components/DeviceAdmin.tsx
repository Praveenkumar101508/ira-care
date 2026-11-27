'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Api, ApiError } from '@/lib/api';
import {
  ACTION_HELP, ACTION_WORDS, DEVICE_ACTIONS, MAX_MINUTES, MIN_MINUTES, MIN_REASON, canApprove, classifyRefusal, eventWords, isLive, isUuid, issueProblems,
  refusalMessage, statusTone, statusWords, stepUpWords, targetComplete, targetProblems, when,
  type AdminMe, type DeviceAction, type Instruction, type Target,
} from '@/lib/deviceAdmin';
import { LiveGate, REFRESH_MS, liveWords, type LiveStatus } from '@/lib/live';
import type { StoredSession } from '@/lib/session';
import type { Device, Person } from '@/lib/types';

/** Refused: the account holds no deployment administrator grant. Nothing else is shown. */
export function NotDeploymentAdmin() {
  return (
    <section data-testid="not-admin" aria-label="Deployment administration">
      <h2>Deployment administration</h2>
      <p>This area is for deployment administrators.</p>
      <p><a href="/">Back to the people you look after</a></p>
    </section>
  );
}

/** "Confirm it is you": a password (and an authenticator code when one is enrolled), sent once
 *  and kept nowhere. On success the caller carries out the action that was refused. */
export function StepUpPanel({ mfaEnrolled, busy, error, onConfirm, onCancel, actionLabel }: {
  mfaEnrolled: boolean; busy: boolean; error: string; actionLabel: string;
  onConfirm: (password: string, totp: string) => Promise<void>; onCancel: () => void;
}) {
  const [password, setPassword] = useState('');
  const [totp, setTotp] = useState('');
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const p = password; const t = totp;
    setPassword(''); setTotp('');
    await onConfirm(p, t);
  };
  return (
    <form className="card" data-testid="step-up" aria-labelledby="step-up-title" onSubmit={submit} style={{ borderColor: 'var(--notice)' }}>
      <h3 id="step-up-title">Confirm it is you</h3>
      <p className="muted">Before this is carried out ({actionLabel}), please confirm who you are. Your password is sent once and not kept.</p>
      <div className="row">
        <label>Password <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        {mfaEnrolled ? <label>Authenticator code <input inputMode="numeric" autoComplete="one-time-code" value={totp} onChange={(e) => setTotp(e.target.value)} required /></label> : null}
      </div>
      {error ? <p className="error" role="alert" data-testid="step-up-error">{error}</p> : null}
      <div className="row">
        <button type="submit" disabled={busy || !password || (mfaEnrolled && !totp)}>Confirm and continue</button>
        <button type="button" className="grey" onClick={onCancel} disabled={busy}>Cancel</button>
      </div>
    </form>
  );
}

function Pill({ status }: { status: string }) {
  return <span className={`pill ${statusTone(status)}`} data-testid="status">{statusWords(status)}</span>;
}

/** Every instruction for the chosen watch, with who did what and when, and the audit trail. */
export function InstructionTable({ rows, busy, onApprove, onRevoke }: {
  rows: Instruction[]; busy: boolean;
  onApprove: (row: Instruction) => void; onRevoke: (row: Instruction, reason: string) => void;
}) {
  const [revoking, setRevoking] = useState<string | null>(null);
  const [reason, setReason] = useState('');
  if (rows.length === 0) return <p className="muted" data-testid="no-instructions">No instructions have been sent to this watch.</p>;
  return (
    <table data-testid="instructions">
      <thead>
        <tr><th>Action</th><th>Where it stands</th><th>Issued or requested</th><th>Approved</th><th>Revoked</th><th>Reason</th><th>Audit trail</th><th>Actions</th></tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id} data-testid="instruction-row">
            <td>{(ACTION_WORDS as Record<string, string>)[r.action] ?? r.action}{r.window_minutes ? <span className="muted"> · {r.window_minutes} min</span> : null}</td>
            <td><Pill status={r.status} /></td>
            <td>{r.requested_at && !r.issued_at ? `requested ${when(r.requested_at)}` : when(r.issued_at)}{r.issued_by ? <span className="muted"> by {r.issued_by}</span> : null}{r.expires_at ? <span className="muted"> · ends {when(r.expires_at)}</span> : null}{r.approval_deadline && r.status === 'awaiting_approval' ? <span className="muted"> · approve by {when(r.approval_deadline)}</span> : null}</td>
            <td>{r.approved_at ? <>{when(r.approved_at)}{r.approved_by ? <span className="muted"> by {r.approved_by}</span> : null}</> : <span className="muted">—</span>}</td>
            <td>{r.revoked_at ? <>{when(r.revoked_at)}{r.revoked_by ? <span className="muted"> by {r.revoked_by}</span> : null}</> : <span className="muted">—</span>}</td>
            <td>{r.reason}{r.outcome_reason ? <span className="muted"> · watch: {r.outcome_reason}</span> : null}</td>
            <td>
              <details>
                <summary>{(r.events ?? []).length} events</summary>
                <ul className="plain" data-testid="audit-trail">
                  {(r.events ?? []).map((e, i) => <li key={i}>{when(e.at)} · {eventWords(e.event)} · {e.actor_kind}{e.actor_id ? ` ${e.actor_id}` : ''}{e.detail ? ` · ${e.detail}` : ''}</li>)}
                </ul>
              </details>
            </td>
            <td>
              {canApprove(r.status) ? <button className="ok" disabled={busy} onClick={() => onApprove(r)}>Approve</button> : null}
              {isLive(r.status) ? (
                revoking === r.id ? (
                  <span className="row">
                    <label>Why revoke <input value={reason} onChange={(e) => setReason(e.target.value)} /></label>
                    <button className="sos" disabled={busy || reason.trim().length < MIN_REASON} onClick={() => { onRevoke(r, reason.trim()); setRevoking(null); setReason(''); }}>Confirm revoke</button>
                    <button className="grey" onClick={() => { setRevoking(null); setReason(''); }}>Keep</button>
                  </span>
                ) : <button className="grey" disabled={busy} onClick={() => setRevoking(r.id)}>Revoke</button>
              ) : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const firstAction: DeviceAction = DEVICE_ACTIONS[0];

type Pending = { label: string; run: () => Promise<void> } | null;

/** The deployment administrator's screen: choose a watch, see every instruction sent to it, send one. */
export function DeviceAdminScreen({ api, session, me, onMe }: { api: Api; session: StoredSession; me: AdminMe; onMe?: (me: AdminMe) => void }) {
  const [target, setTarget] = useState<Target>({ tenantId: '', workspaceId: session.workspaceId ?? '', deviceId: '' });
  const [people, setPeople] = useState<Person[]>([]);
  const [peopleError, setPeopleError] = useState('');
  const [personId, setPersonId] = useState('');
  const [devices, setDevices] = useState<Device[]>([]);
  const [devicesError, setDevicesError] = useState('');
  const [rows, setRows] = useState<Instruction[] | null>(null);
  const [historyError, setHistoryError] = useState('');
  const [live, setLive] = useState<LiveStatus>({ lastOk: null, failures: 0 });
  const [clock, setClock] = useState(() => new Date());
  const [action, setAction] = useState(firstAction);
  const [minutes, setMinutes] = useState(10);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [actionError, setActionError] = useState('');
  const [pending, setPending] = useState<Pending>(null);
  const [stepUpError, setStepUpError] = useState('');
  const gate = useRef(new LiveGate());
  const complete = targetComplete(target);

  // The tenant comes from the sign-in's workspace list; a failure leaves the field to be typed.
  useEffect(() => {
    let gone = false;
    api.workspaces().then((w) => { if (!gone && w.tenant_id) setTarget((t) => (t.tenantId ? t : { ...t, tenantId: w.tenant_id })); }).catch(() => {});
    api.people().then((p) => { if (!gone) { setPeople(p); setPeopleError(''); } }).catch(() => { if (!gone) setPeopleError('The people list could not be loaded; type the watch id instead.'); });
    return () => { gone = true; };
  }, [api]);

  useEffect(() => {
    if (!personId) { setDevices([]); setDevicesError(''); return; }
    let gone = false;
    api.personDevices(personId).then((d) => { if (!gone) { setDevices(d); setDevicesError(''); } })
      .catch((e) => { if (!gone) { setDevices([]); setDevicesError(e instanceof ApiError ? `This person's watches could not be loaded: ${e.message}` : 'This person’s watches could not be loaded.'); } });
    return () => { gone = true; };
  }, [api, personId]);

  const load = useCallback(async () => {
    if (!complete) return;
    const ticket = gate.current.begin();
    if (ticket === null) return;
    try {
      const list = await api.deviceInstructions(target.tenantId.trim(), target.workspaceId.trim(), target.deviceId.trim());
      if (!gate.current.current(ticket)) return;
      setRows(list); setHistoryError('');
      setLive({ lastOk: new Date(), failures: 0 });
    } catch (e) {
      if (!gate.current.current(ticket)) return;
      setLive((l) => ({ ...l, failures: l.failures + 1 }));
      if (e instanceof ApiError && e.status === 403) {
        setHistoryError(classifyRefusal(403, e.body, e.message) === 'step_up' ? 'The history needs you to confirm who you are first.' : 'The history is for deployment administrators only.');
      } else {
        setHistoryError(e instanceof ApiError ? `The history could not be loaded: ${e.message}` : 'The history could not be loaded.');
      }
    } finally {
      gate.current.end(ticket);
    }
  }, [api, complete, target.tenantId, target.workspaceId, target.deviceId]);

  // Another watch: nothing of the last one may show, and nothing in flight for it may land.
  useEffect(() => {
    gate.current.next();
    setRows(null); setHistoryError(''); setLive({ lastOk: null, failures: 0 }); setNotice(''); setActionError('');
  }, [target.tenantId, target.workspaceId, target.deviceId]);

  useEffect(() => {
    if (!complete) return;
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    const onVisible = () => { if (document.visibilityState === 'visible') void load(); };
    document.addEventListener('visibilitychange', onVisible);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', onVisible); };
  }, [load, complete]);
  useEffect(() => { const t = window.setInterval(() => setClock(new Date()), 5_000); return () => window.clearInterval(t); }, []);

  const refreshMe = useCallback(async () => {
    try { const m = await api.deploymentAdminMe(); onMe?.(m); } catch { /* the standing shown stays as it was */ }
  }, [api, onMe]);

  /** Run an action. A refusal that asks for confirmation opens the panel and keeps the action
   *  to run again, once, after the confirmation. Nothing is retried on any other failure. */
  const act = useCallback(async (label: string, run: () => Promise<void>, retried = false) => {
    setBusy(true); setActionError(''); setNotice('');
    try {
      await run();
      await load();
    } catch (e) {
      if (!(e instanceof ApiError)) { setActionError(`${label} failed.`); return; }
      const kind = classifyRefusal(e.status, e.body, e.message);
      if (kind === 'step_up' && !retried) { setPending({ label, run }); setStepUpError(''); return; }
      if (kind === 'step_up') { setActionError(`${label} was refused again after you confirmed who you are. Please sign in again and retry.`); return; }
      if (kind === 'not_admin') { setActionError(refusalMessage(e.body, e.message)); return; }
      if (e.status === 0) { setActionError(`${label} could not be sent: ${e.message}`); return; }
      setActionError(`${label} was refused: ${e.message}`);
    } finally { setBusy(false); }
  }, [load]);

  const confirmStepUp = async (password: string, totp: string) => {
    if (!pending) return;
    setBusy(true); setStepUpError('');
    try {
      await api.stepUp(password, totp || undefined);
    } catch (e) {
      setStepUpError(e instanceof ApiError ? e.message : 'Confirming who you are failed.');
      setBusy(false);
      return;
    }
    const p = pending;
    setPending(null);
    setBusy(false);
    void refreshMe();
    await act(p.label, p.run, true);
  };

  const t = { tenant_id: target.tenantId.trim(), workspace_id: target.workspaceId.trim() };
  const issue = () => act('Sending the instruction', async () => {
    const out = await api.issueDeviceInstruction({ ...t, device_id: target.deviceId.trim(), action, minutes, reason: reason.trim() });
    setNotice(out.state === 'pending_approval' ? 'Requested. Waiting for a second administrator to approve it within 30 minutes.' : `Signed. The watch will collect it; it ends ${when(out.expires_at)}.`);
    setReason('');
  });
  const approve = (row: Instruction) => act('Approving', async () => { await api.approveDeviceInstruction(row.id, t); setNotice('Approved and signed. The watch will collect it.'); });
  const revoke = (row: Instruction, why: string) => act('Revoking', async () => { await api.revokeDeviceInstruction(row.id, t, why); setNotice('Revoked. The watch is told not to carry it out.'); });

  const liveNow = liveWords(live, clock);
  const problems = targetProblems(target);
  const formProblems = issueProblems(action, minutes, reason);
  const paired = devices.filter((d) => d.status === 'paired');

  return (
    <div data-testid="device-admin">
      <h2>Deployment administration</h2>
      <p className="muted">Signed in as {session.username}. Three actions, one watch, at most {MAX_MINUTES} minutes; everything here is audited.</p>
      <p className="muted" data-testid="step-up-state">{stepUpWords(me)}</p>

      <section className="card" aria-labelledby="target-title">
        <h3 id="target-title">Which watch</h3>
        <div className="row">
          <label>Tenant id <input value={target.tenantId} onChange={(e) => setTarget({ ...target, tenantId: e.target.value })} spellCheck={false} aria-invalid={!!target.tenantId && !isUuid(target.tenantId)} /></label>
          <label>Workspace id <input value={target.workspaceId} onChange={(e) => setTarget({ ...target, workspaceId: e.target.value })} spellCheck={false} aria-invalid={!!target.workspaceId && !isUuid(target.workspaceId)} /></label>
          <label>Watch id <input value={target.deviceId} onChange={(e) => setTarget({ ...target, deviceId: e.target.value })} spellCheck={false} aria-invalid={!!target.deviceId && !isUuid(target.deviceId)} /></label>
        </div>
        <div className="row">
          <label>Or choose a person <select value={personId} onChange={(e) => setPersonId(e.target.value)} disabled={people.length === 0}>
            <option value="">{people.length === 0 ? 'No people to choose from' : 'Choose a person'}</option>
            {people.map((p) => <option key={p.id} value={p.id}>{p.display_name}</option>)}
          </select></label>
          {personId ? (
            <label>Their watch <select value="" onChange={(e) => { if (e.target.value) setTarget({ ...target, deviceId: e.target.value }); }} disabled={paired.length === 0} data-testid="watch-picker">
              <option value="">{paired.length === 0 ? (devices.length ? 'No paired watch' : 'No watch') : 'Choose a paired watch'}</option>
              {paired.map((d) => <option key={d.id} value={d.id}>{d.platform} · battery {d.battery_percent ?? '?'} % · synced {when(d.last_sync_at) || 'never'}</option>)}
            </select></label>
          ) : null}
        </div>
        {peopleError ? <p className="error" data-testid="people-error">{peopleError}</p> : null}
        {devicesError ? <p className="error" data-testid="devices-error">{devicesError}</p> : null}
        {!complete ? <p className="muted" data-testid="target-problems">{problems.join(' ')}</p> : null}
      </section>

      {complete ? (
        <section className="card" aria-labelledby="history-title">
          <h3 id="history-title">Instructions sent to this watch</h3>
          <p data-testid="live" role="status" aria-live="polite" className={liveNow.state === 'live' ? 'muted' : 'error'} style={{ margin: '0 0 6px', fontSize: 13 }}>{liveNow.words}</p>
          {historyError ? <p className="error" role="alert" data-testid="history-error">{historyError}</p> : null}
          {rows ? <InstructionTable rows={rows} busy={busy} onApprove={approve} onRevoke={revoke} /> : null}
        </section>
      ) : null}

      {pending ? <StepUpPanel mfaEnrolled={me.mfa_enrolled} busy={busy} error={stepUpError} actionLabel={pending.label.toLowerCase()} onConfirm={confirmStepUp} onCancel={() => { setPending(null); setStepUpError(''); setActionError('Nothing was sent: you did not confirm who you are.'); }} /> : null}

      {notice ? <p role="status" data-testid="notice" className="card" style={{ borderColor: 'var(--ok)' }}>{notice}</p> : null}
      {actionError ? <p className="error" role="alert" data-testid="action-error">{actionError}</p> : null}

      <section className="card" aria-labelledby="issue-title">
        <h3 id="issue-title">Send an instruction</h3>
        <div className="row">
          <label>Action <select value={action} onChange={(e) => setAction(e.target.value as DeviceAction)}>
            {DEVICE_ACTIONS.map((a) => <option key={a} value={a}>{ACTION_WORDS[a]}</option>)}
          </select></label>
          <label>Minutes <input type="number" min={MIN_MINUTES} max={MAX_MINUTES} value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} /></label>
          <label style={{ flex: 1 }}>Reason <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why this watch needs it" /></label>
        </div>
        <p className="muted" data-testid="action-help">{ACTION_HELP[action]}</p>
        {action === 'FACTORY_RESET' ? <p data-testid="two-person"><strong>Two people:</strong> this wipes the watch, so a different deployment administrator must approve the request within 30 minutes. It is not signed until then.</p> : null}
        {formProblems.length > 0 && reason ? <p className="muted" data-testid="form-problems">{formProblems.join(' ')}</p> : null}
        <button className={action === 'FACTORY_RESET' ? 'sos' : undefined} disabled={busy || !complete || formProblems.length > 0} onClick={issue}>
          {action === 'FACTORY_RESET' ? 'Request a factory reset' : 'Send to the watch'}
        </button>
      </section>
    </div>
  );
}
