import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { TABS, TAB_WORDS, type Tab } from '../model/experience';
import { useColors, useTextScale } from '../ui';

/** The four tabs: Home, Care, Alerts, Settings. Large targets, a badge for alerts that need someone. */
export function TabBar({ tab, onTab, alertCount }: { tab: Tab; onTab: (t: Tab) => void; alertCount: number }) {
  const c = useColors();
  const scale = useTextScale();
  return (
    <View style={[styles.bar, { backgroundColor: c.card, borderTopColor: c.border }]} accessibilityRole="tablist">
      {TABS.map((t) => {
        const on = t === tab;
        // With a badge, the name is the words on screen ("Alerts", "2"), so a person using voice control
        // can say what they see (WCAG 2.5.3; axe on the web build, round 10); the hint says what the number is.
        const badge = t === 'alerts' && alertCount > 0;
        const label = badge ? undefined : TAB_WORDS[t];
        return (
          <Pressable key={t} testID={`tab-${t}`} accessibilityRole="tab" accessibilityLabel={label}
            accessibilityHint={badge ? `${alertCount} need someone` : undefined} accessibilityState={{ selected: on }}
            onPress={() => onTab(t)} style={styles.item}>
            <Text style={[styles.label, { color: on ? c.notice : c.muted, fontSize: 15 * scale, fontWeight: on ? '700' : '500' }]} maxFontSizeMultiplier={1.6}>{TAB_WORDS[t]}</Text>
            {t === 'alerts' && alertCount > 0 ? (
              <View style={[styles.badge, { backgroundColor: c.sos }]}><Text style={styles.badgeText}>{alertCount}</Text></View>
            ) : null}
          </Pressable>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  bar: { flexDirection: 'row', borderTopWidth: 1, paddingBottom: 18, paddingTop: 6 },
  item: { flex: 1, minHeight: 56, alignItems: 'center', justifyContent: 'center' },
  label: {},
  badge: { position: 'absolute', top: 2, right: '22%', minWidth: 22, height: 22, borderRadius: 11, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 5 },
  badgeText: { color: '#fff', fontSize: 12, fontWeight: '700' },
});
