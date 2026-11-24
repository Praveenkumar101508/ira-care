import React, { useEffect, useState } from 'react';
import { Linking, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import type { CareClient } from '../api/client';
import { ApiError } from '../api/client';
import type { Checkin, CircleMember, ConsentList, Device, Person, PersonStatus, Report, Visit, VisitOutcome } from '../api/types';
import { VISIT_OUTCOMES, VISIT_OUTCOME_WORDS, currentVisit, visitActions, visitTitle } from '../model/visits';
import { alertTitle } from '../model/alerts';
import { deviceSummary, readingLabel, telLink, timeAgo } from '../model/format';
import { BigButton, Card, Muted, Screen, colors } from '../ui';

type Tab = 'status' | 'visits' | 'history' | 'reports' | 'contacts' | 'consent';

export function PersonScreen({ client, person, canManage, onBack, onTalk, onPair }: {
  client: CareClient; person: Person; canManage: boolean; onBack: () => void; onTalk?: () => void; onPair?: () => void;
}) {
  const [tab, setTab] = useState<Tab>('status');
  const [status, setStatus] = useState<PersonStatus | null>(null);
  const [checkins, setCheckins] = useState<Checkin[]>([]);
  const [answersIncluded, setAnswersIncluded] = useState(false);
  const [circle, setCircle] = useState<CircleMember[]>([]);
  const [consents, setConsents] = useState<ConsentList | null>(null);
  const [pairing, setPairing] = useState<Device | null>(null);
  const [reports, setReports] = useState<Report[]>([]);
  const [visits, setVisits] = useState<Visit[]>([]);
  const [handover, setHandover] = useState('');
  const [outcome, setOutcome] = useState<VisitOutcome>('all_well');
  const [error, setError] = useState('');

  const load = async () => {
    setError('');
    try {
      const [s, c, m, k, rp, vs] = await Promise.all([
        client.status(person.id), client.checkins(person.id), client.circle(person.id), client.consents(person.id), client.reports(person.id),
        client.visits(person.id),
      ]);
      setStatus(s); setCheckins(c.checkins); setAnswersIncluded(c.answers_included); setCircle(m); setConsents(k); setReports(rp); setVisits(vs);
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
        {(['status', 'visits', 'history', 'reports', 'contacts', 'consent'] as Tab[]).map((t) => (
          <Text key={t} onPress={() => setTab(t)} style={[styles.tab, tab === t && styles.tabOn]} testID={`tab-${t}`}>
            {t[0].toUpperCase() + t.slice(1)}
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
                <Muted>{m.role.replace('_', ' ')}{m.relationship ? ` · ${m.relationship}` : ''} · tier {m.tier}</Muted>
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

const styles = StyleSheet.create({
  tabs: { flexDirection: 'row', gap: 8, marginBottom: 10 },
  tab: { paddingVertical: 6, paddingHorizontal: 12, borderRadius: 999, backgroundColor: colors.card, color: colors.muted, borderWidth: 1, borderColor: colors.border },
  tabOn: { backgroundColor: colors.notice, color: '#fff', borderColor: colors.notice },
  input: { borderWidth: 1, borderColor: colors.border, borderRadius: 8, padding: 10, minHeight: 60, color: colors.text, marginVertical: 8 },
  h: { fontSize: 16, fontWeight: '700', color: colors.text, marginBottom: 4 },
  line: { fontSize: 15, color: colors.text, marginTop: 2 },
  quote: { fontSize: 15, color: colors.text, fontStyle: 'italic', marginTop: 4 },
  code: { fontSize: 15, fontFamily: 'monospace', marginTop: 6, color: colors.text },
});
