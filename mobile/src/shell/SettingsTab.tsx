import React, { useState } from 'react';
import { Platform, Pressable, ScrollView, StyleSheet, Switch, Text, View } from 'react-native';
import type { CircleMember, ConsentList, Device, Person } from '../api/types';
import { BUILT_IN_IMAGES, WALLPAPER_COLOURS, moveCard, orderCards, type Appearance } from '../appearance/theme';
import { HOME_CARDS, type Experience } from '../model/experience';
import { deviceSummary, timeAgo } from '../model/format';
import { LicencesView } from '../screens/LicencesScreen';
import { BigButton, Body, Card, Muted, Screen, useColors } from '../ui';

export type SettingsSection = 'appearance' | 'accessibility' | 'notifications' | 'contacts' | 'privacy' | 'watch' | 'account' | 'licences';
const SECTIONS: { key: SettingsSection; title: string; words: string }[] = [
  { key: 'appearance', title: 'Appearance', words: 'Wallpaper, light or dark, the order of your home cards' },
  { key: 'accessibility', title: 'Accessibility', words: 'Text size, contrast, less motion' },
  { key: 'notifications', title: 'Notifications', words: 'What this phone is told, and whether it is registered' },
  { key: 'contacts', title: 'Trusted contacts', words: 'The care circle and who is called first' },
  { key: 'privacy', title: 'Privacy', words: 'What is shared, with whose consent, and what IRA remembers' },
  { key: 'watch', title: 'The watch', words: 'Pairing, battery, sync; guided set-up' },
  { key: 'account', title: 'Account', words: 'Who you are signed in as, and signing out' },
  { key: 'licences', title: 'Open-source licences', words: 'The software inside this app and the licences it comes under' },
];

export interface SettingsProps {
  appearance: Appearance; onAppearance: (a: Appearance) => void; experience: Experience;
  person?: Person; device?: Device; circle: CircleMember[]; consents: ConsentList | null;
  pushRegistered: boolean | null; onRegisterPush: () => void;
  username: string; server: string; onSignOut: () => void; onPairWatch: () => void; onOpenCare: () => void;
  onPickPhoto: () => Promise<string | null>;
}

export function SettingsTab(p: SettingsProps) {
  const c = useColors();
  const [section, setSection] = useState<SettingsSection | null>(null);
  const a = p.appearance;
  const set = (patch: Partial<Appearance>) => p.onAppearance({ ...a, ...patch });
  const cards = HOME_CARDS[p.experience];
  const order = orderCards(cards, a.card_order).map((x) => x.key);

  if (section === null) {
    return (
      <Screen title="Settings">
        <ScrollView>
          {SECTIONS.map((s) => (
            <Pressable key={s.key} testID={`settings-${s.key}`} accessibilityRole="button" onPress={() => setSection(s.key)}>
              <Card><Text style={[styles.h, { color: c.text }]}>{s.title}</Text><Muted>{s.words}</Muted></Card>
            </Pressable>
          ))}
        </ScrollView>
      </Screen>
    );
  }

  // The notices are long: their own scrolling list, not inside the section's ScrollView.
  if (section === 'licences') {
    return <Screen title="Open-source licences"><LicencesView onBack={() => setSection(null)} /></Screen>;
  }

  const back = <BigButton label="Back to settings" color={c.muted} testID="settings-back" onPress={() => setSection(null)} />;
  return (
    <Screen title={SECTIONS.find((s) => s.key === section)!.title}>
      <ScrollView contentContainerStyle={{ paddingBottom: 24 }}>
        {section === 'appearance' && (
          <>
            <Card>
              <Text style={[styles.h, { color: c.text }]}>Wallpaper</Text>
              <Muted>A colour, one of our pictures, or a photo of your own. Words stay readable over any of them.</Muted>
              <View style={styles.rowWrap}>
                {WALLPAPER_COLOURS.map((col) => (
                  <Pressable key={col} testID={`wallpaper-colour-${col.slice(1)}`} accessibilityRole="button" accessibilityLabel={`Colour ${col}`}
                    accessibilityState={{ selected: a.wallpaper_kind === 'colour' && a.wallpaper_value === col }}
                    onPress={() => set({ wallpaper_kind: 'colour', wallpaper_value: col })}
                    style={[styles.swatch, { backgroundColor: col, borderColor: a.wallpaper_kind === 'colour' && a.wallpaper_value === col ? c.notice : c.border, borderWidth: a.wallpaper_kind === 'colour' && a.wallpaper_value === col ? 3 : 1 }]} />
                ))}
              </View>
              <View style={styles.rowWrap}>
                {Object.entries(BUILT_IN_IMAGES).map(([key, img]) => (
                  <Pressable key={key} testID={`wallpaper-image-${key}`} accessibilityRole="button" accessibilityLabel={`Picture: ${img.name}`}
                    accessibilityState={{ selected: a.wallpaper_kind === 'image' && a.wallpaper_value === key }}
                    onPress={() => set({ wallpaper_kind: 'image', wallpaper_value: key })}
                    style={[styles.chip, { borderColor: a.wallpaper_kind === 'image' && a.wallpaper_value === key ? c.notice : c.border, backgroundColor: img.from }]}>
                    <Text style={{ color: '#1F2A24' }}>{img.name}</Text>
                  </Pressable>
                ))}
              </View>
              <BigButton label={a.wallpaper_kind === 'photo' ? 'Change my photo' : 'Use a photo of my own'} color={c.notice} testID="wallpaper-photo"
                onPress={() => { void p.onPickPhoto().then((uri) => { if (uri) set({ wallpaper_kind: 'photo', wallpaper_value: uri }); }); }} />
              {a.wallpaper_kind === 'photo' ? <Muted>Your photo is kept with your account so your other devices show it too; it is never shown to anyone else.</Muted> : null}
            </Card>
            <Card>
              <Text style={[styles.h, { color: c.text }]}>Light or dark</Text>
              <View style={styles.rowWrap}>
                {(['system', 'light', 'dark'] as const).map((m) => (
                  <Pressable key={m} testID={`mode-${m}`} accessibilityRole="button" accessibilityLabel={m === 'system' ? 'Follow the phone' : m === 'light' ? 'Light' : 'Dark'} accessibilityState={{ selected: a.mode === m }}
                    onPress={() => set({ mode: m })} style={[styles.chip, { borderColor: c.border, backgroundColor: a.mode === m ? c.notice : c.card }]}>
                    <Text style={{ color: a.mode === m ? '#fff' : c.text }}>{m === 'system' ? 'Follow the phone' : m === 'light' ? 'Light' : 'Dark'}</Text>
                  </Pressable>
                ))}
              </View>
            </Card>
            <Card>
              <Text style={[styles.h, { color: c.text }]}>Order of your home cards</Text>
              <Muted>{p.experience === 'wearer' ? 'Help always stays at the top.' : 'Alerts that need someone always stay at the top.'}</Muted>
              {order.map((key, i) => (
                <View key={key} style={styles.row}>
                  <Body style={{ flex: 1 }}>{cards.find((x) => x.key === key)?.title ?? key}</Body>
                  <Pressable testID={`card-up-${key}`} accessibilityRole="button" accessibilityLabel={`Move ${cards.find((x) => x.key === key)?.title} up`} disabled={i === 0}
                    onPress={() => set({ card_order: moveCard(a.card_order, cards.map((x) => x.key), key, -1) })} style={[styles.small, { borderColor: c.border, opacity: i === 0 ? 0.4 : 1 }]}><Text style={{ color: c.text }}>Up</Text></Pressable>
                  <Pressable testID={`card-down-${key}`} accessibilityRole="button" accessibilityLabel={`Move ${cards.find((x) => x.key === key)?.title} down`} disabled={i === order.length - 1}
                    onPress={() => set({ card_order: moveCard(a.card_order, cards.map((x) => x.key), key, 1) })} style={[styles.small, { borderColor: c.border, opacity: i === order.length - 1 ? 0.4 : 1 }]}><Text style={{ color: c.text }}>Down</Text></Pressable>
                </View>
              ))}
            </Card>
            {back}
          </>
        )}
        {section === 'accessibility' && (
          <>
            <Card>
              <Text style={[styles.h, { color: c.text }]}>Text size</Text>
              <View style={styles.rowWrap}>
                {[{ v: 1.0, w: 'Normal' }, { v: 1.2, w: 'Large' }, { v: 1.45, w: 'Larger' }, { v: 1.8, w: 'Largest' }].map((o) => (
                  <Pressable key={o.v} testID={`text-${o.w.toLowerCase()}`} accessibilityRole="button" accessibilityLabel={`Text size ${o.w}`} accessibilityState={{ selected: Math.abs(a.text_scale - o.v) < 0.01 }}
                    onPress={() => set({ text_scale: o.v })} style={[styles.chip, { borderColor: c.border, backgroundColor: Math.abs(a.text_scale - o.v) < 0.01 ? c.notice : c.card }]}>
                    <Text style={{ color: Math.abs(a.text_scale - o.v) < 0.01 ? '#fff' : c.text, fontSize: 14 * o.v }}>{o.w}</Text>
                  </Pressable>
                ))}
              </View>
              <Body>This is how body text will look.</Body>
            </Card>
            <Card>
              <View style={styles.row}><Body style={{ flex: 1 }}>High contrast</Body><Switch testID="contrast" accessibilityLabel="High contrast" value={a.high_contrast} onValueChange={(v) => set({ high_contrast: v })} /></View>
              <View style={styles.row}><Body style={{ flex: 1 }}>Less motion</Body><Switch testID="motion" accessibilityLabel="Less motion" value={a.reduce_motion} onValueChange={(v) => set({ reduce_motion: v })} /></View>
              <Muted>The phone's own text size, screen reader and bold text settings apply on top of these.</Muted>
            </Card>
            {back}
          </>
        )}
        {section === 'notifications' && (
          <>
            <Card>
              <Text style={[styles.h, { color: c.text }]}>This phone</Text>
              <Body>{p.pushRegistered === true ? 'This phone is registered for alerts.' : p.pushRegistered === false ? 'This phone is not registered for alerts yet.' : 'Checking…'}</Body>
              <Muted>Alerts for the people you look after reach this phone through the care server; nothing is sent through a third party's cloud other than the phone maker's delivery service.</Muted>
              <BigButton label={p.pushRegistered ? 'Register again' : 'Register this phone'} color={c.notice} testID="register-push" onPress={p.onRegisterPush} />
            </Card>
            {back}
          </>
        )}
        {section === 'contacts' && (
          <>
            <Card>
              <Text style={[styles.h, { color: c.text }]}>{p.person ? `${p.person.preferred_name || p.person.display_name}'s care circle` : 'Care circle'}</Text>
              {p.circle.length === 0 ? <Muted>No one yet.</Muted> : [...p.circle].sort((x, y) => x.tier - y.tier || x.priority - y.priority).map((m) => (
                <Body key={m.id}>{m.display_name} · {m.relationship || m.role.replace('_', ' ')} · called {m.tier === 1 ? 'first' : m.tier === 2 ? 'second' : 'last'}{m.phone ? ` · ${m.phone}` : ''}</Body>
              ))}
              <Muted>Trusted contacts are added by an administrator on the dashboard or with an invitation.</Muted>
            </Card>
            {back}
          </>
        )}
        {section === 'privacy' && (
          <>
            <Card>
              <Text style={[styles.h, { color: c.text }]}>What is shared</Text>
              {p.consents ? (
                <>
                  <Body>Family may read the person's own words: {p.consents.effective.transcripts_family ? 'yes' : 'no'}.</Body>
                  <Body>Carers may: {p.consents.effective.transcripts_carer ? 'yes' : 'no'}.</Body>
                  <Body>Kept for {p.consents.retention_months} months.</Body>
                </>
              ) : <Muted>Consents are shown once a person is selected.</Muted>}
              <Muted>Summaries are shared with the care circle; the person's words only with their recorded consent. The person or their representative can change or clear what IRA remembers, on the Care tab.</Muted>
              <BigButton label="Open the care record" color={c.notice} testID="privacy-care" onPress={p.onOpenCare} />
            </Card>
            {back}
          </>
        )}
        {section === 'watch' && (
          <>
            <Card>
              <Text style={[styles.h, { color: c.text }]}>{p.person ? `${p.person.preferred_name || p.person.display_name}'s watch` : 'The watch'}</Text>
              <Body>{deviceSummary(p.device)}</Body>
              {p.device ? <Muted>Last sync {timeAgo(p.device.last_sync_at)}</Muted> : null}
              <BigButton label={p.device ? 'Set up a new watch (step by step)' : 'Set up the watch (step by step)'} color={c.notice} testID="watch-setup" onPress={p.onPairWatch} />
            </Card>
            {back}
          </>
        )}
        {section === 'account' && (
          <>
            <Card>
              <Text style={[styles.h, { color: c.text }]}>Signed in</Text>
              <Body>{p.username}</Body>
              <Muted>{p.server}</Muted>
              <Muted>You use the app as: {p.experience === 'wearer' ? 'the person wearing the watch' : p.experience === 'carer' ? 'a carer' : 'family'}.</Muted>
              <BigButton label="Sign out" color={c.muted} testID="sign-out" onPress={p.onSignOut} />
            </Card>
            {back}
          </>
        )}
      </ScrollView>
    </Screen>
  );
}

/** Pick a photo for the wallpaper: the system picker where the app has it, a file field on the web. */
export async function pickPhoto(): Promise<string | null> {
  try {
    if (Platform.OS === 'web' && typeof document !== 'undefined') {
      return await new Promise((resolve) => {
        const input = document.createElement('input');
        input.type = 'file'; input.accept = 'image/*';
        input.onchange = () => {
          const f = input.files?.[0];
          if (!f) { resolve(null); return; }
          const reader = new FileReader();
          reader.onload = () => resolve(typeof reader.result === 'string' && reader.result.length < 480000 ? reader.result : null);
          reader.readAsDataURL(f);
        };
        input.click();
      });
    }
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const picker = require('expo-image-picker') as { launchImageLibraryAsync: (o: object) => Promise<{ canceled: boolean; assets?: { base64?: string; mimeType?: string }[] }> };
    const res = await picker.launchImageLibraryAsync({ mediaTypes: ['images'], quality: 0.4, base64: true, allowsEditing: true, aspect: [9, 16] });
    const asset = res.canceled ? undefined : res.assets?.[0];
    if (!asset?.base64) return null;
    const uri = `data:${asset.mimeType || 'image/jpeg'};base64,${asset.base64}`;
    return uri.length < 480000 ? uri : null;
  } catch {
    return null;
  }
}

const styles = StyleSheet.create({
  h: { fontSize: 18, fontWeight: '600', marginBottom: 6 },
  row: { flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 6 },
  rowWrap: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginVertical: 8 },
  swatch: { width: 44, height: 44, borderRadius: 22 },
  chip: { borderWidth: 1, borderRadius: 999, paddingHorizontal: 12, paddingVertical: 8, minHeight: 40, justifyContent: 'center' },
  small: { borderWidth: 1, borderRadius: 999, paddingHorizontal: 12, paddingVertical: 8, minHeight: 40, justifyContent: 'center' },
});

