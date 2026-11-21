import * as SecureStore from 'expo-secure-store';

const KEY = 'ira_care_session';

export interface StoredSession {
  baseUrl: string;
  accessToken: string;
  refreshToken: string;
  workspaceId: string | null;
  workspaceName: string | null;
  username: string;
}

export async function loadSession(): Promise<StoredSession | null> {
  try {
    const raw = await SecureStore.getItemAsync(KEY);
    return raw ? (JSON.parse(raw) as StoredSession) : null;
  } catch {
    return null;
  }
}

export async function saveSession(session: StoredSession | null): Promise<void> {
  try {
    if (session) await SecureStore.setItemAsync(KEY, JSON.stringify(session));
    else await SecureStore.deleteItemAsync(KEY);
  } catch {
    // Storage failures are not fatal: the person signs in again next time.
  }
}
