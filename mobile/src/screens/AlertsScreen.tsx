import React, { useState } from 'react';
import { FlatList, StyleSheet, Text, View } from 'react-native';
import type { Alert, Person } from '../api/types';
import { alertTitle, needsSomeone, showCall999, sortAlerts } from '../model/alerts';
import { timeAgo } from '../model/format';
import { BigButton, Card, Muted, Screen, colors } from '../ui';

export function AlertsScreen({ alerts, people, onAcknowledge, onResolve, onBack, onCall999 }: {
  alerts: Alert[]; people: Person[];
  onAcknowledge: (a: Alert) => Promise<void>; onResolve: (a: Alert) => Promise<void>;
  onBack: () => void; onCall999: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
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
            {item.status === 'acknowledged' ? <Text style={{ color: colors.ok, marginTop: 6 }}>Someone is on their way.</Text> : null}
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
            {item.status === 'acknowledged' || item.severity === 'notice' ? (
              <BigButton
                label="Resolved: all well"
                color={colors.muted}
                disabled={busy !== null}
                testID={`resolve-${item.id}`}
                onPress={async () => { setBusy(item.id); try { await onResolve(item); } finally { setBusy(null); } }}
              />
            ) : null}
          </Card>
        )}
      />
      <BigButton label="Back" color={colors.muted} onPress={onBack} />
    </Screen>
  );
}

const styles = StyleSheet.create({
  kind: { fontSize: 18, fontWeight: '700' },
  who: { fontSize: 15, color: colors.text, marginTop: 2, marginBottom: 4 },
  callBox: { backgroundColor: '#FDECEA', borderRadius: 10, padding: 10, marginTop: 8 },
  callText: { color: colors.sos, fontWeight: '600' },
});
