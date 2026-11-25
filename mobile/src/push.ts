import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';
import { Platform } from 'react-native';
import type { CareClient } from './api/client';

/**
 * Register this phone for push. The backend sends through the existing
 * notifier (mobile push is off there until its key is configured), so this is
 * best effort and silent when unavailable.
 */
let registeredToken: string | null = null;

export async function registerForPush(client: CareClient): Promise<boolean> {
  try {
    if (!Device.isDevice) return false;
    const { status } = await Notifications.requestPermissionsAsync();
    if (status !== 'granted') return false;
    const token = (await Notifications.getExpoPushTokenAsync()).data;
    await client.registerPush(token, Platform.OS);
    registeredToken = token;
    return true;
  } catch {
    return false;
  }
}

/** On sign-out: stop this phone receiving the account's alerts, and take the account's
 *  notifications off the screen. Best effort and bounded: signing out never waits on the
 *  network, and an offline phone still signs out (the server also drops the token when the
 *  push service reports it gone). */
export async function unregisterForPush(client: CareClient | null, timeoutMs = 3000): Promise<void> {
  const token = registeredToken;
  registeredToken = null;
  try { await Notifications.dismissAllNotificationsAsync(); } catch { /* not fatal */ }
  if (!client || !token) return;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const limit = new Promise<void>((r) => { timer = setTimeout(r, timeoutMs); });
  try {
    await Promise.race([client.unregisterPush(token).catch(() => undefined), limit]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}
