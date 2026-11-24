import React, { useState } from 'react';
import { FlatList, Linking, StyleSheet, Text, View } from 'react-native';
import type { AccessNotes, Alert, AlertOutcome, Person } from '../api/types';
import { alertTitle, awaitingArrival, needsSomeone, showCall999, sortAlerts, whereabouts } from '../model/alerts';
import { timeAgo } from '../model/format';
import { BigButton, Card, Muted, Screen , useColors, type Palette } from '../ui';

export function AlertsScreen({ alerts, people, onAcknowledge, onArrived, onResolve, onBack, onCall999, onAccessNotes }: {
  alerts: Alert[]; people: Person[];
  onAcknowledge: (a: Alert) => Promise<void>; onResolve: (a: Alert, outcome: AlertOutcome) => Promise<void>;
  onArrived?: (a: Alert) => Promise<void>;
  onBack: () => void; onCall999: () => void;
  onAccessNotes?: (a: Alert) => Promise<AccessNotes>;
}) {
  const colors = useColors();
  const styles = makeStyles(colors);
  const [busy, setBusy] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, AccessNotes | 'refused'>>({});
  const personOf = (id: string) => people.find((p) => p.id === id);
  const nameOf = (id: string) => people.find((p) => p.id === id)?.preferred_name || people.find((p) => p.id === id)?.display_name || 'the person';
  const colour = (a: Alert) => (a.severity === 'sos' ? colors.sos : a.severity === 'urgent' ? colors.urgent : colors.notice);
  return (
    <Screen title="Alerts">
      <FlatList
        data={sortAlerts(alerts)}
        keyExtractor={(a) => a.id}
        ListEmptyComponent={<Muted>No open alerts.</Muted>}
        renderItem={({ item }) => (
          <Card>
            <Text style={[styles.kind, { color: colour(item) }]}>{alertTitle(item)}</Text>
            <Text style={styles.who}>{nameOf(item.person_id)} · {timeAgo(item.opened_at)}</Text>
            {typeof item.details?.summary === 'string' ? <Muted>{item.details.summary as string}</Muted> : null}
            {item.severity !== 'notice' ? (() => {
              const w = whereabouts(item, personOf(item.person_id));
              return (
                <View style={styles.where} testID={`where-${item.id}`}>
                  <Text style={styles.whereText}>{w.line}</Text>
                  {w.mapLink ? <Text style={styles.link} onPress={() => Linking.openURL(w.mapLink!)}>Open map</Text> : null}
                  {personOf(item.person_id)?.emergency_summary ? <Muted>{personOf(item.person_id)!.emergency_summary}</Muted> : null}
                </View>
              );
            })() : null}
            {item.severity !== 'notice' && item.status !== 'resolved' && onAccessNotes ? (
              notes[item.id] === undefined ? (
                <BigButton label="Show access notes (key safe, door, pets)" color={colors.muted} testID={`notes-${item.id}`}
                  onPress={async () => { try { setNotes({ ...notes, [item.id]: await onAccessNotes(item) }); } catch { setNotes({ ...notes, [item.id]: 'refused' }); } }} />
              ) : notes[item.id] === 'refused' ? (
                <Muted>Access notes are shown only to the people called first and second while the alert is open.</Muted>
              ) : (
                <View style={styles.where}>
                  <Text style={styles.whereText}>{(notes[item.id] as AccessNotes).access_notes || 'No access notes recorded.'}</Text>
                  <Muted>This view has been recorded in the access log.</Muted>
                </View>
              )
            ) : null}
            {item.status === 'acknowledged' ? <Text style={{ color: colors.ok, marginTop: 6 }}>{awaitingArrival(item) ? 'Someone is on their way.' : 'Someone has arrived; the watch has been told.'}</Text> : null}
            {awaitingArrival(item) && onArrived ? (
              <BigButton label={busy === item.id ? 'Sending…' : "I've arrived"} disabled={busy !== null} testID={`arrived-${item.id}`}
                onPress={async () => { setBusy(item.id); try { await onArrived(item); } finally { setBusy(null); } }} />
            ) : null}
            {showCall999(item) ? (
              <View style={styles.callBox}>
                <Text style={styles.callText}>Nobody has acknowledged this alert. If you cannot reach anyone, call 999 now.</Text>
                <BigButton label="Call 999" color={colors.sos} onPress={onCall999} testID={`call999-${item.id}`} />
              </View>
            ) : null}
            {needsSomeone(item) ? (
              <BigButton
                label={busy === item.id ? 'Sending…' : "I'm on my way"}
                disabled={busy !== null}
                testID={`ack-${item.id}`}
                onPress={async () => { setBusy(item.id); try { await onAcknowledge(item); } finally { setBusy(null); } }}
              />
            ) : null}
            {item.status === 'acknowledged' && item.follow_up_due_at ? <Muted>{item.follow_up_asked_at ? 'Have you arrived? The alert is raised again if nobody does.' : `Arrival or outcome expected by ${new Date(item.follow_up_due_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}.`}</Muted> : null}
            {item.status === 'acknowledged' || item.severity === 'notice' ? (
              <>
                <BigButton
                  label="Resolved: all well"
                  color={colors.muted}
                  disabled={busy !== null}
                  testID={`resolve-${item.id}`}
                  onPress={async () => { setBusy(item.id); try { await onResolve(item, 'all_well'); } finally { setBusy(null); } }}
                />
                {item.severity !== 'notice' ? (
                  <BigButton
                    label="Resolved: needed help"
                    color={colors.muted}
                    disabled={busy !== null}
                    testID={`resolve-help-${item.id}`}
                    onPress={async () => { setBusy(item.id); try { await onResolve(item, 'needed_help'); } finally { setBusy(null); } }}
                  />
                ) : null}
              </>
            ) : null}
          </Card>
        )}
      />
      <BigButton label="Back" color={colors.muted} onPress={onBack} />
    </Screen>
  );
}

const makeStyles = (colors: Palette) => StyleSheet.create({
  kind: { fontSize: 18, fontWeight: '700' },
  where: { marginTop: 8, padding: 8, borderRadius: 8, backgroundColor: '#f4f4f0' },
  whereText: { fontSize: 15 },
  link: { color: '#1a5fb4', marginTop: 4, textDecorationLine: 'underline' },
  who: { fontSize: 15, color: colors.text, marginTop: 2, marginBottom: 4 },
  callBox: { backgroundColor: '#FDECEA', borderRadius: 10, padding: 10, marginTop: 8 },
  callText: { color: colors.sos, fontWeight: '600' },
});
