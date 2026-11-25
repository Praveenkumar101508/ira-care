import React, { useEffect, useMemo, useState } from 'react';
import { Linking, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import type { Person, TalkReply } from '../api/types';
import { telLink } from '../model/format';
import { BigButton, Card, Muted, Screen , useColors, type Palette } from '../ui';
import { heardWords, makeListener, makeSpeaker, type Listener, type Speaker } from '../voice/voice';

/**
 * Talk to IRA from the person's own phone: company, reminders, "what's on
 * today", "did I take my tablets", "call my daughter". A call shows the
 * contact and the phone dials only when the person taps: their own action.
 */
export function TalkScreen({ person, onSend, onBack, voice }: {
  person: Person; onSend: (text: string, onSentence: (sentence: string) => void) => Promise<TalkReply>; onBack: () => void;
  /** The phone's speech, out and in (voice/voice.ts). Tests pass their own. */
  voice?: { speaker: Speaker; listener: Listener };
}) {
  const { speaker, listener } = useMemo(() => voice ?? { speaker: makeSpeaker(), listener: makeListener() }, [voice]);
  const canListen = useMemo(() => listener.available(), [listener]);
  const [aloud, setAloud] = useState(true);
  const [listening, setListening] = useState(false);
  // Leaving the screen stops IRA mid-sentence and stops listening.
  useEffect(() => () => { speaker.stop(); listener.stop(); }, [speaker, listener]);
  const colors = useColors();
  const styles = makeStyles(colors);
  const [text, setText] = useState('');
  const [log, setLog] = useState<{ who: 'you' | 'ira'; text: string }[]>([]);
  const [action, setAction] = useState<TalkReply['action']>(null);
  const [busy, setBusy] = useState(false);
  const name = person.preferred_name || person.display_name;
  const send = async (said?: string) => {
    const t = (said ?? text).trim();
    if (!t || busy) return;
    setBusy(true); setText(''); setAction(null);
    speaker.stop();
    setLog((l) => [...l, { who: 'you', text: t }]);
    try {
      // Each sentence shows, and is read aloud, the moment it arrives.
      const reply = await onSend(t, (s) => {
        setLog((l) => [...l, { who: 'ira' as const, text: s }]);
        if (aloud) speaker.speak(s, { language: 'en-GB' });
      });
      setAction(reply.action);
    } catch {
      setLog((l) => [...l, { who: 'ira', text: "I can't reach IRA right now. Please try again in a moment." }]);
    } finally { setBusy(false); }
  };
  const speak = async () => {
    if (busy || listening) return;
    speaker.stop();
    setListening(true);
    const heard = await listener.listen('en-GB');
    setListening(false);
    if ('text' in heard) { await send(heard.text); return; }
    const words = heardWords(heard);
    if (words) setLog((l) => [...l, { who: 'ira', text: words }]);
  };
  return (
    <Screen title={`Talk to IRA, ${name}`}>
      <ScrollView style={{ flex: 1 }}>
        {log.length === 0 ? <Muted>Say anything: how your day is going, what is on today, whether you took your tablets, or ask to call someone.</Muted> : null}
        {log.map((l, i) => (
          <Card key={i}><Text style={[styles.line, l.who === 'ira' && styles.ira]} testID={`talk-${l.who}-${i}`}>{l.who === 'ira' ? 'IRA: ' : 'You: '}{l.text}</Text></Card>
        ))}
        {action?.kind === 'call' && action.phone ? (
          <Card>
            <Text style={styles.line}>{action.name}</Text>
            <BigButton label={`Call ${action.name}`} testID="talk-call" onPress={() => { void Linking.openURL(telLink(action.phone!)); }} />
          </Card>
        ) : null}
        {action?.kind === 'safety_check' ? (
          <Card><Text style={[styles.line, { color: colors.sos }]}>IRA is checking you are OK. If you need help now, use the Help button on your watch or call someone.</Text></Card>
        ) : null}
      </ScrollView>
      <View style={styles.row}>
        <TextInput value={text} onChangeText={setText} placeholder="Type here…" accessibilityLabel="Type here…" style={styles.input} testID="talk-input"
          onSubmitEditing={() => { void send(); }} editable={!busy} />
        <BigButton label={busy ? '…' : 'Send'} disabled={busy} testID="talk-send" onPress={() => { void send(); }} />
      </View>
      <View style={styles.row}>
        {canListen ? (
          <BigButton label={listening ? 'Listening…' : 'Speak'} disabled={busy || listening} testID="talk-speak"
            hint="Say what you want to tell IRA; it is sent when you stop speaking" onPress={() => { void speak(); }} />
        ) : null}
        <BigButton label={aloud ? 'Read aloud: on' : 'Read aloud: off'} color={colors.muted} testID="talk-aloud"
          onPress={() => { if (aloud) speaker.stop(); setAloud(!aloud); }} />
      </View>
      <BigButton label="Back" color={colors.muted} onPress={() => { speaker.stop(); listener.stop(); onBack(); }} />
    </Screen>
  );
}

const makeStyles = (colors: Palette) => StyleSheet.create({
  line: { fontSize: 18 },
  ira: { color: '#1b4d3e' },
  row: { flexDirection: 'row', alignItems: 'center', gap: 8, marginVertical: 8 },
  input: { flex: 1, borderWidth: 1, borderColor: '#bbb', borderRadius: 10, padding: 12, fontSize: 18, backgroundColor: '#fff' },
});
