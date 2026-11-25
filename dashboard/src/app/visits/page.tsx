'use client';

import { useCallback, useEffect, useState } from 'react';
import { VisitPlan } from '@/components/Team';
import { useApi } from '@/components/useApi';
import { ApiError } from '@/lib/api';
import { may } from '@/lib/carePlan';
import type { CircleMember, Permissions, Person, PlannedVisit } from '@/lib/types';

export default function VisitPlanPage() {
  const { api, ready } = useApi();
  const [perms, setPerms] = useState<Permissions | null>(null);
  const [visits, setVisits] = useState<PlannedVisit[]>([]);
  const [people, setPeople] = useState<Person[]>([]);
  const [carers, setCarers] = useState<CircleMember[]>([]);
  const [form, setForm] = useState({ person: '', carer: '', start: '', hours: '1', purpose: '' });
  const [error, setError] = useState('');
  const [saved, setSaved] = useState('');
  const load = useCallback(async () => {
    if (!api) return;
    try {
      const [p, v, ppl] = await Promise.all([api.permissions(), api.visitPlan(), api.people()]);
      setPerms(p); setVisits(v); setPeople(ppl); setError('');
    } catch (e) { setError(e instanceof ApiError ? e.message : 'Could not load.'); }
  }, [api]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!api || !form.person) { setCarers([]); return; }
    api.circle(form.person).then((m) => setCarers(m.filter((x) => x.role === 'carer' && x.active))).catch(() => setCarers([]));
  }, [api, form.person]);
  if (!ready) return null;
  const plan = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!api || !form.person || !form.start) { setError('Choose a person and a start time.'); return; }
    const start = new Date(form.start);
    const end = new Date(start.getTime() + Math.max(0.25, Number(form.hours) || 1) * 3_600_000);
    const carer = carers.find((c) => c.id === form.carer);
    try {
      await api.planVisit(form.person, { planned_start: start.toISOString(), planned_end: end.toISOString(), purpose: form.purpose,
        carer_member_id: carer?.id ?? null, carer_name: carer?.display_name ?? '' });
      setSaved('Visit planned.'); setForm({ ...form, start: '', purpose: '' }); await load();
    } catch (err) { setError(err instanceof ApiError ? err.message : 'Could not plan the visit.'); }
  };
  return (
    <>
      <h2>Visit plan</h2>
      <p className="muted">{perms?.sees_everyone ? 'Every visit in the next seven days, for everyone the organisation cares for.' : 'Visits in the next seven days for the people you look after.'}</p>
      {error ? <p className="error">{error}</p> : null}
      {saved ? <p role="status">{saved}</p> : null}
      {may(perms, 'record_visits') ? (
        <form onSubmit={plan} className="card" aria-label="Plan a visit">
          <label>Person <select value={form.person} onChange={(e) => setForm({ ...form, person: e.target.value, carer: '' })}>
            <option value="">Choose</option>{people.map((p) => <option key={p.id} value={p.id}>{p.preferred_name || p.display_name}</option>)}</select></label>
          <label>Carer <select value={form.carer} onChange={(e) => setForm({ ...form, carer: e.target.value })}>
            <option value="">Not yet assigned</option>{carers.map((c) => <option key={c.id} value={c.id}>{c.display_name}</option>)}</select></label>
          <label>Starts <input type="datetime-local" value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} /></label>
          <label>Hours <input type="number" min="0.25" step="0.25" value={form.hours} onChange={(e) => setForm({ ...form, hours: e.target.value })} /></label>
          <label>What for <input maxLength={200} value={form.purpose} onChange={(e) => setForm({ ...form, purpose: e.target.value })} /></label>
          <button type="submit">Plan the visit</button>
        </form>
      ) : null}
      <VisitPlan visits={visits} />
    </>
  );
}
