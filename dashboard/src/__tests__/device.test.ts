import { DeviceClient, heartRateRun, noMovement, outOfRange, parseTalk, readTalk, sosWords, wristOff } from '@/lib/device';

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

test('the first sentence is spoken while the rest of the reply is still on its way', async () => {
  // Found in the round 4 film: the acknowledgement and the model's sentence were spoken together,
  // seconds late, because the whole body was read before anything was said.
  const enc = new TextEncoder();
  let release: () => void = () => {};
  const later = new Promise<void>((r) => { release = r; });
  const body = new ReadableStream<Uint8Array>({
    async start(c) {
      c.enqueue(enc.encode('{"sentence": "Mm, Ada."}\n{"sent'));
      c.enqueue(enc.encode('ence": "Is it'));
      await later;
      c.enqueue(enc.encode(' sunny?"}\n{"done": true, "action": null, "needs_help": true}\n'));
      c.close();
    },
  });
  const heard: string[] = [];
  const reply = readTalk({ body } as unknown as Response, (s) => heard.push(s));
  await new Promise((r) => setTimeout(r, 20));
  expect(heard).toEqual(['Mm, Ada.']);
  release();
  const r = await reply;
  expect(heard).toEqual(['Mm, Ada.', 'Is it sunny?']);
  expect(r.sentences).toEqual(['Mm, Ada.', 'Is it sunny?']);
  expect(r.needsHelp).toBe(true);
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

test('the watch face words for an SOS come from the server, never from the button press', () => {
  expect(sosWords(null, 'sending')).toBe('Sending…');
  expect(sosWords(null, 'waiting')).toBe('Waiting for connection. Help request saved.');
  expect(sosWords({ state: 'received', alert_id: 'a', responder_name: null }, null)).toBe('Help request received.');
  expect(sosWords({ state: 'on_the_way', alert_id: 'a', responder_name: 'Sravani' }, null)).toBe('Sravani is on the way.');
  expect(sosWords({ state: 'arrived', alert_id: 'a', responder_name: 'Sravani' }, null)).toBe('Sravani has arrived.');
  expect(sosWords({ state: 'idle', alert_id: null, responder_name: null }, null)).toBeNull();
  for (const p of ['sending', 'waiting'] as const) expect(sosWords(null, p)!.toLowerCase()).not.toMatch(/on (the|its) way/);
});

test('a sample is out of range only against a range the carer set', () => {
  const r = { heart_rate_min: 45, heart_rate_max: 130, reading_upload_interval_s: 60 };
  expect(outOfRange(160, r)).toBe(true); expect(outOfRange(40, r)).toBe(true); expect(outOfRange(72, r)).toBe(false);
  expect(outOfRange(200, null)).toBe(false);
  expect(outOfRange(200, { ...r, heart_rate_max: null })).toBe(false);
});

test('a help request carries its idempotency key so a retry is the same request', async () => {
  const calls: { url: string; init: RequestInit }[] = [];
  const fakeFetch = (async (url: string, init: RequestInit) => { calls.push({ url, init }); return { ok: true, status: 201, text: async () => '{"id":"a1","kind":"help_requested","status":"open","at_home":null,"sos":{"state":"received","alert_id":"a1","responder_name":null}}' }; }) as unknown as typeof fetch;
  const c = new DeviceClient('http://care.invalid', 'cd1.scope.secret', fakeFetch);
  const a = await c.help('button', undefined, 'key-1');
  expect((calls[0].init.headers as Record<string, string>)['X-Idempotency-Key']).toBe('key-1');
  expect(a.sos?.state).toBe('received');
});
