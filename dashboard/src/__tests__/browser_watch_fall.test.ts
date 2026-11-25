import { act, renderHook } from '@testing-library/react';
import { vi } from 'vitest';

/** Every Help the browser watch sends, in order. */
const helps: string[] = [];

vi.mock('@/lib/device', () => {
  class DeviceError extends Error { constructor(public status: number, m: string) { super(m); } }
  class DeviceClient {
    constructor(public baseUrl: string, public token: string) {
      // Any method the hook polls (due checks, SOS state, readings) answers with nothing.
      return new Proxy(this, {
        get(target, prop, recv) {
          if (prop in target) return Reflect.get(target, prop, recv);
          return async () => null;
        },
      });
    }
    async me() { return { fall: { fall_detection_enabled: true, fall_countdown_s: 30 } }; }
    async fallStarted() { return {}; }
    async fallOk() { return {}; }
    async help(via: string) { helps.push(via); return { kind: 'help_requested', status: 'open', at_home: true, sos: null }; }
    static async pair() { return { device_token: 't', id: 'd', person_id: 'p' }; }
  }
  return { DeviceClient, DeviceError };
});

import { useBrowserWatch } from '@/lib/browserWatch';

beforeEach(() => { helps.length = 0; vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); });

async function pairedWatch() {
  const h = renderHook(() => useBrowserWatch('watch'));
  await act(async () => { h.result.current.setSpeak(false); h.result.current.setToken('device-token'); });
  return h;
}

test('Help during a fall countdown ends the countdown, and only one SOS is sent', async () => {
  const h = await pairedWatch();
  await act(async () => { await h.result.current.fall(); });
  expect(h.result.current.fallCountdown).toBe(30);

  await act(async () => { await h.result.current.help('button'); });
  expect(h.result.current.fallCountdown).toBeNull();
  expect(helps).toEqual(['button']);

  // Long after the countdown would have reached zero: no second SOS.
  await act(async () => { vi.advanceTimersByTime(40_000); });
  expect(helps).toEqual(['button']);
});

test('with no answer the countdown sends Help once, by itself', async () => {
  const h = await pairedWatch();
  await act(async () => { await h.result.current.fall(); });
  await act(async () => { vi.advanceTimersByTime(31_000); });
  expect(helps).toEqual(['fall']);
  expect(h.result.current.fallCountdown).toBeNull();
});
