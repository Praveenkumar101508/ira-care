import React, { useState } from 'react';
import { StyleSheet, Text, TextInput } from 'react-native';
import { CareClient } from '../api/client';
import { BigButton, Card, Muted, Screen , useColors, type Palette } from '../ui';

/** "I have an invitation link": sign in with the invited address and join the care circle. */
export function JoinScreen({ baseUrl, onJoined, onBack }: { baseUrl: string; onJoined: (session: { accessToken: string; refreshToken: string; workspaceId: string | null; username: string }) => void; onBack: () => void }) {
  const colors = useColors();
  const styles = makeStyles(colors);
  const [link, setLink] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const token = (() => { try { return new URL(link.trim()).searchParams.get('token') ?? link.trim(); } catch { return link.trim(); } })();
  const join = async () => {
    setBusy(true); setMessage('');
    try {
      const tokens = await CareClient.login(baseUrl, email, password);
      const out = await CareClient.acceptInvitation(baseUrl, tokens.access_token, token);
      setMessage('You have joined the care circle.');
      onJoined({ accessToken: tokens.access_token, refreshToken: tokens.refresh_token, workspaceId: out.workspace_id, username: email });
    } catch (e) { setMessage(e instanceof Error ? e.message : 'Could not join.'); } finally { setBusy(false); }
  };
  return (
    <Screen title="Join a care circle">
      <Card>
        <Muted>Paste the invitation link (or its code), then sign in with the email address it was sent to. The link works once, for that address only.</Muted>
        <TextInput style={styles.input} placeholder="Invitation link" accessibilityLabel="Invitation link" autoCapitalize="none" autoCorrect={false} spellCheck={false} keyboardType="url" value={link} onChangeText={setLink} testID="join-link" />
        <TextInput style={styles.input} placeholder="Email" accessibilityLabel="Email" autoCapitalize="none" autoCorrect={false} spellCheck={false} autoComplete="email" textContentType="emailAddress" keyboardType="email-address" value={email} onChangeText={setEmail} testID="join-email" />
        <TextInput style={styles.input} placeholder="Password" accessibilityLabel="Password" secureTextEntry autoCorrect={false} autoComplete="new-password" textContentType="newPassword" value={password} onChangeText={setPassword} testID="join-password" />
        {message ? <Text style={{ color: message.startsWith('You have') ? colors.ok : colors.sos, marginTop: 8 }}>{message}</Text> : null}
      </Card>
      <BigButton label={busy ? 'Joining…' : 'Sign in and join'} disabled={busy || !token || !email || !password} onPress={() => { void join(); }} testID="join-go" />
      <BigButton label="Back" color={colors.muted} onPress={onBack} />
    </Screen>
  );
}

const makeStyles = (colors: Palette) => StyleSheet.create({ input: { borderWidth: 1, borderColor: '#bbb', borderRadius: 10, padding: 12, fontSize: 18, backgroundColor: '#fff', marginTop: 8 } });
