import React, { useEffect, useState } from 'react';
import { Linking, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import type { CareClient } from '../api/client';
import { ApiError } from '../api/client';
import type { CarePlan, Checkin, CircleMember, CommunicationProfile, ConsentList, Device, Medication, Memory, Person, PersonStatus, Reminder, Report, Visit, VisitOutcome } from '../api/types';
import { REMINDER_WORDS, reminderActions, reminderTitle } from '../model/reminders';
import { VISIT_OUTCOMES, VISIT_OUTCOME_WORDS, currentVisit, visitActions, visitTitle } from '../model/visits';
import { alertTitle } from '../model/alerts';
import { deviceSummary, readingLabel, telLink, timeAgo } from '../model/format';
import { BigButton, Card, Muted, Screen , useColors, type Palette } from '../ui';

type Tab = 'status' | 'plan' | 'visits' | 'ira' | 'history' | 'reports' | 'contacts' | 'consent';

export function PersonScreen({ client, person, canManage, onBack, onTalk, onPair }: {
  client: CareClient; person: Person; canManage: boolean; onBack: () => void; onTalk?: () => void; onPair?: () => void;
}) {
  const colors = useColors();
  const styles = makeStyles(colors);
  const [tab, setTab] = useState<Tab>('status');
  const [status, setStatus] = useState<PersonStatus | null>(null);
  const [checkins, setCheckins] = useState<Checkin[]>([]);
  const [answersIncluded, setAnswersIncluded] = useState(false);
  const [circle, setCircle] = useState<CircleMember[]>([]);
  const [consents, setConsents] = useState<ConsentList | null>(null);
  const [pairing, setPairing] = useState<Device | null>(null);
  const [reports, setReports] = useState<Report[]>([]);
  const [visits, setVisits] = useState<Visit[]>([]);
  const [profile, setProfile] = useState<CommunicationProfile | null>(null);
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [memory, setMemory] = useState<Memory[]>([]);
  const [medications, setMedications] = useState<Medication[]>([]);
  const [plan, setPlan] = useState<CarePlan | null>(null);
  const [editingDay, setEditingDay] = useState<string | null>(null);
  const [memoryText, setMemoryText] = useState('');
  const [handover, setHandover] = useState('');
  const [outcome, setOutcome] = useState<VisitOutcome>('all_well');
  const [error, setError] = useState('');

  const load = async () => {
    setError('');
    try {
      const [s, c, m, k, rp, vs, pr, rm, me, md, pl] = await Promise.all([
        client.status(person.id), client.checkins(person.id), client.circle(person.id), client.consents(person.id), client.reports(person.id),
        client.visits(person.id), client.profile(person.id), client.reminders(person.id), client.memory(person.id),
        client.medications(person.id), client.plan(person.id),
      ]);
      setStatus(s); setCheckins(c.checkins); setAnswersIncluded(c.answers_included); setCircle(m); setConsents(k); setReports(rp); setVisits(vs);
      setProfile(pr); setReminders(rm); setMemory(me); setMedications(md); setPlan(pl);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not load.');
    }
  };
  useEffect(() => { void load(); }, [person.id]);
  const act = async (fn: () => Promise<unknown>) => { try { await fn(); await load(); } catch (e) { setError(e instanceof ApiError ? e.message : 'Failed.'); } };

  const name = person.preferred_name || person.display_name;
  const personPhone = circle.find((m) => m.relationship.toLowerCase() === 'self')?.phone ?? '';
  const device = status?.devices[0];

  return (
    <Screen title={name}>
      <View style={styles.tabs}>
        {(['status', 'plan', 'visits', 'ira', 'history', 'reports', 'contacts', 'consent'] as Tab[]).map((t) => (
          <Text key={t} onPress={() => setTab(t)} style={[styles.tab, tab === t && styles.tabOn]} testID={`tab-${t}`}>
            {t === 'ira' ? 'IRA' : t[0].toUpperCase() + t.slice(1)}
          </Text>
        ))}
      </View>
      {error ? <Text style={{ color: colors.sos }}>{error}</Text> : null}
      <ScrollView>
        {tab === 'status' && (
          <>
            <Card>
              <Text style={styles.h}>Watch</Text>
              <Muted>{deviceSummary(device)}</Muted>
              {status?.latest_readings.map((r) => <Text key={r.kind} style={styles.line}>{readingLabel(r)} · {timeAgo(r.recorded_at)}</Text>)}
            </Card>
            <Card>
              <Text style={styles.h}>Last check-in</Text>
              {status?.last_checkin ? (
                <>
                  <Text style={styles.line}>{status.last_checkin.question_kind} · {status.last_checkin.status} · {timeAgo(status.last_checkin.scheduled_at)}</Text>
                  {status.last_checkin.answer_summary ? <Muted>{status.last_checkin.answer_summary}</Muted> : null}
                </>
              ) : <Muted>None yet.</Muted>}
            </Card>
            {status && status.open_alerts.length > 0 ? (
              <Card>
                <Text style={styles.h}>Open alerts</Text>
                {status.open_alerts.map((a) => <Text key={a.id} style={[styles.line, { color: colors.sos }]}>{alertTitle(a)} · {a.status}</Text>)}
              </Card>
            ) : null}
            {personPhone ? <BigButton label={`Call ${name}`} onPress={() => Linking.openURL(telLink(personPhone))} /> : null}
            <BigButton label="Ask IRA to check in now" color={colors.notice} onPress={async () => { await client.askNow(person.id); await load(); }} />
            {onTalk ? <BigButton label="Talk to IRA (on this phone)" color={colors.ok} testID="open-talk" onPress={onTalk} /> : null}
            {canManage && onPair ? <BigButton label="Pair a watch (step by step)" color={colors.muted} testID="open-pair" onPress={onPair} /> : null}
            {canManage && !onPair ? (
              <BigButton
                label={pairing ? 'Pairing code shown below' : 'Pair a new watch'}
                color={colors.muted}
                disabled={!!pairing}
                onPress={async () => setPairing(await client.newDevice(person.id))}
              />
            ) : null}
            {pairing?.pairing_code ? (
              <Card>
                <Text style={styles.h}>Type this code on the watch (valid 15 minutes)</Text>
                <Text selectable style={styles.code}>{pairing.pairing_code}</Text>
              </Card>
            ) : null}
          </>
        )}
        {tab === 'plan' && (
          <>
            <Card testID="medication-card">
              <Text style={styles.h}>Medication</Text>
              {medications.filter((m) => m.active).map((m) => (
                <Text key={m.id} style={styles.line} testID={`medication-${m.id}`}>{medicationLine(m)}</Text>
              ))}
              {medications.filter((m) => m.active).length === 0 ? <Muted>No medication recorded. The care team records it from the dashboard.</Muted> : null}
            </Card>
            <Card testID="care-plan-card">
              <Text style={styles.h}>Care plan</Text>
              {plan ? (
                <>
                  <Muted>{`Version ${plan.version}${plan.review_on ? ` · review on ${plan.review_on}` : ''}`}</Muted>
                  {plan.goals ? <Text style={styles.line} testID="plan-goals">{`Goals: ${plan.goals}`}</Text> : null}
                  {plan.support ? <Text style={styles.line} testID="plan-support">{`Support: ${plan.support}`}</Text> : null}
                  {plan.routine.map((r, i) => <Text key={i} style={styles.line} testID={`plan-routine-${i}`}>{`${r.when ? `${r.when} · ` : ''}${r.what} (${r.who.replace(/_/g, ' ')})`}</Text>)}
                  {plan.risks ? <Text style={styles.line}>{`Risks: ${plan.risks}`}</Text> : null}
                  {plan.emergency ? <Text style={styles.line}>{`In an emergency: ${plan.emergency}`}</Text> : null}
                </>
              ) : <Muted>No care plan written yet. The care team writes it from the dashboard.</Muted>}
            </Card>
          </>
        )}
        {tab === 'visits' && (
          <>
            {(() => {
              const v = currentVisit(visits);
              if (!v) return <Muted>No visit planned. Visits are planned from the dashboard.</Muted>;
              const acts = visitActions(v);
              return (
                <Card>
                  <Text style={styles.h} testID="visit-current">{visitTitle(v)}</Text>
                  {v.purpose ? <Muted>{v.purpose}</Muted> : null}
                  {v.tasks.map((t) => <Text key={t.id} style={styles.line}>{t.done ? '☑' : '☐'} {t.title}</Text>)}
                  {acts.includes('arrive') ? (
                    <BigButton label="I've arrived" testID="visit-arrive" hint="Tells the watch and the circle you are with them. Check-ins pause while you are here."
                      onPress={() => { void act(() => client.arrive(v.id)); }} />
                  ) : null}
                  {acts.includes('depart') ? (
                    <>
                      <Text style={styles.h}>Leaving: what happened?</Text>
                      <View style={styles.tabs}>
                        {VISIT_OUTCOMES.map((o) => (
                          <Text key={o} onPress={() => setOutcome(o)} style={[styles.tab, outcome === o && styles.tabOn]} testID={`visit-outcome-${o}`}>{VISIT_OUTCOME_WORDS[o]}</Text>
                        ))}
                      </View>
                      <TextInput value={handover} onChangeText={setHandover} placeholder="Handover for the next carer (optional)" multiline
                        style={styles.input} testID="visit-handover" accessibilityLabel="Handover for the next carer" />
                      <BigButton label="I'm leaving" testID="visit-depart" color={colors.notice} hint="Records the outcome, the handover and which tasks were done."
                        onPress={() => { void act(() => client.depart(v.id, { outcome, notes: '', handover, tasks_done: v.tasks.filter((t) => t.done).map((t) => t.id) })); setHandover(''); }} />
                    </>
                  ) : null}
                </Card>
              );
            })()}
            {visits.filter((v) => v.status === 'departed').slice(0, 10).map((v) => (
              <Card key={v.id}>
                <Text style={styles.line}>{visitTitle(v)}</Text>
                <Muted>{VISIT_OUTCOME_WORDS[v.outcome as VisitOutcome] ?? v.outcome}{v.handover ? ` · Handover: ${v.handover}` : ''}</Muted>
              </Card>
            ))}
          </>
        )}
        {tab === 'ira' && profile && (
          <>
            <Card>
              <Text style={styles.h}>How IRA speaks to {name}</Text>
              <Muted>Set by {name} or their representative. Applied on the watch.</Muted>
              <Text style={styles.line}>Pace</Text>
              <View style={styles.tabs}>
                {(['slow', 'normal'] as const).map((v) => <Text key={v} testID={`profile-pace-${v}`} onPress={() => setProfile({ ...profile, pace: v })} style={[styles.tab, profile.pace === v && styles.tabOn]}>{v === 'slow' ? 'Slow' : 'Normal'}</Text>)}
              </View>
              <Text style={styles.line}>Prompting</Text>
              <View style={styles.tabs}>
                {(['light', 'standard', 'full'] as const).map((v) => <Text key={v} testID={`profile-prompting-${v}`} onPress={() => setProfile({ ...profile, prompting_level: v })} style={[styles.tab, profile.prompting_level === v && styles.tabOn]}>{v[0].toUpperCase() + v.slice(1)}</Text>)}
              </View>
              <View style={styles.tabs}>
                {(['voice_on', 'touch_on', 'vibration_on'] as const).map((k) => <Text key={k} testID={`profile-${k}`} onPress={() => setProfile({ ...profile, [k]: !profile[k] })} style={[styles.tab, profile[k] && styles.tabOn]}>{k.replace('_on', '')}</Text>)}
              </View>
              {profile.topics_consented ? (
                <>
                  <TextInput value={profile.familiar_topics.join(', ')} onChangeText={(t) => setProfile({ ...profile, familiar_topics: t.split(',').map((x) => x.trim()).filter(Boolean) })} placeholder="Familiar topics, comma separated" style={styles.input} accessibilityLabel="Familiar topics" />
                  <TextInput value={profile.topics_avoid.join(', ')} onChangeText={(t) => setProfile({ ...profile, topics_avoid: t.split(',').map((x) => x.trim()).filter(Boolean) })} placeholder="Topics to avoid, comma separated" style={styles.input} accessibilityLabel="Topics to avoid" />
                </>
              ) : <Muted>Familiar topics and topics to avoid need the &quot;topics&quot; consent.</Muted>}
              <BigButton label="Save how IRA speaks" testID="profile-save" color={colors.notice} onPress={() => { void act(() => client.putProfile(person.id, profile)); }} />
            </Card>
            <Card>
              <Text style={styles.h}>Reminders</Text>
              {reminders.length === 0 ? <Muted>None. Say &quot;remind me to…&quot; to IRA, or add one on the dashboard.</Muted> : null}
              {reminders.slice(0, 10).map((r) => (
                <View key={r.id}>
                  <Text style={styles.line} testID={`reminder-${r.id}`}>{reminderTitle(r)} · {REMINDER_WORDS[r.status]}</Text>
                  <View style={styles.tabs}>
                    {reminderActions(r).map((a) => <Text key={a} testID={`reminder-${r.id}-${a}`} onPress={() => { void act(() => client.actOnReminder(r.id, a)); }} style={styles.tab}>{a === 'confirm' ? 'Done' : a === 'snooze' ? 'Later' : 'Skip'}</Text>)}
                  </View>
                </View>
              ))}
            </Card>
            <Card>
              <Text style={styles.h}>What IRA remembers</Text>
              <Muted>Summaries by day, never {name}&apos;s words. {name} or their representative can change or clear any day.</Muted>
              {memory.map((m) => (
                <View key={m.day}>
                  <Text style={styles.line}>{m.day}{m.edited_at ? ' · edited' : ''}</Text>
                  {editingDay === m.day ? (
                    <>
                      <TextInput value={memoryText} onChangeText={setMemoryText} multiline style={styles.input} accessibilityLabel={`What IRA remembers for ${m.day}`} testID="memory-edit" />
                      <BigButton label="Save" testID="memory-save" onPress={() => { void act(() => client.editMemory(person.id, m.day, memoryText)); setEditingDay(null); }} />
                    </>
                  ) : <Muted>{m.summary}</Muted>}
                  <View style={styles.tabs}>
                    <Text onPress={() => { setEditingDay(m.day); setMemoryText(m.summary); }} style={styles.tab} testID={`memory-change-${m.day}`}>Change</Text>
                    <Text onPress={() => { void act(() => client.forgetMemory(person.id, m.day)); }} style={styles.tab} testID={`memory-clear-${m.day}`}>Clear this day</Text>
                  </View>
                </View>
              ))}
            </Card>
          </>
        )}
        {tab === 'history' && (
          <>
            {!answersIncluded ? <Muted>Summaries only. The person's own words need their recorded consent.</Muted> : null}
            {checkins.map((c) => (
              <Card key={c.id}>
                <Text style={styles.line}>{c.question_kind} · {c.status} · {timeAgo(c.scheduled_at)}</Text>
                {c.answer_summary ? <Muted>{c.answer_summary}</Muted> : null}
                {answersIncluded && c.answer_text ? <Text style={styles.quote}>“{c.answer_text}”</Text> : null}
              </Card>
            ))}
            {checkins.length === 0 ? <Muted>No check-ins yet.</Muted> : null}
          </>
        )}
        {tab === 'reports' && (
          <>
            <Muted>The weekly report: summaries only, never the person's own words.</Muted>
            {reports.map((r) => (
              <Card key={r.id}>
                <Text style={styles.h}>{r.title}</Text>
                <Text style={styles.line}>{r.body_text}</Text>
              </Card>
            ))}
            {reports.length === 0 ? <Muted>No weekly report yet. The first one arrives on Monday morning.</Muted> : null}
          </>
        )}
        {tab === 'contacts' && (
          <>
            {circle.map((m) => (
              <Card key={m.id}>
                <Text style={styles.h}>{m.display_name}</Text>
                <Muted>{m.role.replace('_', ' ')}{m.relationship ? ` · ${m.relationship}` : ''} · {m.tier === 1 ? 'called first' : m.tier === 2 ? 'called second' : 'called last'}</Muted>
                {m.phone ? <BigButton label={`Call ${m.display_name}`} color={colors.notice} onPress={() => Linking.openURL(telLink(m.phone))} /> : null}
              </Card>
            ))}
            {circle.length === 0 ? <Muted>No contacts yet. A carer adds them from the dashboard.</Muted> : null}
          </>
        )}
        {tab === 'consent' && consents && (
          <>
            <Card>
              <Text style={styles.h}>What family may see</Text>
              <Muted>Alerts and summaries always. The person's words only with consent.</Muted>
              <Text style={styles.line}>Transcripts for family: {consents.effective.transcripts_family ? 'allowed' : 'not allowed'}</Text>
              <Text style={styles.line}>Transcripts for carers: {consents.effective.transcripts_carer ? 'allowed' : 'not allowed'}</Text>
              <Text style={styles.line}>Kept for {consents.retention_months} months</Text>
            </Card>
            {canManage ? (
              <>
                <BigButton label="Record: the person agrees family may read transcripts" color={colors.notice}
                  onPress={async () => { await client.recordConsent(person.id, 'transcripts_family', true, 'person', name); await load(); }} />
                <BigButton label="Record: withdrawn" color={colors.muted}
                  onPress={async () => { await client.recordConsent(person.id, 'transcripts_family', false, 'person', name); await load(); }} />
              </>
            ) : null}
            {consents.consents.map((c) => (
              <Card key={c.id}>
                <Text style={styles.line}>{c.kind.replace(/_/g, ' ')}: {c.granted ? 'granted' : 'declined'} by {c.granted_by.replace('_', ' ')}{c.granted_by_name ? ` (${c.granted_by_name})` : ''} · {timeAgo(c.recorded_at)}{c.revoked_at ? ' · revoked' : ''}</Text>
              </Card>
            ))}
          </>
        )}
      </ScrollView>
      <BigButton label="Back" color={colors.muted} onPress={onBack} />
    </Screen>
  );
}

/** One line per medicine: name, dose and the times of day, as the care team recorded them. */
export function medicationLine(m: Medication): string {
  const times = m.times.map((t) => t.slice(0, 5)).join(', ');
  return `${m.name}${m.dose ? ` · ${m.dose}` : ''}${times ? ` · ${times}` : ''}`;
}

const makeStyles = (colors: Palette) => StyleSheet.create({
  tabs: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginBottom: 10 },
  tab: { paddingVertical: 6, paddingHorizontal: 12, borderRadius: 999, backgroundColor: colors.card, color: colors.muted, borderWidth: 1, borderColor: colors.border },
  tabOn: { backgroundColor: colors.notice, color: '#fff', borderColor: colors.notice },
  input: { borderWidth: 1, borderColor: colors.border, borderRadius: 8, padding: 10, minHeight: 60, color: colors.text, marginVertical: 8 },
  h: { fontSize: 16, fontWeight: '700', color: colors.text, marginBottom: 4 },
  line: { fontSize: 15, color: colors.text, marginTop: 2 },
  quote: { fontSize: 15, color: colors.text, fontStyle: 'italic', marginTop: 4 },
  code: { fontSize: 15, fontFamily: 'monospace', marginTop: 6, color: colors.text },
});
