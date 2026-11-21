import { useState } from 'react';
import type { AccessNotes, Alert, Person } from '@/lib/types';
import { alertWords, timeAgo, whereabouts } from '@/lib/view';

export function AlertCard({ alert, personName, person, onAcknowledge, onResolve, onAccessNotes, busy }: {
  alert: Alert; personName: string; busy: boolean; person?: Person | null;
  onAcknowledge: (a: Alert) => void; onResolve: (a: Alert) => void;
  onAccessNotes?: (a: Alert) => Promise<AccessNotes>;
}) {
  const open = alert.status === 'open' || alert.status === 'escalating';
  const summary = typeof alert.details?.summary === 'string' ? (alert.details.summary as string) : '';
  const [notes, setNotes] = useState<AccessNotes | 'refused' | null>(null);
  const where = alert.severity !== 'notice' ? whereabouts(alert, person) : null;
  return (
    <div className="card" data-testid={`alert-${alert.id}`}>
      <div className="row">
        <span className={`pill ${alert.severity}`}>{alert.severity.toUpperCase()}</span>
        <strong>{alertWords(alert)}</strong>
        <span className="muted">{personName} · {timeAgo(alert.opened_at)} · tier {alert.current_tier} · {alert.status}</span>
      </div>
      {summary ? <p className="muted" style={{ margin: '6px 0' }}>{summary}</p> : null}
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
          <p className="muted">Access notes are shown only to tier 1 and 2 recipients while the alert is open.</p>
        ) : (
          <p data-testid="access-notes"><strong>Access notes:</strong> {notes.access_notes || 'none recorded'} <span className="muted">(this view is in the access log)</span></p>
        )
      ) : null}
      {alert.details?.show_call_999_screen === true && alert.status !== 'resolved' ? (
        <p className="error">Nobody has acknowledged this alert. The family app is showing &quot;call 999 now&quot;.</p>
      ) : null}
      <div className="row">
        {open && alert.severity !== 'notice' ? <button className="ok" disabled={busy} onClick={() => onAcknowledge(alert)}>I&apos;m on my way</button> : null}
        {alert.status !== 'resolved' ? <button className="grey" disabled={busy} onClick={() => onResolve(alert)}>Resolve</button> : null}
      </div>
    </div>
  );
}
