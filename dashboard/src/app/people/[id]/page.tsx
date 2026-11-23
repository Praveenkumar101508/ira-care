'use client';

import { useParams, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import { AlertCard } from '@/components/AlertCard';
import { Sparkline } from '@/components/Sparkline';
import { canManage, useApi } from '@/components/useApi';
import { ApiError, type Reading } from '@/lib/api';
import type {
  AccessEntry, Alert, Checkin, CircleMember, ConsentList, Device, HomeUpdate, InvitationOut, Medication, Memory, MedicationEvent, Person, PersonStatus, Report, Routine, SafetySettings, SearchResponse, Transcript,
} from '@/lib/types';
import { byDay, checkinCounts, series, sortAlerts, timeAgo } from '@/lib/view';

const TABS = ['timeline', 'readings', 'routines', 'medication', 'circle', 'home', 'watch', 'settings', 'consent', 'conversation', 'reports', 'search', 'access'] as const;
type Tab = (typeof TABS)[number];

export default function PersonPage() {
  const { id } = useParams<{ id: string }>();
  const search = useSearchParams();
  const tab = (search.get('tab') as Tab | null) ?? 'timeline';
  const { api, session, ready } = useApi();
  const manage = canManage(session);

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
  const [query, setQuery] = useState('');
  const [found, setFound] = useState<SearchResponse | null>(null);
  const [pairing, setPairing] = useState<Device | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!api) return;
    try {
      const [s, a, c, v, r, m, me, ci, se, co] = await Promise.all([
        api.status(id), api.alerts(false, id), api.checkins(id), api.vitals(id), api.routines(id), api.medications(id),
        api.medicationEvents(id), api.circle(id), api.settings(id), api.consents(id),
      ]);
      setStatus(s); setAlerts(a); setCheckins(c); setVitals(v); setRoutines(r); setMeds(m); setMedEvents(me); setCircle(ci); setSettings(se); setConsents(co);
      setError('');
      if (tab === 'conversation') {
        setMemory(await api.memory(id));
        try { setTranscripts(await api.transcripts(id)); } catch (e) { setTranscripts(e instanceof ApiError && e.status === 403 ? 'refused' : null); }
      }
      if (tab === 'access' && manage) setAccess(await api.accessLog(id));
      if (tab === 'reports') setReports(await api.reports(id));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) window.location.href = '/login';
      else setError(e instanceof ApiError ? e.message : 'Could not load.');
    }
  }, [api, id, tab, manage]);
  useEffect(() => { void load(); }, [load]);

  if (!ready || !status) return error ? <p className="error">{error}</p> : null;
  const person = status.person;
  const name = person.preferred_name || person.display_name;
  const act = async (fn: () => Promise<unknown>) => { setBusy(true); try { await fn(); await load(); } catch (e) { setError(e instanceof ApiError ? e.message : 'Failed.'); } finally { setBusy(false); } };
  const counts = checkinCounts(checkins.checkins);
  const device = status.devices[0];

  return (
    <>
      <h2>{name} <span className="muted" style={{ fontSize: 14 }}>{person.display_name} · {person.timezone} · {person.status}</span></h2>
      <div className="card row">
        <span><strong>Watch:</strong> {device ? `${device.status}, battery ${device.battery_percent ?? '?'} %, ${device.on_wrist === false ? 'off the wrist' : 'on the wrist'}, synced ${timeAgo(device.last_sync_at)}${device.background_sensor_permission === false ? ', sensor permission OFF' : ''}` : 'none paired'}</span>
        <span><strong>Check-ins (last 200):</strong> {counts.answered} answered, {counts.missed} missed, {counts.skippedQuiet} at night</span>
        <span><strong>Open alerts:</strong> {status.open_alerts.length}</span>
      </div>
      <nav className="tabs" aria-label="Sections of this person's record">
        {TABS.filter((t) => t !== 'access' || manage).map((t) => <Link key={t} href={`/people/${id}?tab=${t}`} className={t === tab ? 'on' : ''}>{t[0].toUpperCase() + t.slice(1)}</Link>)}
      </nav>
      {error ? <p className="error">{error}</p> : null}

      {tab === 'timeline' && (
        <>
          {sortAlerts(alerts).filter((a) => a.status !== 'resolved').map((a) => (
            <AlertCard key={a.id} alert={a} personName={name} person={person} busy={busy}
              onAccessNotes={(x) => api!.accessNotes(x.id)}
              onAcknowledge={(x) => { void act(() => api!.acknowledge(x.id, 'from the dashboard')); }}
              onResolve={(x, outcome, resolution) => { void act(() => api!.resolve(x.id, resolution, outcome)); }} />
          ))}
          {byDay(checkins.checkins, 'scheduled_at').map(({ day, items }) => (
            <div className="card" key={day}>
              <strong>{day}</strong>
              <table><tbody>
                {items.map((c) => (
                  <tr key={c.id}>
                    <td className="muted">{c.scheduled_at.slice(11, 16)}</td>
                    <td>{c.question_kind}{c.question_source === 'model' ? <span className="muted"> · composed</span> : null}</td>
                    <td><span className={`pill ${c.status === 'answered' ? 'ok' : c.status === 'missed' ? 'urgent' : 'grey'}`}>{c.status}</span></td>
                    <td>{c.answer_summary}{checkins.answers_included && c.answer_text ? <em className="muted"> “{c.answer_text}”</em> : null}</td>
                  </tr>
                ))}
              </tbody></table>
            </div>
          ))}
          {!checkins.answers_included ? <p className="muted">Summaries only: the person&apos;s own words need their recorded consent for your role.</p> : null}
        </>
      )}

      {tab === 'readings' && (
        <div className="grid">
          {['heart_rate', 'steps', 'movement', 'on_wrist', 'battery'].map((k) => (
            <div className="card" key={k}><Sparkline points={series(vitals, k)} label={k.replace('_', ' ')} /></div>
          ))}
          {settings ? <div className="card"><strong>Range set for this person</strong><div className="muted">Heart rate {settings.heart_rate_min ?? 'not set'} to {settings.heart_rate_max ?? 'not set'}. Set by a carer or clinician; there is no built-in range.</div></div> : null}
        </div>
      )}

      {tab === 'routines' && (
        <div className="card">
          <table><thead><tr><th>Routine</th><th>Kind</th><th>Time</th><th>Days</th></tr></thead><tbody>
            {routines.map((r) => <tr key={r.id}><td>{r.name}</td><td>{r.kind}</td><td>{r.time_of_day?.slice(0, 5) ?? '-'}</td><td>{r.days_of_week.join(' ')}</td></tr>)}
          </tbody></table>
          {manage ? <AddRoutine onAdd={(b) => act(() => api!.addRoutine(id, b))} /> : null}
        </div>
      )}

      {tab === 'medication' && (
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

      {tab === 'circle' && (
        <div className="card">
          <table><thead><tr><th>Name</th><th>Role</th><th>Tier</th><th>Phone</th><th>Active</th></tr></thead><tbody>
            {circle.map((m) => <tr key={m.id}><td>{m.display_name}{m.relationship ? ` (${m.relationship})` : ''}</td><td>{m.role.replace('_', ' ')}</td><td>{m.tier}</td><td>{m.phone}</td><td>{m.active ? 'yes' : 'no'}</td></tr>)}
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

      {tab === 'watch' ? (
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
          <p className="muted">No watch to hand? The <a href="/simulator">watch simulator</a> pairs with the same code. The full guide is docs/DEVICE_SETUP.md.</p>
        </div>
      ) : null}

      {tab === 'home' ? <HomeForm person={person} manage={manage} onSave={(b) => act(() => api!.updateHome(id, b))} /> : null}

      {tab === 'settings' && settings ? <SettingsForm settings={settings} manage={manage} onSave={(s) => act(() => api!.putSettings(id, s))} /> : null}

      {tab === 'consent' && consents ? (
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

      {tab === 'conversation' && (
        <div className="card">
          <strong>What IRA remembers (summaries, never the person&apos;s words)</strong>
          {memory.length === 0 ? <p className="muted">Nothing yet.</p> : (
            <table><tbody>{memory.map((m) => <tr key={m.day}><td className="muted">{m.day}</td><td style={{ whiteSpace: 'pre-line' }}>{m.summary}</td><td className="muted">{m.exchanges} exchanges</td></tr>)}</tbody></table>
          )}
        </div>
      )}
      {tab === 'conversation' && (
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
          <table><thead><tr><th>When</th><th>Who</th><th>What</th><th>Action</th></tr></thead><tbody>
            {access.map((e) => <tr key={e.id}><td className="muted">{e.at.slice(0, 19).replace('T', ' ')}</td><td>{e.actor_label || e.actor_user_id || 'system'}</td><td>{e.resource_kind}</td><td>{e.action}</td></tr>)}
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
      <button type="submit">Add to circle (tier {tier})</button>
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
          <div className="muted">Encrypted at rest. Shown only to tier 1 and 2 recipients while an alert is open; every view is logged.</div>
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
        <tr><td>Usual wake time / morning check after (min)</td><td><input type="time" value={s.usual_wake_time.slice(0, 5)} disabled={!manage} onChange={(e) => setS({ ...s, usual_wake_time: e.target.value })} /> / {num('wake_grace_minutes')}</td></tr>
      </tbody></table>
      {manage ? <button type="submit">Save settings</button> : <p className="muted">Only an administrator may change these.</p>}
    </form>
  );
}
