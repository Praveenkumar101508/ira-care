'use client';

import { NotMeasured } from '@/components/NotMeasured';
import { demoPagesEnabled } from '@/lib/demoPages';
import { useParams, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCard } from '@/components/AlertCard';
import { Sparkline } from '@/components/Sparkline';
import { useApi } from '@/components/useApi';
import { CarePlanSection } from '@/components/CarePlan';
import { may } from '@/lib/carePlan';
import { ApiError, type Reading } from '@/lib/api';
import type {
  AccessEntry, Alert, Checkin, CircleMember, ConsentList, Device, HomeUpdate, InvitationOut, Medication, Memory, MedicationEvent, Person, PersonStatus, Reminder, Report, Routine, SafetySettings, SearchResponse, Transcript, Visit, VisitOutcome, CommunicationProfile, Permissions, CarePlan,
} from '@/lib/types';
import { VISIT_OUTCOME_WORDS } from '@/lib/types';
import { VISIT_WORDS, byDay, checkinCounts, circleWords, freshness, headline, series, sortAlerts, timeAgo, timeline, visitLine, wristState } from '@/lib/view';
import { LiveGate, REFRESH_MS, liveWords, settleAll, type LiveStatus } from '@/lib/live';

/** What was looked at, in a reader's words; who looked, in their role's words rather than a raw identifier. */
const ACCESS_WORDS: Record<string, string> = { vitals: 'readings', checkins: 'check-ins', medication: 'medication', transcripts: 'transcripts', access_notes: 'entry instructions', reports: 'reports', memory: 'what IRA remembers', alerts: 'alerts' };
function accessWho(label: string): string {
  if (label === 'workspace_admin') return 'care manager';
  if (/^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(label)) return 'a member of the care team';
  return label.replace(/_/g, ' ');
}

const TABS = ['overview', 'profile', 'plan', 'health', 'care', 'timeline', 'alerts', 'ira', 'reports', 'search', 'access'] as const;
const TAB_WORDS: Record<Tab, string> = { overview: 'Overview', profile: 'Profile', plan: 'Care plan', health: 'Health and wellbeing', care: 'Visits and medication', timeline: 'Timeline', alerts: 'Alerts', ira: 'IRA', reports: 'Reports', search: 'Search', access: 'Access' };
type Tab = (typeof TABS)[number];


/** The readings the watch sends (care/capabilities.py); nothing else gets a reading card. */
const MEASURED = ['heart_rate', 'steps', 'movement', 'on_wrist', 'battery'];

export default function PersonPage() {
  const { id } = useParams<{ id: string }>();
  const search = useSearchParams();
  // An unknown or old tab name (a bookmark, an old link) shows the overview, never a blank page.
  const tab: Tab = (TABS as readonly string[]).includes(search.get('tab') ?? '') ? (search.get('tab') as Tab) : 'overview';
  const { api, ready } = useApi();
  // What this account may do, from the server (GET /care/me/permissions). Until it answers, or if
  // it cannot, nothing that changes the record is shown; the server checks every request anyway.
  const [perms, setPerms] = useState<Permissions | null>(null);
  const manage = !!perms?.can_manage;
  const recordVisits = may(perms, 'record_visits');

  const [status, setStatus] = useState<PersonStatus | null>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [checkins, setCheckins] = useState<{ checkins: Checkin[]; answers_included: boolean }>({ checkins: [], answers_included: false });
  const [vitals, setVitals] = useState<Reading[]>([]);
  const [routines, setRoutines] = useState<Routine[]>([]);
  const [meds, setMeds] = useState<Medication[]>([]);
  const [medEvents, setMedEvents] = useState<MedicationEvent[]>([]);
  const [circle, setCircle] = useState<CircleMember[]>([]);
  const [settings, setSettings] = useState<SafetySettings | null>(null);
  const [consents, setConsents] = useState<ConsentList | null>(null);
  const [memory, setMemory] = useState<Memory[]>([]);
  const [transcripts, setTranscripts] = useState<Transcript[] | 'refused' | null>(null);
  const [access, setAccess] = useState<AccessEntry[]>([]);
  const [reports, setReports] = useState<Report[]>([]);
  const [visits, setVisits] = useState<Visit[]>([]);
  const [carePlan, setCarePlan] = useState<CarePlan | null>(null);
  const [planHistory, setPlanHistory] = useState<CarePlan[]>([]);
  const [profile, setProfile] = useState<CommunicationProfile | null>(null);
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [entry, setEntry] = useState<{ access_notes: string } | 'refused' | null>(null);
  const [query, setQuery] = useState('');
  const [found, setFound] = useState<SearchResponse | null>(null);
  const [pairing, setPairing] = useState<Device | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [live, setLive] = useState<LiveStatus>({ lastOk: null, failures: 0 });
  const [ops, setOps] = useState<{ monitoring_paused: boolean; delivery?: { delivery_problem: boolean } } | null>(null);
  const [partial, setPartial] = useState<string[]>([]);
  const [clock, setClock] = useState(() => new Date());
  const gate = useRef(new LiveGate());

  // Another person: nothing of the last one may show here, and nothing still in flight for it may land.
  useEffect(() => {
    gate.current.next();
    setStatus(null); setAlerts([]); setCheckins({ checkins: [], answers_included: false }); setVitals([]); setRoutines([]);
    setMeds([]); setMedEvents([]); setCircle([]); setSettings(null); setConsents(null); setMemory([]); setTranscripts(null);
    setAccess([]); setReports([]); setVisits([]); setCarePlan(null); setPlanHistory([]); setProfile(null); setReminders([]); setEntry(null); setFound(null);
    setPairing(null); setError(''); setPartial([]); setLive({ lastOk: null, failures: 0 });
  }, [id]);

  const load = useCallback(async () => {
    if (!api) return;
    const ticket = gate.current.begin();
    if (ticket === null) return; // a load for this person is already running
    try {
      // What matters in an emergency first, on its own: the status and the alerts.
      const [s, a] = await Promise.all([api.status(id), api.alerts(false, id)]);
      if (!gate.current.current(ticket)) return;
      setStatus(s); setAlerts(a); setError('');
      // Whether the system can vouch for "all well" (care jobs alive, messages getting out). Unknown counts as not.
      // Never part of the record's own load: a health check that fails in any way only withholds "All well".
      Promise.resolve().then(() => api.opsStatus()).then((o) => { if (gate.current.current(ticket)) setOps(o); }).catch(() => { if (gate.current.current(ticket)) setOps(null); });
      // The rest side by side: one section failing is named, never blanks the record.
      const { values: v, failed } = await settleAll({
        checkins: () => api.checkins(id), vitals: () => api.vitals(id), routines: () => api.routines(id),
        medications: () => api.medications(id), medicationEvents: () => api.medicationEvents(id), circle: () => api.circle(id),
        settings: () => api.settings(id), consents: () => api.consents(id), visits: () => api.visits(id),
        ...(tab === 'ira' || tab === 'overview' ? { memory: () => api.memory(id), profile: () => api.profile(id), reminders: () => api.reminders(id) } : {}),
        ...(tab === 'access' && manage ? { access: () => api.accessLog(id) } : {}),
        ...(tab === 'reports' ? { reports: () => api.reports(id) } : {}),
        ...(tab === 'plan' ? { plan: () => api.carePlan(id), planHistory: () => api.carePlanHistory(id) } : {}),
      });
      if (!gate.current.current(ticket)) return;
      if (v.checkins) setCheckins(v.checkins); if (v.vitals) setVitals(v.vitals); if (v.routines) setRoutines(v.routines);
      if (v.medications) setMeds(v.medications); if (v.medicationEvents) setMedEvents(v.medicationEvents); if (v.circle) setCircle(v.circle);
      if (v.settings) setSettings(v.settings); if (v.consents) setConsents(v.consents); if (v.visits) setVisits(v.visits);
      if (v.memory) setMemory(v.memory); if (v.profile) setProfile(v.profile); if (v.reminders) setReminders(v.reminders);
      if (v.access) setAccess(v.access); if (v.reports) setReports(v.reports);
      if (tab === 'plan' && 'plan' in v) setCarePlan(v.plan ?? null); if (v.planHistory) setPlanHistory(v.planHistory);
      if (tab === 'ira' || tab === 'overview') {
        try {
          const t = await api.transcripts(id);
          if (gate.current.current(ticket)) setTranscripts(t);
        } catch (e) {
          if (gate.current.current(ticket)) setTranscripts(e instanceof ApiError && e.status === 403 ? 'refused' : null);
        }
      }
      setPartial(failed.map(String));
      setLive({ lastOk: new Date(), failures: 0 });
    } catch (e) {
      if (!gate.current.current(ticket)) return;
      if (e instanceof ApiError && e.status === 401) { window.location.href = '/login'; return; }
      setLive((l) => ({ ...l, failures: l.failures + 1 }));
      setError(e instanceof ApiError ? e.message : 'Could not load.');
    } finally {
      gate.current.end(ticket);
    }
  }, [api, id, tab, manage]);
  // Permissions belong to the account, not the person: asked once per sign-in.
  useEffect(() => {
    if (!api) return;
    let gone = false;
    (async () => { try { const p = await api.permissions(); if (!gone) setPerms(p); } catch { if (!gone) setPerms(null); } })();
    return () => { gone = true; };
  }, [api]);
  // Live: every REFRESH_MS, and at once when the page comes back into view. Editing a form never
  // pauses it: each form keeps its own draft, so a refresh cannot overwrite what is being typed.
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    const onVisible = () => { if (document.visibilityState === 'visible') void load(); };
    document.addEventListener('visibilitychange', onVisible);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', onVisible); };
  }, [load]);
  useEffect(() => { const t = window.setInterval(() => setClock(new Date()), 5_000); return () => window.clearInterval(t); }, []);

  const liveNow = liveWords(live, clock);
  if (!ready || !status) return error ? <p className="error" role="alert">{error} <span className="muted">{liveNow.words}</span></p> : <p className="muted" aria-live="polite">Loading this record…</p>;
  const person = status.person;
  const name = person.preferred_name || person.display_name;
  const act = async (fn: () => Promise<unknown>) => { setBusy(true); try { await fn(); await load(); } catch (e) { setError(e instanceof ApiError ? e.message : 'Failed.'); } finally { setBusy(false); } };
  const counts = checkinCounts(checkins.checkins);
  const device = status.devices[0];

  return (
    <>
      <h2>{name} <span className="muted" style={{ fontSize: 14 }}>{person.display_name} · {person.timezone} · {person.status}</span></h2>
      <p data-testid="live" role="status" aria-live="polite" className={liveNow.state === 'live' ? 'muted' : 'error'} style={{ margin: '0 0 6px', fontSize: 13 }}>
        {liveNow.words}{partial.length ? ` Some sections could not be loaded (${partial.join(', ')}); what they show may be missing.` : ''}
      </p>
      <p data-testid="headline" style={{ fontSize: 18, margin: '0 0 8px' }}>{headline(status, visits.find((v) => v.status === 'arrived') ?? visits.filter((v) => (v.status === 'planned' || v.status === 'late') && new Date(v.planned_end) > new Date()).sort((x, y) => x.planned_start.localeCompare(y.planned_start))[0] ?? null, clock,
        { dataStale: liveNow.state !== 'live', monitoringPaused: ops ? ops.monitoring_paused : true, deliveryProblem: ops ? !!ops.delivery?.delivery_problem : true })}</p>
      <div className="card row">
        <span><strong>Watch:</strong> {device ? `${device.status}, battery ${device.battery_percent ?? '?'} %, ${wristState(device).words}, synced ${timeAgo(device.last_sync_at)}${device.background_sensor_permission === false ? ', sensor permission OFF' : ''}` : 'none paired'}</span>
        <span><strong>Check-ins (last 200):</strong> {counts.answered} answered, {counts.missed} missed, {counts.skippedQuiet} at night</span>
        <span><strong>Open alerts:</strong> {status.open_alerts.length}</span>
      </div>
      <nav className="tabs" aria-label="Sections of this person's record">
        {TABS.filter((t) => t !== 'access' || manage).map((t) => <Link key={t} href={`/people/${id}?tab=${t}`} className={t === tab ? 'on' : ''} aria-current={t === tab ? 'page' : undefined}>{TAB_WORDS[t]}</Link>)}
      </nav>
      {error ? <p className="error">{error}</p> : null}

      {tab === 'overview' && (
        <div className="grid" data-testid="overview-tab">
          <div className="card"><strong>The watch</strong>
            <div>{device ? `${device.status}, battery ${device.battery_percent ?? '?'} %, ${wristState(device).words}` : 'None paired'}</div>
            <div className="muted">Last sync {timeAgo(device?.last_sync_at)}{device?.background_sensor_permission === false ? ' · sensor permission OFF' : ''}</div>
          </div>
          <div className="card"><strong>Latest readings</strong>
            {status.latest_readings.length === 0 ? <div className="muted">No readings yet.</div> : status.latest_readings.map((r) => {
              const f = freshness(r as Reading & { received_at?: string | null }, Math.max(30, (settings?.reading_upload_interval_s ?? 60) / 60 * 3));
              return <div key={r.kind}><span className={`pill ${f.state === 'fresh' ? 'ok' : f.state === 'stale' ? 'urgent' : 'grey'}`}>{f.state}</span> {r.kind.replace('_', ' ')} {Math.round(r.value)}{r.unit ? ` ${r.unit}` : ''} <span className="muted">· {f.words}</span></div>;
            })}
          </div>
          <div className="card"><strong>Last check-in</strong>
            {status.last_checkin ? <div>{status.last_checkin.question_kind} · {status.last_checkin.status} · {timeAgo(status.last_checkin.scheduled_at)}{status.last_checkin.answer_summary ? <div className="muted">{status.last_checkin.answer_summary}</div> : null}</div> : <div className="muted">None yet.</div>}
            <div className="muted">Last 200: {counts.answered} answered, {counts.missed} missed, {counts.skippedQuiet} at night</div>
          </div>
          <div className="card"><strong>Visits</strong>
            {(() => { const v = visits.find((x) => x.status === 'arrived') ?? visits.filter((x) => (x.status === 'planned' || x.status === 'late') && new Date(x.planned_end) > new Date()).sort((x, y) => x.planned_start.localeCompare(y.planned_start))[0]; return v ? <div>{VISIT_WORDS[v.status]}: {visitLine(v)}</div> : <div className="muted">No visit planned.</div>; })()}
            <div className="muted">{visits.filter((v) => v.status === 'departed').length} done, {visits.filter((v) => v.status === 'missed').length} did not happen (loaded)</div>
          </div>
          <div className="card"><strong>Alerts</strong>
            {status.open_alerts.length === 0 ? <div className="muted">None open.</div> : status.open_alerts.map((a) => <div key={a.id}><span className={`pill ${a.severity}`}>{a.severity.toUpperCase()}</span> {a.kind.replace(/_/g, ' ')} · {a.status} · {timeAgo(a.opened_at)}</div>)}
            <Link href={`/people/${id}?tab=alerts`}>All alerts, with delivery and outcomes</Link>
          </div>
          <div className="card"><strong>What IRA remembers today</strong>
            {memory[0] ? <div style={{ whiteSpace: 'pre-line' }}>{memory[0].summary}</div> : <div className="muted">Nothing yet. Summaries by IRA, never the person&apos;s words.</div>}
            {visits.filter((v) => v.status === 'departed' && v.handover)[0] ? <div className="muted">Last handover: {visits.filter((v) => v.status === 'departed' && v.handover)[0].handover}</div> : null}
          </div>
        </div>
      )}

      {tab === 'alerts' && (
        <>
          <p className="muted">Every alert for {name}, newest first: who was told and whether the phone received it, the follow-up deadline, and the outcome.</p>
          {sortAlerts(alerts).map((a) => (
            <AlertCard key={a.id} alert={a} personName={name} person={person} busy={busy}
              onAccessNotes={(x) => api!.accessNotes(x.id)}
              onAcknowledge={(x) => { void act(() => api!.acknowledge(x.id, 'from the dashboard')); }}
              onArrived={(x) => { void act(() => api!.acknowledge(x.id, 'from the dashboard', 'arrived')); }}
              onResolve={(x, outcome, resolution) => { void act(() => api!.resolve(x.id, resolution, outcome)); }} />
          ))}
          {alerts.length === 0 ? <p className="muted">No alerts recorded.</p> : null}
        </>
      )}

      {tab === 'profile' && (
        <div className="card">
          <strong>Entry instructions (key safe, door, pets)</strong>
          <p className="muted">Shown here to managers only, and every reading is written to the access log. During an alert, the people called see them on the alert itself.</p>
          {manage ? (entry === null ? <button className="grey" disabled={busy} onClick={() => { api!.profileAccessNotes(id).then((n) => setEntry({ access_notes: n.access_notes })).catch(() => setEntry('refused')); }}>Show entry instructions</button>
            : entry === 'refused' ? <p className="muted">Not available to this account.</p>
            : <p data-testid="entry-instructions">{entry.access_notes || 'None recorded.'} <button className="grey" onClick={() => setEntry(null)}>Hide</button></p>) : <p className="muted">Only a manager may read them here.</p>}
        </div>
      )}

      {tab === 'timeline' && (
        <>
          {sortAlerts(alerts).filter((a) => a.status !== 'resolved').map((a) => (
            <AlertCard key={a.id} alert={a} personName={name} person={person} busy={busy}
              onAccessNotes={(x) => api!.accessNotes(x.id)}
              onAcknowledge={(x) => { void act(() => api!.acknowledge(x.id, 'from the dashboard')); }}
              onResolve={(x, outcome, resolution) => { void act(() => api!.resolve(x.id, resolution, outcome)); }} />
          ))}
          {byDay(timeline(checkins.checkins, visits, alerts), 'at').map(({ day, items }) => (
            <div className="card" key={day}>
              <strong>{day}</strong>
              <table><tbody>
                {items.map((e) => (
                  <tr key={e.id}>
                    <td className="muted">{e.at.slice(11, 16)}</td>
                    <td><span className="pill grey" title="Where this line came from">{e.source}</span></td>
                    <td>{e.title}</td>
                    <td><span className={`pill ${e.tone}`}>{e.detail.split(':')[0]}</span></td>
                    <td>{e.detail.includes(':') ? e.detail.slice(e.detail.indexOf(':') + 1).trim() : ''}
                      {e.kind === 'checkin' && checkins.answers_included ? (() => { const c = checkins.checkins.find((x) => `c-${x.id}` === e.id); return c?.answer_text ? <em className="muted"> “{c.answer_text}”</em> : null; })() : null}
                    </td>
                  </tr>
                ))}
              </tbody></table>
            </div>
          ))}
          {!checkins.answers_included ? <p className="muted">Summaries only: the person&apos;s own words need their recorded consent for your role.</p> : null}
        </>
      )}

      {tab === 'care' && (
        <>
          {visits.filter((v) => v.status !== 'departed' && v.status !== 'cancelled').map((v) => (
            <div className="card" key={v.id}>
              <strong>{VISIT_WORDS[v.status]}</strong> <span className="muted">{v.planned_start.slice(0, 10)}</span>
              <div>{visitLine(v)}</div>
              {v.tasks.length ? <ul>{v.tasks.map((t) => <li key={t.id}>{t.done ? '☑' : '☐'} {t.title}</li>)}</ul> : null}
              {recordVisits ? <div className="row">
                {v.status !== 'arrived' ? <button disabled={busy} onClick={() => act(() => api!.arrive(v.id))}>Carer has arrived</button> : null}
                {v.status === 'arrived' ? <DepartForm busy={busy} tasks={v.tasks.map((t) => t.id)} onDepart={(b) => act(() => api!.depart(v.id, b))} /> : null}
                {v.status !== 'arrived' ? <button disabled={busy} onClick={() => act(() => api!.cancelVisit(v.id, 'Cancelled from the dashboard'))}>Cancel</button> : null}
              </div> : null}
            </div>
          ))}
          {recordVisits ? <PlanVisit circle={circle} onPlan={(b) => act(() => api!.planVisit(id, b))} busy={busy} /> : null}
          <div className="card">
            <strong>Past visits</strong>
            <table><thead><tr><th>When</th><th>Carer</th><th>Outcome</th><th>Tasks</th><th>Handover</th></tr></thead><tbody>
              {visits.filter((v) => v.status === 'departed' || v.status === 'cancelled' || v.status === 'missed').map((v) => (
                <tr key={v.id}><td className="muted">{v.planned_start.slice(0, 16).replace('T', ' ')}</td><td>{v.carer_name || '-'}</td>
                  <td>{v.status === 'departed' ? VISIT_OUTCOME_WORDS[v.outcome as Exclude<VisitOutcome, ''>] ?? v.outcome : VISIT_WORDS[v.status]}</td>
                  <td>{v.tasks.filter((t) => t.done).length}/{v.tasks.length}</td><td>{v.handover}</td></tr>
              ))}
            </tbody></table>
          </div>
        </>
      )}

      {tab === 'plan' && (
        <>
          <p className="muted" data-testid="staff-role">{perms ? `You are signed in as ${perms.label}. ${perms.who}` : 'Checking what this account may do…'}</p>
          <CarePlanSection plan={carePlan} history={planHistory} canEdit={may(perms, 'edit_care_plan')} busy={busy}
            onSave={async (d) => {
              // Unlike act(), a failure is passed back, so the form keeps what was typed (a 409 when
              // someone else saved first: reload, and the draft is still there to re-apply).
              setBusy(true);
              try { await api!.saveCarePlan(id, d); setError(''); await load(); }
              catch (e) { setError(e instanceof ApiError ? e.message : 'Failed.'); throw e; }
              finally { setBusy(false); }
            }} />
        </>
      )}

      {tab === 'health' && (
        <div className="grid">
          {MEASURED.map((k) => {
            const latest = status.latest_readings.find((r) => r.kind === k) as (Reading & { received_at?: string | null }) | undefined;
            const f = freshness(latest, Math.max(30, (settings?.reading_upload_interval_s ?? 60) / 60 * 3));
            return (
              <div className="card" key={k} data-testid={`reading-${k}`}>
                <div className="row"><strong>{k.replace('_', ' ')}</strong> <span className={`pill ${f.state === 'fresh' ? 'ok' : f.state === 'stale' ? 'urgent' : 'grey'}`}>{f.state}</span></div>
                <div>{latest ? `${Math.round(latest.value)}${latest.unit ? ` ${latest.unit}` : ''}` : 'Unavailable'} <span className="muted">· {f.words}</span></div>
                {series(vitals, k).length > 1 ? <Sparkline points={series(vitals, k)} label={k.replace('_', ' ')} /> : null}
              </div>
            );
          })}
          {settings ? <div className="card"><strong>Range set for this person</strong><div className="muted">Heart rate {settings.heart_rate_min ?? 'not set'} to {settings.heart_rate_max ?? 'not set'}. Set by a carer or clinician; there is no built-in range.</div></div> : null}
          {api ? <NotMeasured api={api} /> : null}
        </div>
      )}

      {tab === 'care' && (
        <div className="card">
          <table><thead><tr><th>Routine</th><th>Kind</th><th>Time</th><th>Days</th></tr></thead><tbody>
            {routines.map((r) => <tr key={r.id}><td>{r.name}</td><td>{r.kind}</td><td>{r.time_of_day?.slice(0, 5) ?? '-'}</td><td>{r.days_of_week.join(' ')}</td></tr>)}
          </tbody></table>
          {manage ? <AddRoutine onAdd={(b) => act(() => api!.addRoutine(id, b))} /> : null}
        </div>
      )}

      {tab === 'care' && (
        <>
          <div className="card">
            <table><thead><tr><th>Medication</th><th>Dose</th><th>Times</th></tr></thead><tbody>
              {meds.map((m) => <tr key={m.id}><td>{m.name}</td><td>{m.dose}</td><td>{m.times.map((t) => t.slice(0, 5)).join(', ')}</td></tr>)}
            </tbody></table>
            {manage ? <AddMedication onAdd={(b) => act(() => api!.addMedication(id, b))} /> : null}
          </div>
          <div className="card">
            <strong>Doses</strong>
            <table><tbody>
              {medEvents.map((e) => <tr key={e.id}><td className="muted">{e.due_at.slice(0, 16).replace('T', ' ')}</td><td>{meds.find((m) => m.id === e.medication_id)?.name ?? ''}</td><td><span className={`pill ${e.status === 'taken' ? 'ok' : e.status === 'missed' ? 'urgent' : 'grey'}`}>{e.status}</span></td><td className="muted">{e.confirmed_via}</td></tr>)}
            </tbody></table>
          </div>
        </>
      )}

      {tab === 'profile' && (
        <div className="card">
          <table><thead><tr><th>Name</th><th>Role</th><th>Called</th><th>Phone</th><th>Active</th></tr></thead><tbody>
            {circle.map((m) => <tr key={m.id}><td>{m.display_name}{m.relationship ? ` (${m.relationship})` : ''}</td><td>{m.role.replace('_', ' ')}</td><td>{circleWords(m.tier).toLowerCase()}</td><td>{m.phone}</td><td>{m.active ? 'yes' : 'no'}</td></tr>)}
          </tbody></table>
          {manage ? <AddMember onAdd={(b) => act(() => api!.addMember(id, b))} /> : null}
          {manage ? <InviteForm onInvite={(b) => api!.invite(id, b)} /> : null}
          {manage ? (
            <div style={{ marginTop: 12 }}>
              <button disabled={busy || !!pairing} onClick={() => { void act(async () => setPairing(await api!.newDevice(id))); }}>Pair a new watch</button>
              {pairing?.pairing_code ? <p>Type this on the watch within 15 minutes: <code>{pairing.pairing_code}</code></p> : null}
            </div>
          ) : null}
        </div>
      )}

      {tab === 'health' ? (
        <div className="card">
          <strong>Pair a watch, step by step</strong>
          <ol>
            <li>{device ? `A watch is ${device.status} (battery ${device.battery_percent ?? '?'} %, synced ${timeAgo(device.last_sync_at)}).` : 'No watch is paired yet.'} {manage ? <button disabled={busy || !!pairing} onClick={() => { void act(async () => setPairing(await api!.newDevice(id))); }}>Create a pairing code</button> : null}</li>
            {pairing?.pairing_code ? <li>Type this code on the watch within 15 minutes: <code style={{ fontSize: 18 }}>{pairing.pairing_code}</code> (it works once).</li> : <li>The code appears here; it works once and expires after 15 minutes.</li>}
            <li>On the watch: install the IRA CARE app, open it, and type the code.</li>
            <li>Allow body sensors, physical activity, the microphone, notifications and location when asked, then allow <em>body sensors in the background</em> on the settings page the app opens.</li>
            <li>Keep the app&apos;s ongoing notification: it is what collects readings and asks the questions.</li>
            <li>Within a few minutes the Readings tab shows the first readings and the watch line above says &quot;synced just now&quot;. Then ask IRA to check in now and answer on the watch.</li>
          </ol>
          {demoPagesEnabled() ? <p className="muted">No watch to hand? The <a href="/watch">browser watch</a> pairs with the same code.</p> : null}
        </div>
      ) : null}

      {tab === 'profile' ? <HomeForm person={person} manage={manage} onSave={(b) => act(() => api!.updateHome(id, b))} /> : null}

      {tab === 'care' && settings ? <SettingsForm settings={settings} manage={manage} onSave={(s) => act(() => api!.putSettings(id, s))} /> : null}

      {tab === 'profile' && consents ? (
        <div className="card">
          <p>Family see alerts and summaries. The person&apos;s words are shown to a role only under a recorded consent.</p>
          <ul>
            <li>Transcripts for family: <strong>{consents.effective.transcripts_family ? 'allowed' : 'not allowed'}</strong></li>
            <li>Transcripts for carers: <strong>{consents.effective.transcripts_carer ? 'allowed' : 'not allowed'}</strong></li>
            <li>Transcripts for the care organisation: <strong>{consents.effective.transcripts_organisation ? 'allowed' : 'not allowed'}</strong></li>
            <li>Kept for <strong>{consents.retention_months} months</strong></li>
          </ul>
          <table><thead><tr><th>Kind</th><th>Decision</th><th>By</th><th>When</th></tr></thead><tbody>
            {consents.consents.map((c) => <tr key={c.id}><td>{c.kind.replace(/_/g, ' ')}</td><td>{c.granted ? 'granted' : 'declined'}{c.revoked_at ? ' (revoked)' : ''}</td><td>{c.granted_by.replace('_', ' ')} {c.granted_by_name}</td><td className="muted">{timeAgo(c.recorded_at)}</td></tr>)}
          </tbody></table>
          <p className="muted">Consents are recorded from the family app or by an administrator with the person present.</p>
        </div>
      ) : null}

      {tab === 'ira' && profile ? (
        <ProfileForm profile={profile} circle={circle} onSave={(b) => act(() => api!.putProfile(id, b))} busy={busy} />
      ) : null}
      {tab === 'ira' && (
        <RemindersCard reminders={reminders} busy={busy} onAdd={(t, d) => act(() => api!.addReminder(id, t, d))} onAct={(r, a) => act(() => api!.actOnReminder(r, a))} />
      )}
      {tab === 'ira' && (
        <div className="card">
          <strong>What IRA remembers (summaries, never the person&apos;s words)</strong>
          <p className="muted">The person or their representative can change or clear any day. An edited day is marked.</p>
          {memory.length === 0 ? <p className="muted">Nothing yet.</p> : (
            <table><tbody>{memory.map((m) => (
              <MemoryRow key={m.day} m={m} busy={busy} onSave={(summary) => act(() => api!.editMemory(id, m.day, summary))} onForget={() => act(() => api!.forgetMemory(id, m.day))} />
            ))}</tbody></table>
          )}
        </div>
      )}
      {tab === 'ira' && (
        <div className="card">
          {transcripts === 'refused' ? <p>Transcripts are private. They can be read only with the person&apos;s recorded consent for your role in their care circle. This attempt was written to the access log.</p> : null}
          {Array.isArray(transcripts) ? (
            <table><tbody>
              {transcripts.map((t) => <tr key={t.id}><td className="muted">{t.created_at.slice(0, 16).replace('T', ' ')}</td><td>{t.speaker === 'companion' ? 'IRA' : name}</td><td>{t.text}</td></tr>)}
            </tbody></table>
          ) : null}
          {Array.isArray(transcripts) && transcripts.length === 0 ? <p className="muted">No conversation yet.</p> : null}
        </div>
      )}

      {tab === 'reports' && (
        <>
          <div className="card row">
            <button disabled={busy} onClick={() => { void act(() => api!.generateReport(id, 'daily_carer', new Date(Date.now() - 86_400_000).toISOString().slice(0, 10))); }}>Yesterday&apos;s daily report</button>
            <button disabled={busy} className="grey" onClick={() => { void act(() => api!.generateReport(id, 'weekly_family', new Date(Date.now() - 7 * 86_400_000).toISOString().slice(0, 10))); }}>Last week&apos;s family report</button>
            <span className="muted">Reports are generated every morning by the server; these buttons generate one now. Summaries only, never the person&apos;s words.</span>
          </div>
          {reports.map((r) => (
            <div className="card" key={r.id}>
              <strong>{r.title}</strong> <span className="muted">· {r.kind.replace('_', ' ')} · sent via {r.delivered_channels.join(', ') || 'not yet'}</span>
              <pre style={{ whiteSpace: 'pre-wrap', fontFamily: 'inherit', marginTop: 8 }}>{r.body_text}</pre>
            </div>
          ))}
          {reports.length === 0 ? <p className="muted">No reports yet.</p> : null}
        </>
      )}

      {tab === 'search' && (
        <div className="card">
          <form className="row" onSubmit={(e) => { e.preventDefault(); void act(async () => setFound(await api!.search(id, query))); }}>
            <input placeholder="Search check-ins, alerts and (with consent) conversation" value={query} onChange={(e) => setQuery(e.target.value)} style={{ minWidth: 320 }} />
            <button type="submit" disabled={!query.trim() || busy}>Search</button>
            <span className="muted">Every search is written to the access log.</span>
          </form>
          {found ? (
            <>
              <p className="muted">{found.results.length} result(s) for &quot;{found.query}&quot;{found.transcripts_included ? '' : ' · conversation not included without consent'}</p>
              <table><tbody>{found.results.map((x) => <tr key={`${x.kind}-${x.id}`}><td className="muted">{x.at.slice(0, 16).replace('T', ' ')}</td><td><span className="pill grey">{x.kind}</span></td><td>{x.text}</td></tr>)}</tbody></table>
            </>
          ) : null}
        </div>
      )}

      {tab === 'access' && manage && (
        <div className="card">
          <p className="muted">Every view of this person&apos;s health data or transcripts.</p>
          {access.length > 100 ? <p className="muted">The latest 100 of {access.length} entries.</p> : null}
          <table><thead><tr><th>When</th><th>Who</th><th>What</th><th>Action</th></tr></thead><tbody>
            {access.slice(0, 100).map((e) => <tr key={e.id}><td className="muted">{e.at.slice(0, 16).replace('T', ' ')}</td><td>{accessWho(e.actor_label || e.actor_user_id || 'system')}</td><td>{ACCESS_WORDS[e.resource_kind] ?? e.resource_kind.replace(/_/g, ' ')}</td><td>{e.action === 'view' ? 'viewed' : e.action}</td></tr>)}
          </tbody></table>
        </div>
      )}
    </>
  );
}

function AddRoutine({ onAdd }: { onAdd: (b: { name: string; kind: string; time_of_day: string | null }) => Promise<void> }) {
  const [name, setName] = useState(''); const [kind, setKind] = useState('meal'); const [time, setTime] = useState('12:30');
  return (
    <form className="row" style={{ marginTop: 10 }} onSubmit={(e) => { e.preventDefault(); void onAdd({ name, kind, time_of_day: time || null }); setName(''); }}>
      <input placeholder="Routine name" value={name} onChange={(e) => setName(e.target.value)} required />
      <select value={kind} onChange={(e) => setKind(e.target.value)}>{['meal', 'medication', 'activity', 'sleep', 'social', 'other'].map((k) => <option key={k}>{k}</option>)}</select>
      <input type="time" value={time} onChange={(e) => setTime(e.target.value)} />
      <button type="submit">Add routine</button>
    </form>
  );
}

function AddMedication({ onAdd }: { onAdd: (b: { name: string; dose: string; times: string[] }) => Promise<void> }) {
  const [name, setName] = useState(''); const [dose, setDose] = useState(''); const [times, setTimes] = useState('08:00');
  return (
    <form className="row" style={{ marginTop: 10 }} onSubmit={(e) => { e.preventDefault(); void onAdd({ name, dose, times: times.split(',').map((t) => t.trim()).filter(Boolean) }); setName(''); }}>
      <input placeholder="Medication" value={name} onChange={(e) => setName(e.target.value)} required />
      <input placeholder="Dose" value={dose} onChange={(e) => setDose(e.target.value)} />
      <input placeholder="Times, e.g. 08:00, 20:00" value={times} onChange={(e) => setTimes(e.target.value)} />
      <button type="submit">Add medication</button>
    </form>
  );
}

function AddMember({ onAdd }: { onAdd: (b: { display_name: string; role: string; tier: number; phone: string; relationship: string }) => Promise<void> }) {
  const [name, setName] = useState(''); const [role, setRole] = useState('family'); const [phone, setPhone] = useState(''); const [rel, setRel] = useState('');
  const tier = role === 'care_organisation' ? 2 : 1;
  return (
    <form className="row" style={{ marginTop: 10 }} onSubmit={(e) => { e.preventDefault(); void onAdd({ display_name: name, role, tier, phone, relationship: rel }); setName(''); setPhone(''); }}>
      <input placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} required />
      <input placeholder="Relationship" value={rel} onChange={(e) => setRel(e.target.value)} />
      <select value={role} onChange={(e) => setRole(e.target.value)}>{['family', 'carer', 'clinician', 'care_organisation'].map((k) => <option key={k} value={k}>{k.replace('_', ' ')}</option>)}</select>
      <input placeholder="+44..." value={phone} onChange={(e) => setPhone(e.target.value)} />
      <button type="submit">Add to circle ({tier === 1 ? 'called first' : tier === 2 ? 'called second' : 'called last'})</button>
    </form>
  );
}

function HomeForm({ person, manage, onSave }: { person: Person; manage: boolean; onSave: (b: HomeUpdate) => Promise<void> }) {
  const [b, setB] = useState<HomeUpdate>({
    home_address: person.home_address, home_latitude: person.home_latitude, home_longitude: person.home_longitude,
    home_radius_m: person.home_radius_m, emergency_summary: person.emergency_summary,
  });
  const [notes, setNotes] = useState('');
  const num = (v: string) => (v.trim() === '' ? null : Number(v));
  return (
    <form className="card" onSubmit={(e) => { e.preventDefault(); void onSave(notes ? { ...b, access_notes: notes } : b).then(() => setNotes('')); }}>
      <p className="muted">Where responders go, and what they need to know on arrival. Free text you control; IRA CARE records no conditions of its own.</p>
      <table><tbody>
        <tr><td>Home address</td><td><input value={b.home_address ?? ''} disabled={!manage} onChange={(e) => setB({ ...b, home_address: e.target.value })} style={{ width: 360 }} /></td></tr>
        <tr><td>Home coordinates (latitude, longitude) and radius (m)</td><td>
          <input value={b.home_latitude ?? ''} disabled={!manage} placeholder="51.50000" onChange={(e) => setB({ ...b, home_latitude: num(e.target.value) })} style={{ width: 110 }} />{' '}
          <input value={b.home_longitude ?? ''} disabled={!manage} placeholder="-0.12000" onChange={(e) => setB({ ...b, home_longitude: num(e.target.value) })} style={{ width: 110 }} />{' '}
          <input type="number" value={b.home_radius_m ?? 150} disabled={!manage} min={20} max={5000} onChange={(e) => setB({ ...b, home_radius_m: Number(e.target.value) })} style={{ width: 80 }} />
          <span className="muted"> · used only to say &quot;at home&quot; or &quot;away&quot; on an SOS</span>
        </td></tr>
        <tr><td>Emergency information (shown with every SOS)</td><td><textarea value={b.emergency_summary ?? ''} disabled={!manage} rows={3} onChange={(e) => setB({ ...b, emergency_summary: e.target.value })} style={{ width: 360 }} /></td></tr>
        <tr><td>Access notes (key safe, which door, pets)</td><td>
          <textarea value={notes} disabled={!manage} rows={3} placeholder={person.has_access_notes ? 'Notes are stored (encrypted). Type here to replace them.' : 'None stored yet.'} onChange={(e) => setNotes(e.target.value)} style={{ width: 360 }} />
          <div className="muted">Encrypted at rest. Shown only to the people called first and second while an alert is open; every view is logged.</div>
        </td></tr>
      </tbody></table>
      {manage ? <button type="submit">Save</button> : null}
    </form>
  );
}

const CONSENT_CHOICES: { kind: string; label: string }[] = [
  { kind: 'transcripts_family', label: 'Family may read what the person says to IRA' },
  { kind: 'health_data_family', label: 'Family may see readings' },
  { kind: 'transcripts_carer', label: 'Carers may read what the person says to IRA' },
  { kind: 'transcripts_organisation', label: 'The organisation may read what the person says to IRA' },
];

function InviteForm({ onInvite }: { onInvite: (b: { email: string; display_name: string; relationship: string; role: string; tier: number; phone: string; consents: { kind: string; granted: boolean; granted_by: string; granted_by_name: string }[] }) => Promise<InvitationOut> }) {
  const [email, setEmail] = useState(''); const [name, setName] = useState(''); const [rel, setRel] = useState('');
  const [role, setRole] = useState('family'); const [phone, setPhone] = useState('');
  const [given, setGiven] = useState<Record<string, boolean>>({});
  const [by, setBy] = useState<'person' | 'legal_representative'>('person'); const [byName, setByName] = useState('');
  const [out, setOut] = useState<InvitationOut | null>(null); const [err, setErr] = useState('');
  return (
    <form className="card" style={{ marginTop: 10 }} onSubmit={(e) => {
      e.preventDefault(); setErr('');
      const consents = CONSENT_CHOICES.filter((c) => given[c.kind]).map((c) => ({ kind: c.kind, granted: true, granted_by: by, granted_by_name: byName }));
      onInvite({ email, display_name: name, relationship: rel, role, tier: 1, phone, consents }).then((o) => { setOut(o); setEmail(''); }).catch((x) => setErr(x instanceof Error ? x.message : 'Could not invite.'));
    }}>
      <strong>Invite by link</strong>
      <div className="row" style={{ gap: 8, marginTop: 6 }}>
        <input placeholder="email" value={email} onChange={(e) => setEmail(e.target.value)} style={{ width: 220 }} data-testid="invite-email" />
        <input placeholder="name" value={name} onChange={(e) => setName(e.target.value)} style={{ width: 140 }} />
        <input placeholder="relationship" value={rel} onChange={(e) => setRel(e.target.value)} style={{ width: 120 }} />
        <select value={role} onChange={(e) => setRole(e.target.value)}><option value="family">family</option><option value="carer">carer</option><option value="clinician">clinician</option><option value="care_organisation">care organisation</option></select>
        <input placeholder="+44…" value={phone} onChange={(e) => setPhone(e.target.value)} style={{ width: 130 }} />
      </div>
      <div style={{ marginTop: 8 }}>
        <span className="muted">Consent given now, for this person&apos;s role:</span>
        {CONSENT_CHOICES.map((c) => <label key={c.kind} style={{ display: 'block' }}><input type="checkbox" checked={!!given[c.kind]} onChange={(e) => setGiven({ ...given, [c.kind]: e.target.checked })} /> {c.label}</label>)}
        <label>Given by <select value={by} onChange={(e) => setBy(e.target.value as 'person' | 'legal_representative')}><option value="person">the person</option><option value="legal_representative">their legal representative</option></select></label>{' '}
        <input placeholder="their name" value={byName} onChange={(e) => setByName(e.target.value)} style={{ width: 160 }} />
      </div>
      <button type="submit" disabled={!email} style={{ marginTop: 8 }}>Send the invitation</button>
      {err ? <p className="error">{err}</p> : null}
      {out ? <p className="ok" data-testid="invite-link">Invitation sent to {out.email} ({out.mail.delivered ? `by ${out.mail.transport}` : 'mail not delivered'}). Link to share another way: <code>{out.link}</code></p> : null}
    </form>
  );
}

function SettingsForm({ settings, manage, onSave }: { settings: SafetySettings; manage: boolean; onSave: (s: SafetySettings) => Promise<void> }) {
  const [s, setS] = useState<SafetySettings>(settings);
  const num = (k: keyof SafetySettings, nullable = false) => (
    <input type="number" value={(s[k] as number | null) ?? ''} disabled={!manage}
      onChange={(e) => setS({ ...s, [k]: e.target.value === '' ? (nullable ? null : 0) : Number(e.target.value) })} style={{ width: 90 }} />
  );
  return (
    <form className="card" onSubmit={(e) => { e.preventDefault(); void onSave(s); }}>
      <p className="muted">Set per person by a carer or clinician. An empty heart-rate bound means no heart-rate rule; there is no built-in range.</p>
      <table><tbody>
        <tr><td>Quiet hours</td><td><input type="time" value={s.quiet_start.slice(0, 5)} disabled={!manage} onChange={(e) => setS({ ...s, quiet_start: e.target.value })} /> to <input type="time" value={s.quiet_end.slice(0, 5)} disabled={!manage} onChange={(e) => setS({ ...s, quiet_end: e.target.value })} /></td></tr>
        <tr><td>Check-in every (minutes)</td><td>{num('checkin_min_minutes')} to {num('checkin_max_minutes')}</td></tr>
        <tr><td>Repeat after (s) / check sensors after (s)</td><td>{num('repeat_after_s')} / {num('sensor_check_after_s')}</td></tr>
        <tr><td>Heart rate range (bpm)</td><td>{num('heart_rate_min', true)} to {num('heart_rate_max', true)}</td></tr>
        <tr><td>No movement window (s) / movement floor</td><td>{num('no_movement_window_s')} / {num('movement_floor_steps')}</td></tr>
        <tr><td>Off wrist alert (min) / battery alert (%) / no sync alert (min)</td><td>{num('off_wrist_alert_minutes')} / {num('battery_alert_percent')} / {num('sync_alert_minutes')}</td></tr>
        <tr><td>Missed check-ins before escalation / wait for acknowledgement (min)</td><td>{num('missed_checkins_before_escalation')} / {num('ack_wait_minutes')}</td></tr>
        <tr><td>&quot;Are you OK?&quot; waits (s) before an SOS</td><td>{num('ask_first_wait_s')}</td></tr>
        <tr><td>Heart rate counts as outside the range after (readings in a row) over at least (s)</td><td>{num('heart_rate_confirm_readings')} / {num('heart_rate_confirm_seconds')}</td></tr>
        <tr><td>Watch off the wrist at night: notice after (min)</td><td>{num('night_off_wrist_minutes')}</td></tr>
        <tr><td>Fall detection (the watch asks first, then an SOS with location)</td><td>
          <label><input type="checkbox" checked={s.fall_detection_enabled} disabled={!manage} onChange={(e) => setS({ ...s, fall_detection_enabled: e.target.checked })} /> on for this person</label>
          {' '}· countdown (s) {num('fall_countdown_s')}
        </td></tr>
        <tr><td>Fall thresholds: impact (g) / still for (s) / movement above (g)</td><td>{num('fall_impact_g')} / {num('fall_still_seconds')} / {num('fall_still_threshold_g')}<span className="muted"> · settings, not medical claims; accuracy on real devices is unmeasured</span></td></tr>
        <tr><td>A planned visit counts as late with no arrival after (min)</td><td>{num('visit_late_minutes')}<span className="muted"> · a notice to the coordinator and an extra check-in; missed at the window&apos;s end is an alert</span></td></tr>
        <tr><td>Hydration prompt every (min; 0 = off)</td><td>{num('hydration_every_minutes')}<span className="muted"> · in waking hours, as a reminder the watch asks</span></td></tr>
        <tr><td>Usual wake time / morning check after (min)</td><td><input type="time" value={s.usual_wake_time.slice(0, 5)} disabled={!manage} onChange={(e) => setS({ ...s, usual_wake_time: e.target.value })} /> / {num('wake_grace_minutes')}</td></tr>
      </tbody></table>
      {manage ? <button type="submit">Save settings</button> : <p className="muted">Only an administrator may change these.</p>}
    </form>
  );
}


function PlanVisit({ circle, onPlan, busy }: { circle: CircleMember[]; onPlan: (b: { planned_start: string; planned_end: string; carer_member_id: string | null; carer_name: string; purpose: string; tasks: string[] }) => void; busy: boolean }) {
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [member, setMember] = useState('');
  const [purpose, setPurpose] = useState('');
  const [tasks, setTasks] = useState('');
  const carers = circle.filter((m) => m.active !== false);
  return (
    <form className="card" onSubmit={(e) => {
      e.preventDefault();
      if (!start || !end) return;
      const chosen = carers.find((m) => m.id === member);
      onPlan({ planned_start: new Date(start).toISOString(), planned_end: new Date(end).toISOString(), carer_member_id: member || null,
        carer_name: chosen?.display_name ?? '', purpose, tasks: tasks.split('\n').map((t) => t.trim()).filter(Boolean) });
      setStart(''); setEnd(''); setPurpose(''); setTasks('');
    }}>
      <strong>Plan a visit</strong>
      <div className="row">
        <label>From <input type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} required /></label>
        <label>To <input type="datetime-local" value={end} onChange={(e) => setEnd(e.target.value)} required /></label>
        <label>Carer <select value={member} onChange={(e) => setMember(e.target.value)}><option value="">Not chosen yet</option>{carers.map((m) => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select></label>
      </div>
      <label>Purpose <input value={purpose} onChange={(e) => setPurpose(e.target.value)} maxLength={200} /></label>
      <label>Tasks, one per line <textarea value={tasks} onChange={(e) => setTasks(e.target.value)} rows={3} /></label>
      <button type="submit" disabled={busy}>Plan the visit</button>
      <p className="muted">With no arrival, the server marks the visit late after the set minutes (a notice) and missed at the window&apos;s end (an alert), and IRA asks the person an extra check-in.</p>
    </form>
  );
}

function DepartForm({ busy, tasks, onDepart }: { busy: boolean; tasks: string[]; onDepart: (b: { outcome: Exclude<VisitOutcome, ''>; notes: string; handover: string; tasks_done: string[] }) => void }) {
  const [outcome, setOutcome] = useState<Exclude<VisitOutcome, ''>>('all_well');
  const [handover, setHandover] = useState('');
  const [notes, setNotes] = useState('');
  return (
    <form className="row" onSubmit={(e) => { e.preventDefault(); onDepart({ outcome, notes, handover, tasks_done: tasks }); }}>
      <label>Outcome <select value={outcome} onChange={(e) => setOutcome(e.target.value as Exclude<VisitOutcome, ''>)}>{(Object.keys(VISIT_OUTCOME_WORDS) as Exclude<VisitOutcome, ''>[]).map((o) => <option key={o} value={o}>{VISIT_OUTCOME_WORDS[o]}</option>)}</select></label>
      <label>Notes <input value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={4000} /></label>
      <label>Handover <input value={handover} onChange={(e) => setHandover(e.target.value)} maxLength={4000} /></label>
      <button type="submit" disabled={busy}>Carer has left: record the visit</button>
    </form>
  );
}


const PROFILE_WORDS = {
  pace: { slow: 'Slow: IRA speaks more slowly and leaves longer pauses', normal: 'Normal' },
  prompting_level: { light: 'Light: one quiet question, no repeat', standard: 'Standard: one repeat after a minute', full: 'Full: repeats sooner, twice' },
} as const;

function ProfileForm({ profile, circle, onSave, busy }: { profile: CommunicationProfile; circle: CircleMember[]; onSave: (b: CommunicationProfile) => void; busy: boolean }) {
  const [p, setP] = useState<CommunicationProfile>(profile);
  const consented = profile.topics_consented === true;
  return (
    <form className="card" onSubmit={(e) => { e.preventDefault(); onSave(p); }}>
      <strong>How IRA speaks to this person</strong>
      <p className="muted">Set by the person or their representative{profile.updated_by_role ? ` (last changed by the ${profile.updated_by_role})` : ''}. Applied on the watch and the phone.</p>
      <table><tbody>
        <tr><td>Pace</td><td><select value={p.pace} onChange={(e) => setP({ ...p, pace: e.target.value as CommunicationProfile['pace'] })}>{(Object.keys(PROFILE_WORDS.pace) as CommunicationProfile['pace'][]).map((k) => <option key={k} value={k}>{PROFILE_WORDS.pace[k]}</option>)}</select></td></tr>
        <tr><td>Prompting</td><td><select value={p.prompting_level} onChange={(e) => setP({ ...p, prompting_level: e.target.value as CommunicationProfile['prompting_level'] })}>{(Object.keys(PROFILE_WORDS.prompting_level) as CommunicationProfile['prompting_level'][]).map((k) => <option key={k} value={k}>{PROFILE_WORDS.prompting_level[k]}</option>)}</select></td></tr>
        <tr><td>Channels</td><td>
          <label><input type="checkbox" checked={p.voice_on} onChange={(e) => setP({ ...p, voice_on: e.target.checked })} /> voice</label>{' '}
          <label><input type="checkbox" checked={p.touch_on} onChange={(e) => setP({ ...p, touch_on: e.target.checked })} /> touch</label>{' '}
          <label><input type="checkbox" checked={p.vibration_on} onChange={(e) => setP({ ...p, vibration_on: e.target.checked })} /> vibration</label>
          <span className="muted"> · a safety question is always spoken</span>
        </td></tr>
        <tr><td>When a worry repeats, offer</td><td><select value={p.redirect_member_id ?? ''} onChange={(e) => setP({ ...p, redirect_member_id: e.target.value || null })}><option value="">the carer</option>{circle.map((m) => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select></td></tr>
        <tr><td>Familiar topics</td><td><input value={p.familiar_topics.join(', ')} disabled={!consented} onChange={(e) => setP({ ...p, familiar_topics: e.target.value.split(',').map((t) => t.trim()).filter(Boolean) })} placeholder={consented ? 'the garden, the grandchildren' : 'needs the topics consent'} /></td></tr>
        <tr><td>Topics to avoid</td><td><input value={p.topics_avoid.join(', ')} disabled={!consented} onChange={(e) => setP({ ...p, topics_avoid: e.target.value.split(',').map((t) => t.trim()).filter(Boolean) })} placeholder={consented ? 'the accident' : 'needs the topics consent'} /></td></tr>
      </tbody></table>
      {!consented ? <p className="muted">Topic lists are used only once the person&apos;s &quot;topics&quot; consent is recorded on the Consent tab.</p> : null}
      <button type="submit" disabled={busy}>Save how IRA speaks</button>
    </form>
  );
}

const REMINDER_WORDS: Record<Reminder['status'], string> = {
  pending: 'Waiting', asked: 'Asked on the watch', confirming: 'Checking once more', confirmed: 'Done', snoozed: 'Asked again later',
  skipped: 'Not today', missed: 'No answer', cancelled: 'Cancelled',
};

function RemindersCard({ reminders, busy, onAdd, onAct }: { reminders: Reminder[]; busy: boolean; onAdd: (text: string, dueAt: string) => void; onAct: (id: string, action: 'confirm' | 'snooze' | 'skip' | 'cancel') => void }) {
  const [text, setText] = useState('');
  const [due, setDue] = useState('');
  return (
    <div className="card">
      <strong>Reminders</strong>
      <p className="muted">From conversation (&quot;remind me to ring Sravani at three&quot;), from here, or the hydration setting. The watch asks at the time; a dose is confirmed in two steps.</p>
      <table><thead><tr><th>When</th><th>What</th><th>From</th><th>State</th><th></th></tr></thead><tbody>
        {reminders.map((r) => (
          <tr key={r.id}><td className="muted">{r.due_at.slice(0, 16).replace('T', ' ')}</td><td>{r.kind === 'hydration' ? 'A drink of water' : r.kind === 'medication' ? `Medication: ${r.text}` : r.text}</td><td>{r.source}</td>
            <td><span className={`pill ${r.status === 'confirmed' ? 'ok' : r.status === 'missed' ? 'urgent' : 'grey'}`}>{REMINDER_WORDS[r.status]}</span></td>
            <td className="row">{['pending', 'asked', 'confirming', 'snoozed'].includes(r.status) ? (<>
              <button disabled={busy} onClick={() => onAct(r.id, 'confirm')}>Done</button>
              <button disabled={busy} onClick={() => onAct(r.id, 'snooze')}>Later</button>
              <button disabled={busy} onClick={() => onAct(r.id, 'skip')}>Skip</button>
            </>) : null}</td></tr>
        ))}
      </tbody></table>
      <form className="row" onSubmit={(e) => { e.preventDefault(); if (text && due) { onAdd(text, new Date(due).toISOString()); setText(''); setDue(''); } }}>
        <label>Remind them to <input value={text} onChange={(e) => setText(e.target.value)} maxLength={200} required /></label>
        <label>at <input type="datetime-local" value={due} onChange={(e) => setDue(e.target.value)} required /></label>
        <button type="submit" disabled={busy}>Add the reminder</button>
      </form>
    </div>
  );
}

function MemoryRow({ m, busy, onSave, onForget }: { m: Memory; busy: boolean; onSave: (summary: string) => void; onForget: () => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(m.summary);
  return (
    <tr>
      <td className="muted">{m.day}{m.edited_at ? <div className="pill grey">edited by the {m.edited_by || 'person'}</div> : null}</td>
      <td style={{ whiteSpace: 'pre-line' }}>{editing ? <textarea value={text} onChange={(e) => setText(e.target.value)} rows={4} style={{ width: '100%' }} aria-label={`What IRA remembers for ${m.day}`} /> : m.summary}</td>
      <td className="muted">{m.exchanges} exchanges</td>
      <td className="row">
        {editing ? <button disabled={busy} onClick={() => { onSave(text); setEditing(false); }}>Save</button> : <button disabled={busy} onClick={() => setEditing(true)}>Change</button>}
        <button disabled={busy} onClick={onForget}>Clear this day</button>
      </td>
    </tr>
  );
}
