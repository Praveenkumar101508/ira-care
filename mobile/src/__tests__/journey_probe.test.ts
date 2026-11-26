import { PROBE_PREFIX, probe } from '../journeyProbe';

describe('the journey probe', () => {
  const dev = (global as unknown as { __DEV__: boolean }).__DEV__;
  afterEach(() => { (global as unknown as { __DEV__: boolean }).__DEV__ = dev; jest.restoreAllMocks(); });

  test('in a development build it logs one line per screen, as JSON after a fixed prefix', () => {
    (global as unknown as { __DEV__: boolean }).__DEV__ = true;
    const log = jest.spyOn(console, 'log').mockImplementation(() => undefined);
    probe('sign-in', { signedIn: false });
    probe('sign-in', { signedIn: false });
    expect(log).toHaveBeenCalledTimes(1);
    probe('home', { signedIn: true });
    expect(log).toHaveBeenCalledTimes(2);
    const line = log.mock.calls[0][0] as string;
    expect(line.startsWith(`${PROBE_PREFIX} `)).toBe(true);
    expect(JSON.parse(line.slice(PROBE_PREFIX.length + 1))).toEqual({ screen: 'sign-in', signedIn: false });
  });

  test('in a release build it logs nothing', () => {
    (global as unknown as { __DEV__: boolean }).__DEV__ = false;
    const log = jest.spyOn(console, 'log').mockImplementation(() => undefined);
    probe('alerts');
    expect(log).not.toHaveBeenCalled();
  });
});
