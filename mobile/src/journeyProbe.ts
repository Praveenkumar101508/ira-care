/**
 * The journey probe: one record per screen change, in development builds only.
 *
 * The iOS simulator has no uiautomator. The simulator journey (scripts/simulator/ios_journey.py)
 * launches the Debug build and needs to know which screen the app is showing. In development
 * bundles (`__DEV__`) the probe writes one line to the console and, because the console of a
 * launched simulator app is not always attached, also posts the same line to a listener the
 * journey script runs on the host's loopback (`PROBE_URL`; the simulator shares it, the Android
 * emulator sees the host as 10.0.2.2). A failed post is ignored: the probe must never affect the
 * app. `__DEV__` is false in every release bundle, so a release build logs and sends nothing and
 * carries no behaviour from this file; `mobile/src/__tests__/journey_probe.test.ts` pins both.
 */
export const PROBE_PREFIX = 'ira-journey';
export const PROBE_URL = 'http://127.0.0.1:8777/journey';

let last = '';

/** Records the screen once per change (a re-render of the same screen says nothing). */
export function probe(screen: string, detail: Record<string, string | number | boolean> = {}): void {
  if (!__DEV__) return;
  const body = JSON.stringify({ screen, ...detail });
  const line = `${PROBE_PREFIX} ${body}`;
  if (line === last) return;
  last = line;
  // eslint-disable-next-line no-console
  console.log(line);
  if (typeof fetch === 'function') {
    try {
      void fetch(PROBE_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body }).catch(() => undefined);
    } catch {
      // nothing: the listener is optional
    }
  }
}
