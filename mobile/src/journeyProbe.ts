/**
 * The journey probe: one log line per screen, in development builds only.
 *
 * The iOS simulator has no uiautomator. The simulator journey (scripts/simulator/ios_journey.py)
 * launches the Debug build with its console attached and reads these lines to know which screen
 * the app is showing. `__DEV__` is false in every release bundle, so a release build logs nothing
 * and carries no behaviour from this file; `mobile/src/__tests__/journey_probe.test.ts` pins both.
 */
export const PROBE_PREFIX = 'ira-journey';

let last = '';

/** Logs the screen once per change (a re-render of the same screen says nothing). */
export function probe(screen: string, detail: Record<string, string | number | boolean> = {}): void {
  if (!__DEV__) return;
  const line = `${PROBE_PREFIX} ${JSON.stringify({ screen, ...detail })}`;
  if (line === last) return;
  last = line;
  // eslint-disable-next-line no-console
  console.log(line);
}
