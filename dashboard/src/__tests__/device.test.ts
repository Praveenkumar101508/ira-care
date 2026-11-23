import { DeviceClient, heartRateRun, noMovement, parseTalk, wristOff } from '@/lib/device';

test('a heart-rate run is spaced a minute apart and ends now', () => {
  const now = new Date('2026-11-21T02:00:00Z');
  const r = heartRateRun([160, 165, 170], now);
  expect(r.map((x) => x.recorded_at)).toEqual(['2026-11-21T01:58:00.000Z', '2026-11-21T01:59:00.000Z', '2026-11-21T02:00:00.000Z']);
  expect(r.every((x) => x.kind === 'heart_rate' && x.unit === 'bpm')).toBe(true);
  expect(noMovement(now).map((x) => x.kind)).toEqual(['steps', 'movement']);
  expect(wristOff(now)[0]).toMatchObject({ kind: 'on_wrist', value: 0 });
});

test('a streamed talk reply is read line by line and each sentence is handed on', () => {
  const heard: string[] = [];
  const r = parseTalk('{"sentence": "Here is Family Member."}\n{"sentence": "Tap to call."}\n{"done": true, "action": {"kind": "call", "name": "Family Member", "phone": "+440000000001"}, "needs_help": false}\n', (s) => heard.push(s));
  expect(heard).toEqual(['Here is Family Member.', 'Tap to call.']);
  expect(r.action).toEqual({ kind: 'call', name: 'Family Member', phone: '+440000000001' });
});

test('the device client sends the token header to the same routes as the watch', async () => {
  const calls: { url: string; init: RequestInit }[] = [];
  const fakeFetch = (async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return { ok: true, status: 200, text: async () => '{"checkin": null}', json: async () => ({}) };
  }) as unknown as typeof fetch;
  const c = new DeviceClient('http://care.invalid', 'cd1.scope.secret', fakeFetch);
  expect(await c.due()).toBeNull();
  await c.help('fall', { latitude: 51.518, longitude: -0.12, accuracy_m: 12 });
  expect(calls[0].url).toBe('http://care.invalid/api/v1/care/devices/me/checkins/due');
  expect((calls[0].init.headers as Record<string, string>)['X-Device-Token']).toBe('cd1.scope.secret');
  expect(calls[1].url).toBe('http://care.invalid/api/v1/care/devices/me/help');
  expect(JSON.parse(calls[1].init.body as string)).toEqual({ via: 'fall', location: { latitude: 51.518, longitude: -0.12, accuracy_m: 12 } });
});
