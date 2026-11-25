jest.mock('expo-device', () => ({ isDevice: true }));
const dismissed: number[] = [];
jest.mock('expo-notifications', () => ({
  requestPermissionsAsync: async () => ({ status: 'granted' }),
  getExpoPushTokenAsync: async () => ({ data: 'ExponentPushToken[test-only]' }),
  dismissAllNotificationsAsync: async () => { dismissed.push(1); },
}));
import { registerForPush, unregisterForPush } from '../push';

test('signing out unregisters this phone and takes the account’s notifications off the screen', async () => {
  const calls: string[] = [];
  const client = { registerPush: async (t: string) => { calls.push(`reg ${t}`); }, unregisterPush: async (t: string) => { calls.push(`unreg ${t}`); } };
  expect(await registerForPush(client as never)).toBe(true);
  await unregisterForPush(client as never);
  expect(calls).toEqual(['reg ExponentPushToken[test-only]', 'unreg ExponentPushToken[test-only]']);
  expect(dismissed.length).toBe(1);
});

test('an offline phone still signs out, without waiting long', async () => {
  const client = { registerPush: async () => {}, unregisterPush: () => new Promise<void>(() => {}) };
  await registerForPush(client as never);
  const started = Date.now();
  await unregisterForPush(client as never, 50);
  expect(Date.now() - started).toBeLessThan(1000);
});
