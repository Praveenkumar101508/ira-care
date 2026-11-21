import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';
import { Platform } from 'react-native';
import type { CareClient } from './api/client';

/**
 * Register this phone for push. The backend sends through the existing
 * notifier (mobile push is off there until its key is configured), so this is
 * best effort and silent when unavailable.
 */
export async function registerForPush(client: CareClient): Promise<boolean> {
  try {
    if (!Device.isDevice) return false;
    const { status } = await Notifications.requestPermissionsAsync();
    if (status !== 'granted') return false;
    const token = (await Notifications.getExpoPushTokenAsync()).data;
    await client.registerPush(token, Platform.OS);
    return true;
  } catch {
    return false;
  }
}
