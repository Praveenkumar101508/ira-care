import React, { useState } from 'react';
import { StyleSheet, Text, TextInput, View } from 'react-native';
import { ApiError, CareClient } from '../api/client';
import type { Workspace } from '../api/types';
import { BigButton, Card, Muted, Screen, colors } from '../ui';

export interface LoginResult {
  baseUrl: string; accessToken: string; refreshToken: string; username: string;
  workspaces: Workspace[];
}

export function LoginScreen({ defaultBaseUrl, onLoggedIn }: { defaultBaseUrl: string; onLoggedIn: (r: LoginResult) => void }) {
  const [baseUrl, setBaseUrl] = useState(defaultBaseUrl);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true); setError('');
    try {
      const tokens = await CareClient.login(baseUrl.trim().replace(/\/$/, ''), username.trim(), password);
      const client = new CareClient({ baseUrl: baseUrl.trim().replace(/\/$/, ''), accessToken: tokens.access_token, workspaceId: null });
      const list = await client.workspaces();
      onLoggedIn({ baseUrl: baseUrl.trim().replace(/\/$/, ''), accessToken: tokens.access_token,
        refreshToken: tokens.refresh_token, username: username.trim(), workspaces: list.workspaces });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Something went wrong. Try again.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Screen title="IRA CARE">
      <Card>
        <Muted>Sign in with the account your care circle gave you.</Muted>
        <TextInput style={styles.input} placeholder="Server address" accessibilityLabel="Server address" autoCapitalize="none" value={baseUrl} onChangeText={setBaseUrl} testID="server" />
        <TextInput style={styles.input} placeholder="Username" accessibilityLabel="Username" autoCapitalize="none" value={username} onChangeText={setUsername} testID="username" />
        <TextInput style={styles.input} placeholder="Password" accessibilityLabel="Password" secureTextEntry value={password} onChangeText={setPassword} testID="password" />
        {error ? <Text style={styles.error}>{error}</Text> : null}
        <BigButton label={busy ? 'Signing in…' : 'Sign in'} onPress={submit} disabled={busy || !username || !password} testID="sign-in" />
      </Card>
      <View style={{ height: 8 }} />
      <Muted>Family see alerts and summaries. Conversations stay private unless the person agrees.</Muted>
    </Screen>
  );
}

const styles = StyleSheet.create({
  input: { borderWidth: 1, borderColor: colors.border, borderRadius: 10, padding: 12, fontSize: 16, marginTop: 10, color: colors.text },
  error: { color: colors.sos, marginTop: 8 },
});
