'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import { canManage, useApi } from '@/components/useApi';
import { ApiError } from '@/lib/api';
import type { Alert, Person } from '@/lib/types';
import { alertWords, timeAgo, worstOpenAlert } from '@/lib/view';

export default function OverviewPage() {
  const { api, session, ready } = useApi();
  const [people, setPeople] = useState<Person[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [error, setError] = useState('');
  const [name, setName] = useState('');
  const [preferred, setPreferred] = useState('');

  const load = useCallback(async () => {
    if (!api) return;
    try { const [p, a] = await Promise.all([api.people(), api.alerts(true)]); setPeople(p); setAlerts(a); setError(''); }
    catch (e) { if (e instanceof ApiError && e.status === 401) window.location.href = '/login'; else setError(e instanceof ApiError ? e.message : 'Could not load.'); }
  }, [api]);

  useEffect(() => { void load(); const t = setInterval(() => { void load(); }, 30_000); return () => clearInterval(t); }, [load]);

  if (!ready) return null;
  const open = alerts.filter((a) => a.status === 'open' || a.status === 'escalating');
  return (
    <>
      <h2>People being looked after</h2>
      <p className="muted">{people.length} people · {open.length} open alerts · {alerts.length - open.length} acknowledged</p>
      {error ? <p className="error">{error}</p> : null}
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
            <input placeholder="Full name" value={name} onChange={(e) => setName(e.target.value)} required />
            <input placeholder="Preferred name" value={preferred} onChange={(e) => setPreferred(e.target.value)} />
            <button type="submit" disabled={!name}>Add</button>
          </div>
        </form>
      ) : null}
    </>
  );
}
