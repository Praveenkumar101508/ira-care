import { heardWords, makeListener, makeSpeaker } from '../voice/voice';

type Fn = (e: any) => void; // eslint-disable-line @typescript-eslint/no-explicit-any

/** A recogniser shaped like expo-speech-recognition's module; `script` plays its events. */
function fakeRecogniser(opts: { available?: boolean; granted?: boolean; script?: (emit: (ev: string, e?: unknown) => void) => void } = {}) {
  const listeners: Record<string, Fn[]> = {};
  const calls: string[] = [];
  const mod = {
    isRecognitionAvailable: () => opts.available ?? true,
    requestPermissionsAsync: async () => ({ granted: opts.granted ?? true }),
    start: (o: Record<string, unknown>) => { calls.push(`start:${o.lang}`); opts.script?.((ev, e) => (listeners[ev] ?? []).forEach((f) => f(e))); },
    stop: () => calls.push('stop'),
    abort: () => calls.push('abort'),
    addListener: (ev: string, f: Fn) => { (listeners[ev] ??= []).push(f); return { remove: () => { listeners[ev] = listeners[ev].filter((g) => g !== f); } }; },
  };
  return { mod, calls, listeners };
}

test('one utterance is heard and turned into text', async () => {
  const { mod, calls, listeners } = fakeRecogniser({ script: (emit) => {
    emit('result', { isFinal: false, results: [{ transcript: 'did I take' }] });
    emit('result', { isFinal: true, results: [{ transcript: 'did I take my tablets' }] });
  } });
  expect(await makeListener(mod).listen('en-GB')).toEqual({ text: 'did I take my tablets' });
  expect(calls).toEqual(['start:en-GB']);
  expect(Object.values(listeners).flat()).toHaveLength(0); // nothing left listening
});

test('without the microphone permission nothing starts', async () => {
  const { mod, calls } = fakeRecogniser({ granted: false });
  expect(await makeListener(mod).listen('en-GB')).toEqual({ error: 'not_allowed' });
  expect(calls).toEqual([]);
});

test('silence, a refusal and a failure each say what happened', async () => {
  for (const [code, want] of [['no-speech', 'nothing_heard'], ['not-allowed', 'not_allowed'], ['network', 'failed']] as const) {
    const { mod } = fakeRecogniser({ script: (emit) => emit('error', { error: code }) });
    expect(await makeListener(mod).listen('en-GB')).toEqual({ error: want });
  }
  const ended = fakeRecogniser({ script: (emit) => emit('end') });
  expect(await makeListener(ended.mod).listen('en-GB')).toEqual({ error: 'nothing_heard' });
});

test('a recogniser that never finishes is stopped, keeping what it heard', async () => {
  jest.useFakeTimers();
  try {
    const { mod, calls } = fakeRecogniser({ script: (emit) => emit('result', { isFinal: false, results: [{ transcript: 'call my daughter' }] }) });
    const p = makeListener(mod, 5000).listen('en-GB');
    await Promise.resolve(); await Promise.resolve();
    jest.advanceTimersByTime(5000);
    expect(await p).toEqual({ text: 'call my daughter' });
    expect(calls).toContain('abort');
  } finally { jest.useRealTimers(); }
});

test('where there is no recogniser, listening is unavailable rather than broken', async () => {
  expect(makeListener(null).available()).toBe(false);
  expect(await makeListener(null).listen('en-GB')).toEqual({ error: 'unavailable' });
  expect(makeListener(fakeRecogniser({ available: false }).mod).available()).toBe(false);
});

test('speech out is in British English at a gentle pace, and a broken engine is harmless', () => {
  const said: { text: string; opts: unknown }[] = [];
  makeSpeaker({ speak: (text, opts) => { said.push({ text, opts }); }, stop: () => {} }).speak('Lunch is at one.');
  expect(said).toEqual([{ text: 'Lunch is at one.', opts: { language: 'en-GB', rate: 0.9 } }]);
  const broken = makeSpeaker({ speak: () => { throw new Error('engine gone'); }, stop: () => { throw new Error('x'); } });
  expect(() => { broken.speak('x'); broken.stop(); }).not.toThrow();
  expect(() => makeSpeaker(null).speak('x')).not.toThrow();
});

test('every way of hearing nothing has plain words for the person', () => {
  for (const e of ['not_allowed', 'nothing_heard', 'unavailable', 'failed'] as const) expect(heardWords({ error: e })).toMatch(/type/);
  expect(heardWords({ text: 'hello' })).toBeNull();
});
