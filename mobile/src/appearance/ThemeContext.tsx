import React, { createContext, useContext, useMemo } from 'react';
import { Image, StyleSheet, View, useColorScheme } from 'react-native';
import { DEFAULT_APPEARANCE, resolveTheme, type Appearance, type ResolvedTheme } from './theme';

const ThemeCtx = createContext<ResolvedTheme>(resolveTheme(DEFAULT_APPEARANCE, 'light'));

export function ThemeProvider({ appearance, children }: { appearance: Appearance; children: React.ReactNode }) {
  const scheme = useColorScheme();
  const theme = useMemo(() => resolveTheme(appearance, scheme), [appearance, scheme]);
  return <ThemeCtx.Provider value={theme}>{children}</ThemeCtx.Provider>;
}

export function useTheme(): ResolvedTheme {
  return useContext(ThemeCtx);
}

/** The wallpaper behind a screen, with the scrim that keeps the words readable over it. */
export function Wallpaper({ children }: { children: React.ReactNode }) {
  const t = useTheme();
  const w = t.wallpaper;
  return (
    <View style={[styles.fill, { backgroundColor: w.kind === 'colour' ? w.value : t.colors.bg }]} testID="wallpaper" accessibilityIgnoresInvertColors>
      {w.kind === 'image' && w.gradient ? (
        <View style={styles.fill} testID="wallpaper-image">
          {/* a soft two-colour gradient drawn as bands: no image file, no native dependency */}
          {Array.from({ length: 12 }, (_, i) => (
            <View key={i} style={{ flex: 1, backgroundColor: mix(w.gradient![0], w.gradient![1], i / 11) }} />
          ))}
        </View>
      ) : null}
      {w.kind === 'photo' ? <Image source={{ uri: w.value }} style={styles.fill} resizeMode="cover" accessibilityIgnoresInvertColors testID="wallpaper-photo" /> : null}
      {t.scrimOpacity > 0 ? <View testID="scrim" style={[styles.fill, { backgroundColor: t.dark ? '#000' : '#fff', opacity: t.scrimOpacity }]} /> : null}
      <View style={styles.fill}>{children}</View>
    </View>
  );
}

function mix(a: string, b: string, k: number): string {
  const pa = parseInt(a.slice(1), 16), pb = parseInt(b.slice(1), 16);
  const ch = (s: number) => Math.round(((pa >> s) & 255) * (1 - k) + ((pb >> s) & 255) * k);
  return `#${[16, 8, 0].map((s) => ch(s).toString(16).padStart(2, '0')).join('')}`;
}

const styles = StyleSheet.create({ fill: { position: 'absolute', left: 0, right: 0, top: 0, bottom: 0 } });
