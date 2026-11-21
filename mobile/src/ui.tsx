import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

export const colors = {
  bg: '#FAF8F4', card: '#FFFFFF', text: '#1F2A24', muted: '#5B6B63',
  sos: '#B3261E', urgent: '#B35C00', notice: '#2F5D8A', ok: '#2E7D32', border: '#E1DED8',
};

export function Screen({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={styles.screen}>
      <Text style={styles.title}>{title}</Text>
      {children}
    </View>
  );
}

export function Card({ children }: { children: React.ReactNode }) {
  return <View style={styles.card}>{children}</View>;
}

export function BigButton({ label, onPress, color = colors.ok, disabled = false, testID }: {
  label: string; onPress: () => void; color?: string; disabled?: boolean; testID?: string;
}) {
  return (
    <Pressable
      testID={testID}
      accessibilityRole="button"
      onPress={onPress}
      disabled={disabled}
      style={({ pressed }) => [styles.button, { backgroundColor: color, opacity: disabled ? 0.5 : pressed ? 0.85 : 1 }]}
    >
      <Text style={styles.buttonText}>{label}</Text>
    </Pressable>
  );
}

export function Muted({ children }: { children: React.ReactNode }) {
  return <Text style={styles.muted}>{children}</Text>;
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg, padding: 16, paddingTop: 48 },
  title: { fontSize: 24, fontWeight: '700', color: colors.text, marginBottom: 12 },
  card: { backgroundColor: colors.card, borderRadius: 14, padding: 14, marginBottom: 10, borderWidth: 1, borderColor: colors.border },
  button: { borderRadius: 14, paddingVertical: 16, alignItems: 'center', marginTop: 8 },
  buttonText: { color: '#fff', fontSize: 18, fontWeight: '700' },
  muted: { color: colors.muted, fontSize: 14 },
});
