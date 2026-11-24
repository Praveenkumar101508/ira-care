'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import { canManage, useApi } from '@/components/useApi';
import { ApiError } from '@/lib/api';
import type { Alert, Overview, Person } from '@/lib/types';
import { alertWords, timeAgo, worstOpenAlert } from '@/lib/view';

/** The live overview: who needs attention now, visits due or overdue, alerts awaiting a response, stale watches. Refreshes every 30 seconds. */
export default function OverviewPage() {
  const { api, session, ready } = useApi();
  const [people, setPeople] = useState<Person[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [ov, setOv] = useState<Overview | null>(null);
  const [error, setError] = useState('');
  const [name, setName] = useState('');
  const [preferred, setPreferred] = useState('');
  const [at, setAt] = useState<Date | null>(null);

  const load = useCallback(async () => {
    if (!api) return;
    try { const [p, a, o] = await Promise.all([api.people(), api.alerts(true), api.overview()]); setPeople(p); setAlerts(a); setOv(o); setAt(new Date()); setError(''); }
    catch (e) { if (e instanceof ApiError && e.status === 401) window.location.href = '/login'; else setError(e instanceof ApiError ? e.message : 'Could not load.'); }
  }, [api]);

  useEffect(() => { void load(); const t = setInterval(() => { void load(); }, 30_000); return () => clearInterval(t); }, [load]);

  if (!ready) return null;
  const open = alerts.filter((a) => a.status === 'open' || a.status === 'escalating');
  return (
    <>
      <h2>Overview</h2>
      <p className="muted">{people.length} people · {open.length} alerts waiting for someone · {alerts.length - open.length} acknowledged{at ? ` · live, updated ${timeAgo(at.toISOString())}` : ''}</p>
      {error ? <p className="error">{error}</p> : null}
      {ov ? (
        <div className="grid" data-testid="overview">
          <section className="card" aria-labelledby="attention">
            <strong id="attention">Needs attention now</strong>
            {ov.attention.length === 0 ? <p className="muted">Nobody, right now.</p> : (
              <ul>{ov.attention.map((x) => <li key={x.person_id}><Link href={`/people/${x.person_id}`}>{x.person_name}</Link>: {x.reasons.join('; ')}</li>)}</ul>
            )}
          </section>
          <section className="card" aria-labelledby="visits">
            <strong id="visits">Visits due or overdue</strong>
            {ov.visits.length === 0 ? <p className="muted">None in the next two hours.</p> : (
              <ul>{ov.visits.map((v) => (
                <li key={v.id}><span className={`pill ${v.state === 'overdue' ? 'urgent' : v.state === 'in_progress' ? 'ok' : 'grey'}`}>{v.state === 'overdue' ? (v.status === 'missed' ? 'did not happen' : 'late') : v.state === 'in_progress' ? 'in progress' : 'due'}</span>{' '}
                  <Link href={`/people/${v.person_id}?tab=visits`}>{v.person_name}</Link> · {v.carer_name || 'carer not chosen'} · {v.planned_start.slice(11, 16)} to {v.planned_end.slice(11, 16)}</li>
              ))}</ul>
            )}
          </section>
          <section className="card" aria-labelledby="awaiting">
            <strong id="awaiting">Alerts awaiting a response</strong>
            {ov.alerts.length === 0 ? <p className="muted">None.</p> : (
              <ul>{ov.alerts.map((a) => (
                <li key={a.id}><span className={`pill ${a.severity}`}>{a.severity.toUpperCase()}</span>{' '}
                  <Link href={`/people/${a.person_id}?tab=alerts`}>{a.person_name}</Link> · {alertWords({ kind: a.kind } as Alert)} · {timeAgo(a.opened_at)} · tier {a.current_tier}
                  {a.awaiting_response ? <span className="error"> · nobody has responded</span> : a.arrived ? <span className="muted"> · {a.responder || 'someone'} has arrived</span> : a.follow_up_overdue ? <span className="error"> · {a.responder || 'the responder'} said they were coming and has not arrived</span> : <span className="muted"> · {a.responder || 'someone'} is on the way</span>}
                </li>
              ))}</ul>
            )}
          </section>
          <section className="card" aria-labelledby="stale">
            <strong id="stale">Watches to check</strong>
            {ov.stale_watches.length === 0 ? <p className="muted">Every watch has synced recently.</p> : (
              <ul>{ov.stale_watches.map((w) => <li key={w.device_id}><Link href={`/people/${w.person_id}?tab=health`}>{w.person_name}</Link>: {w.reasons.join(', ')}</li>)}</ul>
            )}
          </section>
        </div>
      ) : null}
      <h3>People being looked after</h3>
      <div className="grid">
        {people.map((p) => {
          const worst = worstOpenAlert(alerts, p.id);
          return (
            <Link key={p.id} href={`/people/${p.id}`} className="card" data-testid={`person-${p.id}`}>
              <strong style={{ fontSize: 17 }}>{p.preferred_name || p.display_name}</strong>
              <div className="muted">{p.display_name} · {p.timezone}</div>
              <div style={{ marginTop: 8 }}>
                {worst ? <><span className={`pill ${worst.severity}`}>{worst.severity.toUpperCase()}</span> {alertWords(worst)} · {timeAgo(worst.opened_at)}</>
                  : <span className="pill ok">ALL WELL</span>}
              </div>
            </Link>
          );
        })}
      </div>
      {canManage(session) ? (
        <form className="card" onSubmit={async (e) => { e.preventDefault(); if (!api) return; await api.createPerson({ display_name: name, preferred_name: preferred, timezone: 'Europe/London' }); setName(''); setPreferred(''); await load(); }}>
          <strong>Add a person</strong>
          <div className="row" style={{ marginTop: 8 }}>
            <input placeholder="Full name" value={name} onChange={(e) => setName(e.target.value)} required aria-label="Full name" />
            <input placeholder="Preferred name" value={preferred} onChange={(e) => setPreferred(e.target.value)} aria-label="Preferred name" />
            <button type="submit" disabled={!name}>Add</button>
          </div>
        </form>
      ) : null}
    </>
  );
}
