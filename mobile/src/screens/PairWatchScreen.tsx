import React, { useEffect, useState } from 'react';
import { ScrollView, StyleSheet, Text } from 'react-native';
import type { CareClient } from '../api/client';
import type { Device, Person } from '../api/types';
import { timeAgo } from '../model/format';
import { BigButton, Card, Muted, Screen, colors } from '../ui';

/** Pairing a watch, one step at a time, with the code and the first sync shown as they happen. */
export function PairWatchScreen({ client, person, onBack }: { client: CareClient; person: Person; onBack: () => void }) {
  const [pairing, setPairing] = useState<Device | null>(null);
  const [device, setDevice] = useState<Device | undefined>(undefined);
  const [error, setError] = useState('');
  const name = person.preferred_name || person.display_name;
  useEffect(() => {
    let stop = false;
    const poll = async () => { try { const s = await client.status(person.id); if (!stop) setDevice(s.devices[0]); } catch { /* keep the last state */ } };
    void poll();
    const t = setInterval(() => { void poll(); }, 10_000);
    return () => { stop = true; clearInterval(t); };
  }, [client, person.id]);
  const steps = [
    { title: '1. Create a pairing code', body: pairing ? `Code: ${pairing.pairing_code}. It works once and expires in 15 minutes.` : 'Tap the button below. The code is shown here.' },
    { title: '2. Install the IRA CARE watch app', body: 'On the watch, install IRA CARE and open it.' },
    { title: '3. Type the code on the watch', body: `The watch is then paired to ${name}. Nothing else needs typing.` },
    { title: '4. Allow the permissions', body: 'Body sensors, physical activity, microphone, notifications and location when asked; then "body sensors in the background" on the settings page the app opens.' },
    { title: '5. Wait for the first sync', body: device ? `Watch ${device.status}, battery ${device.battery_percent ?? '?'} %, synced ${timeAgo(device.last_sync_at)}.` : 'This line changes as soon as the watch has synced once.' },
  ];
  const done = !!device && device.status === 'paired' && !!device.last_sync_at;
  return (
    <Screen title={`Pair a watch for ${name}`}>
      <ScrollView style={{ flex: 1 }}>
        {steps.map((s, i) => (
          <Card key={s.title}><Text style={[styles.h, i === 4 && done && { color: colors.ok }]}>{s.title}</Text><Muted>{s.body}</Muted></Card>
        ))}
        {done ? <Card><Text style={{ color: colors.ok, fontSize: 18 }}>Paired and syncing. Ask IRA to check in now from the person&apos;s page and answer on the watch.</Text></Card> : null}
        {error ? <Muted>{error}</Muted> : null}
      </ScrollView>
      <BigButton label={pairing ? 'Code shown above' : 'Create a pairing code'} disabled={!!pairing} testID="pair-create"
        onPress={async () => { try { setPairing(await client.newDevice(person.id)); } catch { setError('Could not create a code. Are you a manager of this care team?'); } }} />
      <BigButton label="Back" color={colors.muted} onPress={onBack} />
    </Screen>
  );
}

const styles = StyleSheet.create({ h: { fontSize: 18, fontWeight: '700', marginBottom: 4 } });
