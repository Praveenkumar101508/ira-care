'use client';

import { useCallback, useEffect, useState } from 'react';
import { AlertCard } from '@/components/AlertCard';
import { useApi } from '@/components/useApi';
import { ApiError } from '@/lib/api';
import type { Alert, Person } from '@/lib/types';
import { sortAlerts } from '@/lib/view';

export default function AlertsPage() {
  const { api, ready } = useApi();
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [people, setPeople] = useState<Person[]>([]);
  const [openOnly, setOpenOnly] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    if (!api) return;
    try { const [a, p] = await Promise.all([api.alerts(openOnly), api.people(true)]); setAlerts(a); setPeople(p); setError(''); }
    catch (e) { setError(e instanceof ApiError ? e.message : 'Could not load.'); }
  }, [api, openOnly]);
  useEffect(() => { void load(); const t = setInterval(() => { void load(); }, 30_000); return () => clearInterval(t); }, [load]);

  if (!ready) return null;
  const nameOf = (id: string) => people.find((p) => p.id === id)?.display_name ?? 'unknown person';
  const act = async (fn: () => Promise<unknown>) => { setBusy(true); try { await fn(); await load(); } catch (e) { setError(e instanceof ApiError ? e.message : 'Failed.'); } finally { setBusy(false); } };
  return (
    <>
      <h2>Alerts</h2>
      <p className="row"><label><input type="checkbox" checked={openOnly} onChange={(e) => setOpenOnly(e.target.checked)} /> open and escalating only</label></p>
      {error ? <p className="error">{error}</p> : null}
      {sortAlerts(alerts).map((a) => (
        <AlertCard key={a.id} alert={a} personName={nameOf(a.person_id)} person={people.find((p) => p.id === a.person_id)} busy={busy}
          onAccessNotes={(x) => api!.accessNotes(x.id)}
          onAcknowledge={(x) => { void act(() => api!.acknowledge(x.id, 'from the dashboard')); }}
          onResolve={(x) => { const r = window.prompt('What happened?', 'Checked; all well.'); if (r) void act(() => api!.resolve(x.id, r)); }} />
      ))}
      {alerts.length === 0 ? <p className="muted">No alerts.</p> : null}
    </>
  );
}
