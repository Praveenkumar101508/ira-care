import React from 'react';
import { FlatList, StyleSheet, Text } from 'react-native';
import { BigButton, Muted, useColors, useTextScale } from '../ui';

/**
 * The app's third-party notices, bundled with it: mobile/assets/legal/THIRD_PARTY_NOTICES.json
 * is docs/legal/notices/phone-app.txt as written by `scripts/supply_chain.py render` (Metro
 * bundles JSON as a module, so the text is inside the app, not fetched).
 */
export interface BundledNotices { artefact: string; source: string; text: string }
// eslint-disable-next-line @typescript-eslint/no-var-requires
export const NOTICES: BundledNotices = require('../../assets/legal/THIRD_PARTY_NOTICES.json');

/** The notices in paragraphs, so a long file scrolls as a list rather than one huge text. */
export function noticeParagraphs(text: string): string[] {
  return text.split(/\n{2,}/).map((p) => p.replace(/\s+$/, '')).filter((p) => p.length > 0);
}

/** Open-source licences: every third-party package in this app, its copyright and licence text. */
export function LicencesView({ notices = NOTICES, onBack }: { notices?: BundledNotices; onBack: () => void }) {
  const c = useColors();
  const scale = useTextScale();
  const paragraphs = noticeParagraphs(notices.text);
  return (
    <FlatList
      testID="licences"
      accessibilityLabel="Third-party notices"
      data={paragraphs}
      keyExtractor={(_, i) => String(i)}
      initialNumToRender={12}
      ListHeaderComponent={<Muted>This app is built with open-source software. These are the third-party packages inside it, their copyright lines and the full text of every licence they come under.</Muted>}
      renderItem={({ item }) => <Text selectable style={[styles.p, { color: c.text, fontSize: 13 * scale }]}>{item}</Text>}
      ListFooterComponent={<BigButton label="Back to settings" color={c.muted} testID="licences-back" onPress={onBack} />}
      contentContainerStyle={{ paddingBottom: 24 }}
    />
  );
}

const styles = StyleSheet.create({ p: { marginBottom: 10 } });
