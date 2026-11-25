import { LiveGate, STALE_AFTER_MS, liveWords, settleAll } from '@/lib/live';
import { headline, wristState } from '@/lib/view';

const now = new Date('2026-11-25T10:00:00Z');

test('one load at a time per person, and a late answer for the last person is dropped', () => {
  const gate = new LiveGate();
  gate.next();
  const first = gate.begin();
  expect(first).not.toBeNull();
  expect(gate.begin()).toBeNull(); // no overlapping load
  // The reader opens another person while the first load is still running.
  gate.next();
  const second = gate.begin();
  expect(second).not.toBeNull();
  expect(gate.current(first!)).toBe(false); // the first person's answer must not land here
  expect(gate.current(second!)).toBe(true);
  gate.end(first!); // the stale load finishing does not free the new one's slot
  expect(gate.begin()).toBeNull();
  gate.end(second!);
  expect(gate.begin()).not.toBeNull();
});

test('answers arriving in the wrong order: only the newest person is ever shown', async () => {
  const gate = new LiveGate();
  const shown: string[] = [];
  const loadFor = async (person: string, delay: number) => {
    const t = gate.begin();
    if (t === null) return;
    await new Promise((r) => setTimeout(r, delay));
    if (gate.current(t)) shown.push(person);
    gate.end(t);
  };
  gate.next();
  const a = loadFor('Ada', 30); // slow
  gate.next();
  const b = loadFor('Bea', 5); // fast
  await Promise.all([a, b]);
  expect(shown).toEqual(['Bea']);
});

test('the record says how live it is, and never that it is live when it is not', () => {
  expect(liveWords({ lastOk: null, failures: 0 }, now).state).toBe('loading');
  expect(liveWords({ lastOk: null, failures: 2 }, now).state).toBe('offline');
  expect(liveWords({ lastOk: new Date(now.getTime() - 10_000), failures: 0 }, now)).toEqual({ state: 'live', words: 'Live: updated 10 s ago' });
  const lost = liveWords({ lastOk: new Date(now.getTime() - 40_000), failures: 1 }, now);
  expect(lost.state).toBe('reconnecting');
  expect(lost.words).toContain('may be out of date');
  // No failure reported, but nothing new for too long: still not "live".
  expect(liveWords({ lastOk: new Date(now.getTime() - STALE_AFTER_MS - 1000), failures: 0 }, now).state).toBe('reconnecting');
});

test('one section failing is named, and the others still load', async () => {
  const { values, failed } = await settleAll({
    visits: async () => ['v'],
    consents: async () => { throw new Error('503'); },
    circle: async () => ['c'],
  });
  expect(values).toEqual({ visits: ['v'], circle: ['c'] });
  expect(failed).toEqual(['consents']);
});

test('the wrist is on only when the watch said so recently; unknown is never "on the wrist"', () => {
  const recent = '2026-11-25T09:55:00Z';
  expect(wristState({ on_wrist: true, last_sync_at: recent }, 60, now)).toEqual({ state: 'on', words: 'on the wrist' });
  expect(wristState({ on_wrist: false, last_sync_at: recent }, 60, now).state).toBe('off');
  for (const unknown of [null, undefined]) {
    const w = wristState({ on_wrist: unknown, last_sync_at: recent }, 60, now);
    expect(w).toEqual({ state: 'unknown', words: 'wrist unknown' });
  }
  expect(wristState(undefined, 60, now).state).toBe('unknown');
  const stale = wristState({ on_wrist: true, last_sync_at: '2026-11-25T07:00:00Z' }, 60, now);
  expect(stale.state).toBe('stale');
  expect(stale.words).toBe('last known on the wrist, 3 h ago');
});

test('the headline never says All well while it does not know, or while IRA is still asking', () => {
  const base = { open_alerts: [] as { severity: string; status: string }[], devices: [{ last_sync_at: '2026-11-25T09:55:00Z', on_wrist: true as boolean | null }], last_checkin: null };
  expect(headline(base, null, now)).toBe('All well');
  expect(headline({ ...base, devices: [{ last_sync_at: '2026-11-25T09:55:00Z', on_wrist: null }] }, null, now))
    .toBe('Needs a look: not known whether the watch is worn');
  expect(headline({ ...base, open_alerts: [{ severity: 'sos', status: 'asking' }] }, null, now))
    .toBe('IRA is asking whether they are OK: no answer yet');
  expect(headline({ ...base, devices: [] }, null, now)).toMatch(/^Needs a look: no watch paired/);
  expect(headline({ ...base, devices: [{ last_sync_at: '2026-11-25T09:55:00Z', on_wrist: false }] }, null, now)).toMatch(/^Needs a look: watch off the wrist/);
});
