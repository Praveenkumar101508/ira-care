import React from 'react';
import { FlatList, Pressable, StyleSheet, Text, View } from 'react-native';
import type { Alert, Person } from '../api/types';
import { needsSomeone, sortAlerts } from '../model/alerts';
import { BigButton, Card, Muted, Screen , useColors, type Palette } from '../ui';

export function HomeScreen({ people, alerts, workspaceName, onOpenPerson, onOpenAlerts, onRefresh, onSignOut, error }: {
  people: Person[]; alerts: Alert[]; workspaceName: string; error: string;
  onOpenPerson: (p: Person) => void; onOpenAlerts: () => void; onRefresh: () => void; onSignOut: () => void;
}) {
  const colors = useColors();
  const styles = makeStyles(colors);
  const urgent = sortAlerts(alerts).filter(needsSomeone);
  return (
    <Screen title={workspaceName || 'Care circle'}>
      {urgent.length > 0 ? (
        <Pressable onPress={onOpenAlerts} testID="urgent-banner" style={styles.banner}>
          <Text style={styles.bannerText}>
            {urgent.length === 1 ? '1 alert needs someone' : `${urgent.length} alerts need someone`}. Tap to see.
          </Text>
        </Pressable>
      ) : (
        <Card><Text style={{ color: colors.ok, fontSize: 16 }}>No open alerts.</Text></Card>
      )}
      {error ? <Text style={{ color: colors.sos }}>{error}</Text> : null}
      <FlatList
        data={people}
        keyExtractor={(p) => p.id}
        refreshing={false}
        onRefresh={onRefresh}
        renderItem={({ item }) => (
          <Pressable onPress={() => onOpenPerson(item)} testID={`person-${item.id}`}>
            <Card>
              <Text style={styles.name}>{item.preferred_name || item.display_name}</Text>
              <Muted>{item.status === 'active' ? 'Being looked after' : item.status}</Muted>
            </Card>
          </Pressable>
        )}
        ListEmptyComponent={<Muted>No one is set up yet. A carer adds people from the dashboard.</Muted>}
      />
      <View style={styles.row}>
        <BigButton label="Alerts" onPress={onOpenAlerts} color={colors.notice} />
        <BigButton label="Sign out" onPress={onSignOut} color={colors.muted} />
      </View>
    </Screen>
  );
}

const makeStyles = (colors: Palette) => StyleSheet.create({
  banner: { backgroundColor: colors.sos, borderRadius: 14, padding: 16, marginBottom: 10 },
  bannerText: { color: '#fff', fontSize: 18, fontWeight: '700' },
  name: { fontSize: 20, fontWeight: '600', color: colors.text },
  row: { flexDirection: 'row', gap: 10, justifyContent: 'space-between' },
});
