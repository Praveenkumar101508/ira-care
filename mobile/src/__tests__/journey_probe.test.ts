import { PROBE_PREFIX, PROBE_URL, probe } from '../journeyProbe';

describe('the journey probe', () => {
  const dev = (global as unknown as { __DEV__: boolean }).__DEV__;
  const realFetch = (global as unknown as { fetch?: unknown }).fetch;
  afterEach(() => {
    (global as unknown as { __DEV__: boolean }).__DEV__ = dev;
    (global as unknown as { fetch?: unknown }).fetch = realFetch;
    jest.restoreAllMocks();
  });

  test('in a development build it logs one line per screen change and posts it to the local listener', async () => {
    (global as unknown as { __DEV__: boolean }).__DEV__ = true;
    const log = jest.spyOn(console, 'log').mockImplementation(() => undefined);
    const posted: Array<{ url: string; body: string }> = [];
    (global as unknown as { fetch: unknown }).fetch = jest.fn((url: string, init: { body: string }) => {
      posted.push({ url, body: init.body });
      return Promise.resolve({ ok: true });
    });
    probe('sign-in', { signedIn: false });
    probe('sign-in', { signedIn: false });
    expect(log).toHaveBeenCalledTimes(1);
    const line = log.mock.calls[0][0] as string;
    expect(line.startsWith(`${PROBE_PREFIX} `)).toBe(true);
    expect(JSON.parse(line.slice(PROBE_PREFIX.length + 1))).toEqual({ screen: 'sign-in', signedIn: false });
    expect(posted).toEqual([{ url: PROBE_URL, body: JSON.stringify({ screen: 'sign-in', signedIn: false }) }]);
    probe('home', { signedIn: true });
    expect(log).toHaveBeenCalledTimes(2);
    expect(posted).toHaveLength(2);
  });

  test('a listener that refuses the post does not disturb the app', () => {
    (global as unknown as { __DEV__: boolean }).__DEV__ = true;
    jest.spyOn(console, 'log').mockImplementation(() => undefined);
    (global as unknown as { fetch: unknown }).fetch = jest.fn(() => Promise.reject(new Error('refused')));
    expect(() => probe('alerts', { signedIn: true })).not.toThrow();
  });

  test('in a release build it logs nothing and sends nothing', () => {
    (global as unknown as { __DEV__: boolean }).__DEV__ = false;
    const log = jest.spyOn(console, 'log').mockImplementation(() => undefined);
    const f = jest.fn();
    (global as unknown as { fetch: unknown }).fetch = f;
    probe('care');
    expect(log).not.toHaveBeenCalled();
    expect(f).not.toHaveBeenCalled();
  });
});
