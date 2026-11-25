import React from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import type { Alert, Device, Person, Reminder, Visit } from '../api/types';
import { orderCards } from '../appearance/theme';
import { alertTitle, needsSomeone, sortAlerts } from '../model/alerts';
import { HOME_CARDS, nextVisit, nextVisitWords, openReminders, todaysVisits, type Experience, type Membership } from '../model/experience';
import { deviceSummary, timeAgo } from '../model/format';
import { REMINDER_WORDS, reminderTitle } from '../model/reminders';
import { visitTitle } from '../model/visits';
import { BigButton, Body, Card, Muted, Screen, useColors } from '../ui';

export interface HomeData {
  experience: Experience; memberships: Membership[]; people: Person[]; person?: Person; device?: Device;
  alerts: Alert[]; visits: Visit[]; reminders: Reminder[]; lastCheckin?: { question_kind: string; status: string; scheduled_at: string; answer_summary: string } | null;
  cardOrder: string[]; error: string;
}

export interface HomeActions {
  onHelp: () => void; onTalk: () => void; onAlerts: () => void; onCare: () => void; onSettings: () => void;
  onSwitchPerson: (p: Person) => void; onOnMyWay: (a: Alert) => void; onArrive: (v: Visit) => void; onDepart: (v: Visit) => void;
  onReminder: (r: Reminder, action: 'confirm' | 'snooze' | 'skip') => void;
}

/**
 * The home screen of the one app, by experience. The wearer sees their day;
 * family see the person; a carer sees today's visits. The person's own card
 * order applies, but Help (wearer) and the alerts that need someone (family,
 * carer) are drawn first whatever the order, in the fixed urgent colour.
 */
export function HomeTab({ d, a }: { d: HomeData; a: HomeActions }) {
  const c = useColors();
  const name = d.person ? (d.person.preferred_name || d.person.display_name) : '';
  const urgent = sortAlerts(d.alerts).filter(needsSomeone);
  const cards = orderCards(HOME_CARDS[d.experience], d.cardOrder).map((x) => x.key);
  const title = d.experience === 'wearer' ? `Hello${name ? `, ${name}` : ''}` : d.experience === 'carer' ? 'Today' : name || 'Care circle';

  const urgentFirst = d.experience === 'wearer' ? (
    <Card key="help-first" testID="card-help">
      <Body>If you need someone, press Help. Your family and carers are told at once, with where you are.</Body>
      <BigButton label="Help" color={c.sos} testID="home-help" hint="Tells your family and carers you need them now" onPress={a.onHelp} />
    </Card>
  ) : urgent.length > 0 ? (
    <Pressable key="alerts-first" onPress={a.onAlerts} testID="urgent-banner" style={[styles.banner, { backgroundColor: c.sos }]}>
      <Text style={styles.bannerText} maxFontSizeMultiplier={2}>{urgent.length === 1 ? '1 alert needs someone' : `${urgent.length} alerts need someone`}. Tap to see.</Text>
      {urgent[0] ? <Text style={styles.bannerDetail} maxFontSizeMultiplier={2}>{alertTitle(urgent[0])}{urgent[0].opened_at ? ` · ${timeAgo(urgent[0].opened_at)}` : ''}</Text> : null}
      {urgent[0] && d.experience === 'family' ? (
        <BigButton label="I'm on my way" color="#FFFFFF" textColor={c.sos} testID="home-on-my-way" hint="Tells everyone you are going" onPress={() => a.onOnMyWay(urgent[0])} />
      ) : null}
    </Pressable>
  ) : (
    <Card key="alerts-first" testID="card-alerts"><Text style={{ color: c.ok, fontSize: 16 }} maxFontSizeMultiplier={2}>No open alerts.</Text></Card>
  );

  const render = (key: string): React.ReactNode => {
    switch (key) {
      case 'help': case 'alerts': return null; // drawn first, above
      case 'next_visit': { const v = nextVisit(d.visits); return (
        <Card key={key} testID="card-next-visit"><Text style={[styles.h, { color: c.text }]}>Next visit</Text><Body>{nextVisitWords(v)}</Body>
          {v?.purpose ? <Muted>{v.purpose}</Muted> : null}</Card>); }
      case 'reminders': { const open = openReminders(d.reminders).slice(0, 4); return (
        <Card key={key} testID="card-reminders"><Text style={[styles.h, { color: c.text }]}>Reminders</Text>
          {open.length === 0 ? <Muted>Nothing waiting. Say “remind me to…” to IRA.</Muted> : open.map((r) => (
            <View key={r.id} style={styles.row}>
              <Body style={{ flex: 1 }}>{reminderTitle(r)} · {REMINDER_WORDS[r.status]}</Body>
              <Pressable onPress={() => a.onReminder(r, 'confirm')} accessibilityRole="button" accessibilityLabel={`Done: ${r.text}`} style={[styles.small, { borderColor: c.border }]}><Text style={{ color: c.ok }}>Done</Text></Pressable>
              <Pressable onPress={() => a.onReminder(r, 'snooze')} accessibilityRole="button" accessibilityLabel={`Later: ${r.text}`} style={[styles.small, { borderColor: c.border }]}><Text style={{ color: c.notice }}>Later</Text></Pressable>
            </View>))}</Card>); }
      case 'talk': return (<Card key={key} testID="card-talk"><Text style={[styles.h, { color: c.text }]}>Talk to IRA</Text><Body>Say anything: company, a reminder, what is on today.</Body>
        <BigButton label="Talk to IRA" color={c.notice} testID="home-talk" onPress={a.onTalk} /></Card>);
      case 'sharing': return (<Card key={key} testID="card-sharing"><Text style={[styles.h, { color: c.text }]}>Who sees what</Text>
        <Body>Family see alerts and summaries. Your own words are shown only with your recorded consent. You can change what IRA remembers.</Body>
        <BigButton label="Sharing and privacy" color={c.muted} testID="home-sharing" onPress={a.onSettings} /></Card>);
      case 'person': return (<Card key={key} testID="card-person"><Text style={[styles.h, { color: c.text }]}>{name || 'The person'}</Text>
        {d.lastCheckin ? <Body>Last check-in: {d.lastCheckin.status} · {timeAgo(d.lastCheckin.scheduled_at)}{d.lastCheckin.answer_summary ? ` · ${d.lastCheckin.answer_summary}` : ''}</Body> : <Muted>No check-in yet.</Muted>}
        {d.people.length > 1 ? (<View style={styles.rowWrap}>{d.people.map((p) => (
          <Pressable key={p.id} testID={`switch-${p.id}`} accessibilityRole="button" accessibilityLabel={`Switch to ${p.preferred_name || p.display_name}`} accessibilityState={{ selected: p.id === d.person?.id }}
            onPress={() => a.onSwitchPerson(p)} style={[styles.chip, { borderColor: c.border, backgroundColor: p.id === d.person?.id ? c.notice : c.card }]}>
            <Text style={{ color: p.id === d.person?.id ? '#fff' : c.text }}>{p.preferred_name || p.display_name}</Text></Pressable>))}</View>) : null}
        <BigButton label="Open the care record" color={c.notice} testID="home-care" onPress={a.onCare} /></Card>);
      case 'updates': { const recent = sortAlerts(d.alerts).filter((x) => x.status === 'resolved').slice(0, 3); return (
        <Card key={key} testID="card-updates"><Text style={[styles.h, { color: c.text }]}>Updates</Text>
          {recent.length === 0 ? <Muted>Nothing new.</Muted> : recent.map((x) => <Body key={x.id}>{alertTitle(x)} · resolved {timeAgo(x.resolved_at)}</Body>)}</Card>); }
      case 'watch': return (<Card key={key} testID="card-watch"><Text style={[styles.h, { color: c.text }]}>The watch</Text><Body>{deviceSummary(d.device)}</Body>
        <Muted>Last sync {timeAgo(d.device?.last_sync_at)}</Muted></Card>);
      case 'visits': { const today = todaysVisits(d.visits); return (
        <Card key={key} testID="card-visits"><Text style={[styles.h, { color: c.text }]}>Today's visits</Text>
          {today.length === 0 ? <Muted>No visits planned today.</Muted> : today.map((v) => (
            <View key={v.id} style={{ marginBottom: 8 }}>
              <Body>{(d.people.find((p) => p.id === v.person_id)?.preferred_name) || ''} · {visitTitle(v)}</Body>
              {v.status === 'planned' || v.status === 'late' || v.status === 'missed' ? <BigButton label="I've arrived" testID={`arrive-${v.id}`} onPress={() => a.onArrive(v)} /> : null}
              {v.status === 'arrived' ? <BigButton label="Leaving: record the visit" color={c.notice} testID={`depart-${v.id}`} onPress={() => a.onDepart(v)} /> : null}
            </View>))}</Card>); }
      case 'people': return (<Card key={key} testID="card-people"><Text style={[styles.h, { color: c.text }]}>People</Text>
        <View style={styles.rowWrap}>{d.people.map((p) => (
          <Pressable key={p.id} testID={`switch-${p.id}`} accessibilityRole="button" accessibilityLabel={`Open ${p.preferred_name || p.display_name}`} onPress={() => { a.onSwitchPerson(p); a.onCare(); }}
            style={[styles.chip, { borderColor: c.border, backgroundColor: c.card }]}><Text style={{ color: c.text }}>{p.preferred_name || p.display_name}</Text></Pressable>))}</View></Card>);
      case 'handover': { const last = d.visits.filter((v) => v.status === 'departed' && v.handover).sort((x, y) => (y.departed_at || '').localeCompare(x.departed_at || ''))[0]; return (
        <Card key={key} testID="card-handover"><Text style={[styles.h, { color: c.text }]}>Handover</Text>
          {last ? <Body>{last.carer_name || 'A carer'}, {timeAgo(last.departed_at)}: {last.handover}</Body> : <Muted>No handover written yet.</Muted>}</Card>); }
      default: return null;
    }
  };

  return (
    <Screen title={title}>
      <ScrollView contentContainerStyle={{ paddingBottom: 24 }}>
        {urgentFirst}
        {d.error ? <Text style={{ color: c.sos }}>{d.error}</Text> : null}
        {cards.map(render)}
      </ScrollView>
    </Screen>
  );
}

const styles = StyleSheet.create({
  banner: { borderRadius: 14, padding: 16, marginBottom: 10 },
  bannerText: { color: '#fff', fontSize: 18, fontWeight: '700' },
  bannerDetail: { color: '#fff', fontSize: 16, marginTop: 4 },
  h: { fontSize: 18, fontWeight: '600', marginBottom: 6 },
  row: { flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 6 },
  rowWrap: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginVertical: 8 },
  small: { borderWidth: 1, borderRadius: 999, paddingHorizontal: 10, paddingVertical: 8, minHeight: 40, justifyContent: 'center' },
  chip: { borderWidth: 1, borderRadius: 999, paddingHorizontal: 12, paddingVertical: 8, minHeight: 40, justifyContent: 'center' },
});
