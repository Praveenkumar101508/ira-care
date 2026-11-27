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
  } catch (e) {
    console.warn('ira-storage: the session could not be read on this phone', String(e));
    return null;
  }
}

export async function saveSession(session: StoredSession | null): Promise<void> {
  try {
    if (session) await SecureStore.setItemAsync(KEY, JSON.stringify(session));
    else await SecureStore.deleteItemAsync(KEY);
  } catch (e) {
    // Storage failures are not fatal: the person signs in again next time. Logged (no session
    // contents) so a phone that keeps forgetting its sign-in can be diagnosed.
    console.warn('ira-storage: the session could not be kept on this phone', String(e));
  }
}

const APPEARANCE_KEY = 'ira_care_appearance';

/** The appearance kept on this phone so the app looks right before the server answers.
 *  A photo wallpaper is too large for the secure store, so it is kept on the server only. */
export async function loadAppearance(): Promise<Record<string, unknown> | null> {
  try {
    const raw = await SecureStore.getItemAsync(APPEARANCE_KEY);
    return raw ? (JSON.parse(raw) as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** Forget this account's look on this phone: the next person to sign in starts plain. */
export async function clearAppearance(): Promise<void> {
  try {
    await SecureStore.deleteItemAsync(APPEARANCE_KEY);
  } catch {
    // Not fatal.
  }
}

export async function saveAppearance(a: object): Promise<void> {
  try {
    const copy: Record<string, unknown> = { ...(a as Record<string, unknown>) };
    if (copy.wallpaper_kind === 'photo') { copy.wallpaper_kind = 'colour'; copy.wallpaper_value = '#FAF8F4'; copy.photo_on_server = true; }
    await SecureStore.setItemAsync(APPEARANCE_KEY, JSON.stringify(copy));
  } catch {
    // Not fatal: the server copy is the one that counts.
  }
}
