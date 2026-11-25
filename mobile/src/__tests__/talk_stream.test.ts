import { CareClient, ApiError } from '../api/client';
import { forEachLine, utf8 } from '../api/stream';

const enc = (s: string) => new Uint8Array(Buffer.from(s, 'utf8'));

/** A response whose body arrives in the given byte chunks; `log` records each read. */
function chunked(chunks: Uint8Array[], log: string[] = [], failAt = -1): Response {
  let i = 0;
  const reader = {
    read: async () => {
      if (i === failAt) throw new TypeError('network lost');
      if (i >= chunks.length) { log.push('end'); return { value: undefined, done: true }; }
      log.push(`chunk${i}`);
      return { value: chunks[i++], done: false };
    },
  };
  return { ok: true, status: 200, body: { getReader: () => reader }, text: async () => { throw new Error('not used'); } } as unknown as Response;
}

/** Split bytes at the given offsets, deliberately inside lines and inside characters. */
const cut = (bytes: Uint8Array, at: number[]) => [0, ...at].map((s, k) => bytes.slice(s, [...at, bytes.length][k]));

test('lines are handed on as soon as they are complete, even split mid-line and mid-character', async () => {
  const body = enc('{"sentence":"Café at ten 😊"}\n{"sentence":"Then lunch."}\n{"done":true}\n');
  const ixEmoji = body.indexOf(0xf0) + 2; // inside the 4-byte emoji
  const ixE = body.indexOf(0xc3) + 1;     // inside the 2-byte é
  const log: string[] = [];
  const lines: string[] = [];
  await forEachLine(chunked(cut(body, [ixE, ixEmoji, 40]), log), (l) => { lines.push(l); log.push(`line:${JSON.parse(l).sentence ?? 'done'}`); });
  expect(lines.map((l) => JSON.parse(l).sentence ?? 'done')).toEqual(['Café at ten 😊', 'Then lunch.', 'done']);
  // The first sentence was handed on before the rest of the body had been read.
  expect(log.indexOf('line:Café at ten 😊')).toBeLessThan(log.indexOf('chunk3'));
});

test('a last line without a newline is still delivered, and blank lines are skipped', async () => {
  const lines: string[] = [];
  await forEachLine(chunked([enc('{"a":1}\n\n\n{"b":'), enc('2}')]), (l) => lines.push(l));
  expect(lines).toEqual(['{"a":1}', '{"b":2}']);
});

test('without a readable body the whole text is read and split, as before', async () => {
  const lines: string[] = [];
  const r = { ok: true, status: 200, body: null, text: async () => '{"sentence":"One."}\n{"done":true}\n' } as unknown as Response;
  await forEachLine(r, (l) => lines.push(l));
  expect(lines).toHaveLength(2);
});

test('the small UTF-8 decoder agrees with TextDecoder where a runtime has none', () => {
  const g = globalThis as { TextDecoder?: unknown };
  const saved = g.TextDecoder;
  const text = 'Grüße, नमस्ते, 你好 😊 — done';
  try {
    g.TextDecoder = undefined;
    expect(utf8(enc(text))).toBe(text);
  } finally { g.TextDecoder = saved; }
});

const session = { baseUrl: 'http://care.invalid', accessToken: 't', workspaceId: 'ws', refreshToken: 'r' };

test('Talk shows each sentence as it arrives and returns the closing action', async () => {
  const log: string[] = [];
  const body = enc('{"sentence":"Hello."}\n{"sentence":"Lunch is at one."}\n{"done":true,"action":{"kind":"call","name":"Sam","phone":"+440"},"needs_help":false}\n');
  const stream = (async () => chunked(cut(body, [10, 30, 60]), log)) as unknown as typeof fetch;
  const plain = (async () => { throw new Error('the plain fetch must not be used for Talk'); }) as unknown as typeof fetch;
  const heard: string[] = [];
  const reply = await new CareClient(session, plain, stream).talk('p1', 'hi', (s) => { heard.push(s); log.push(`heard:${s}`); });
  expect(heard).toEqual(['Hello.', 'Lunch is at one.']);
  expect(reply.sentences).toEqual(heard);
  expect(reply.action).toEqual({ kind: 'call', name: 'Sam', phone: '+440' });
  expect(log.indexOf('heard:Hello.')).toBeLessThan(log.indexOf('chunk3'));
});

test('a connection lost part-way keeps what arrived, then says so', async () => {
  const body = enc('{"sentence":"First."}\n{"sentence":"Sec');
  const stream = (async () => chunked(cut(body, [22]), [], 2)) as unknown as typeof fetch;
  const heard: string[] = [];
  const err = await new CareClient(session, undefined, stream).talk('p1', 'hi', (s) => heard.push(s)).catch((e: unknown) => e);
  expect(heard).toEqual(['First.']);
  expect(err).toBeInstanceOf(ApiError);
  expect((err as ApiError).status).toBe(0);
});

test('without a streaming fetch Talk still works, all at once', async () => {
  const plain = (async () => ({ ok: true, status: 200, body: null, text: async () => '{"sentence":"Hi."}\n{"done":true}\n' })) as unknown as typeof fetch;
  const heard: string[] = [];
  const reply = await new CareClient(session, plain).talk('p1', 'hi', (s) => heard.push(s));
  expect(heard).toEqual(['Hi.']);
  expect(reply.sentences).toEqual(['Hi.']);
});
