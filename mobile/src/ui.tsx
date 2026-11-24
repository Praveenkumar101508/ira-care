import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { Wallpaper, useTheme } from './appearance/ThemeContext';

/** The palette as the screens name it. `sos` is the fixed Help colour of the current theme. */
export interface Palette {
  bg: string; card: string; text: string; muted: string; sos: string; urgent: string; notice: string; ok: string; border: string;
}

/** The light palette, for anything built before the theme existed; prefer `useColors()`. */
export const colors: Palette = {
  bg: '#FAF8F4', card: '#FFFFFF', text: '#1F2A24', muted: '#5B6B63',
  sos: '#B3261E', urgent: '#B35C00', notice: '#2F5D8A', ok: '#2E7D32', border: '#E1DED8',
};

/** The current theme's palette: follows the person's appearance (dark, contrast, wallpaper). */
export function useColors(): Palette {
  const t = useTheme();
  return { bg: t.colors.bg, card: t.colors.card, text: t.colors.text, muted: t.colors.muted, sos: t.colors.help,
           urgent: t.colors.urgent, notice: t.colors.notice, ok: t.colors.ok, border: t.colors.border };
}

/** Font sizes scaled by the person's text-size setting (the system setting still applies on top). */
export function useTextScale(): number {
  return useTheme().textScale;
}

export function Screen({ title, children, plain = false }: { title: string; children: React.ReactNode; plain?: boolean }) {
  const c = useColors();
  const scale = useTextScale();
  const body = (
    <View style={[styles.screen, plain ? { backgroundColor: c.bg } : null]}>
      <Text style={[styles.title, { color: c.text, fontSize: 24 * scale }]} accessibilityRole="header" maxFontSizeMultiplier={2}>{title}</Text>
      {children}
    </View>
  );
  return plain ? body : <Wallpaper>{body}</Wallpaper>;
}

export function Card({ children, testID }: { children: React.ReactNode; testID?: string }) {
  const c = useColors();
  return <View testID={testID} style={[styles.card, { backgroundColor: c.card, borderColor: c.border }]}>{children}</View>;
}

export function BigButton({ label, onPress, color, disabled = false, testID, hint }: {
  label: string; onPress: () => void; color?: string; disabled?: boolean; testID?: string; hint?: string;
}) {
  const c = useColors();
  const scale = useTextScale();
  return (
    <Pressable
      testID={testID}
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityHint={hint}
      accessibilityState={{ disabled }}
      onPress={onPress}
      disabled={disabled}
      style={({ pressed }) => [styles.button, { backgroundColor: color ?? c.ok, opacity: disabled ? 0.5 : pressed ? 0.85 : 1 }]}
    >
      <Text style={[styles.buttonText, { fontSize: 18 * scale }]} maxFontSizeMultiplier={2}>{label}</Text>
    </Pressable>
  );
}

export function Muted({ children }: { children: React.ReactNode }) {
  const c = useColors();
  const scale = useTextScale();
  return <Text style={[styles.muted, { color: c.muted, fontSize: 14 * scale }]} maxFontSizeMultiplier={2.5}>{children}</Text>;
}

/** Body text in the theme's colour and the person's size. */
export function Body({ children, style, testID }: { children: React.ReactNode; style?: object; testID?: string }) {
  const c = useColors();
  const scale = useTextScale();
  return <Text testID={testID} style={[{ color: c.text, fontSize: 16 * scale, marginBottom: 4 }, style]} maxFontSizeMultiplier={2.5}>{children}</Text>;
}

const styles = StyleSheet.create({
  screen: { flex: 1, padding: 16, paddingTop: 48 },
  title: { fontWeight: '700', marginBottom: 12 },
  card: { borderRadius: 14, padding: 14, marginBottom: 10, borderWidth: 1 },
  button: { minHeight: 56, borderRadius: 14, paddingVertical: 16, alignItems: 'center', marginTop: 8 },
  buttonText: { color: '#fff', fontWeight: '700' },
  muted: {},
});
