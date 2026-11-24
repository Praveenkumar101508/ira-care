import { useState } from 'react';
import { OUTCOME_WORDS, type AccessNotes, type Alert, type AlertOutcome, type Person } from '@/lib/types';
import { alertWords, circleWords, reachWords, timeAgo, whereabouts } from '@/lib/view';

export function AlertCard({ alert, personName, person, onAcknowledge, onArrived, onResolve, onAccessNotes, busy }: {
  alert: Alert; personName: string; busy: boolean; person?: Person | null;
  onAcknowledge: (a: Alert) => void; onResolve: (a: Alert, outcome: AlertOutcome, resolution: string) => void;
  /** The responder has reached the person: the watch then says "[Name] has arrived". */
  onArrived?: (a: Alert) => void;
  onAccessNotes?: (a: Alert) => Promise<AccessNotes>;
}) {
  const open = alert.status === 'open' || alert.status === 'escalating';
  const arrived = (alert.acknowledgements ?? []).some((a) => a.kind === 'arrived' || a.kind === 'checked_in_person');
  const summary = typeof alert.details?.summary === 'string' ? (alert.details.summary as string) : '';
  const [notes, setNotes] = useState<AccessNotes | 'refused' | null>(null);
  const [closing, setClosing] = useState(false);
  const [outcome, setOutcome] = useState<AlertOutcome>('all_well');
  const [resolution, setResolution] = useState('');
  const followUpDue = alert.status === 'acknowledged' && alert.follow_up_due_at ? new Date(alert.follow_up_due_at) : null;
  const where = alert.severity !== 'notice' ? whereabouts(alert, person) : null;
  return (
    <article className="card" data-testid={`alert-${alert.id}`} aria-label={`${alertWords(alert)}, ${personName}, ${alert.severity}, ${alert.status}`}>
      <div className="row">
        <span className={`pill ${alert.severity}`} aria-hidden="true">{alert.severity.toUpperCase()}</span>
        <strong>{alertWords(alert)}</strong>
        <span className="muted">{personName} · {timeAgo(alert.opened_at)} · {reachWords(alert.current_tier)} · {alert.status}</span>
      </div>
      {summary ? <p className="muted" style={{ margin: '6px 0' }}>{summary}</p> : null}
      {(alert.attempts ?? []).some((a) => a.channel === 'push') ? (
        <ul className="muted" data-testid="delivery" style={{ margin: '6px 0', paddingLeft: 18 }}>
          {(alert.attempts ?? []).filter((a) => a.channel === 'push').map((a) => (
            <li key={a.id}>{circleWords(a.tier)} phone: {a.delivery ?? a.outcome}{a.delivered_at ? ` (${timeAgo(a.delivered_at)})` : ''}</li>
          ))}
        </ul>
      ) : null}
      {where ? (
        <p data-testid="whereabouts" style={{ margin: '6px 0' }}>
          <strong>Where:</strong> {where.line}
          {where.mapLink ? <> · <a href={where.mapLink} target="_blank" rel="noreferrer">open map</a></> : null}
          {person?.emergency_summary ? <><br /><strong>Emergency information:</strong> {person.emergency_summary}</> : null}
        </p>
      ) : null}
      {where && alert.status !== 'resolved' && onAccessNotes ? (
        notes === null ? (
          <p><button className="grey" onClick={() => { onAccessNotes(alert).then(setNotes).catch(() => setNotes('refused')); }}>Show access notes (key safe, door, pets)</button></p>
        ) : notes === 'refused' ? (
          <p className="muted">Access notes are shown only to the people called first and second while the alert is open.</p>
        ) : (
          <p data-testid="access-notes"><strong>Access notes:</strong> {notes.access_notes || 'none recorded'} <span className="muted">(this view is in the access log)</span></p>
        )
      ) : null}
      {alert.details?.show_call_999_screen === true && alert.status !== 'resolved' ? (
        <p className="error">Nobody has acknowledged this alert. The family app is showing &quot;call 999 now&quot;.</p>
      ) : null}
      <div className="row">
        {open && alert.severity !== 'notice' ? <button className="ok" disabled={busy} onClick={() => onAcknowledge(alert)}>I&apos;m on my way</button> : null}
        {alert.status === 'acknowledged' && !arrived && onArrived ? <button className="ok" disabled={busy} onClick={() => onArrived(alert)}>I&apos;ve arrived</button> : null}
        {arrived && alert.status !== 'resolved' ? <span className="muted">Arrived · the watch has been told</span> : null}
        {alert.status !== 'resolved' && !closing ? <button className="grey" disabled={busy} onClick={() => setClosing(true)}>Record the outcome</button> : null}
        {alert.status === 'resolved' && alert.outcome ? <span className="muted">Outcome: {OUTCOME_WORDS[alert.outcome]}</span> : null}
        {followUpDue ? <span className="muted" data-testid="follow-up">{alert.follow_up_asked_at ? 'Responder asked; the alert is raised again if nobody arrives' : `Arrival or outcome expected by ${followUpDue.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`}{alert.re_escalations ? ` · raised again ${alert.re_escalations}×` : ''}</span> : null}
      </div>
      {closing ? (
        <div className="row" data-testid="outcome-form" style={{ marginTop: 8, gap: 8 }}>
          <label>What happened
            <select aria-label="Outcome" value={outcome} onChange={(e) => setOutcome(e.target.value as AlertOutcome)}>
              {(Object.keys(OUTCOME_WORDS) as AlertOutcome[]).map((k) => <option key={k} value={k}>{OUTCOME_WORDS[k]}</option>)}
            </select>
          </label>
          <input aria-label="Notes" placeholder="A line for the record" value={resolution} onChange={(e) => setResolution(e.target.value)} style={{ width: 260 }} />
          <button className="ok" disabled={busy || !resolution.trim()} onClick={() => { onResolve(alert, outcome, resolution.trim()); setClosing(false); }}>Close the alert</button>
          <button className="grey" onClick={() => setClosing(false)}>Cancel</button>
        </div>
      ) : null}
    </article>
  );
}
