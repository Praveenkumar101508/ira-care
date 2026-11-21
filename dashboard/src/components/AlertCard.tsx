import type { Alert } from '@/lib/types';
import { alertWords, timeAgo } from '@/lib/view';

export function AlertCard({ alert, personName, onAcknowledge, onResolve, busy }: {
  alert: Alert; personName: string; busy: boolean;
  onAcknowledge: (a: Alert) => void; onResolve: (a: Alert) => void;
}) {
  const open = alert.status === 'open' || alert.status === 'escalating';
  const summary = typeof alert.details?.summary === 'string' ? (alert.details.summary as string) : '';
  return (
    <div className="card" data-testid={`alert-${alert.id}`}>
      <div className="row">
        <span className={`pill ${alert.severity}`}>{alert.severity.toUpperCase()}</span>
        <strong>{alertWords(alert)}</strong>
        <span className="muted">{personName} · {timeAgo(alert.opened_at)} · tier {alert.current_tier} · {alert.status}</span>
      </div>
      {summary ? <p className="muted" style={{ margin: '6px 0' }}>{summary}</p> : null}
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
