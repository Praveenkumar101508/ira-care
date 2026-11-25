import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { render, screen, waitFor } from '@testing-library/react';
import { ActivityTrend, ACTIVITY_LABEL } from '@/components/ActivityTrend';
import type { ActivityMinutes, ActivitySummary } from '@/lib/types';

const m = (active: number, still: number, other: number, unk: number, unav: number, nr: number): ActivityMinutes => ({
  active, still, other_movement: other, unknown: unk, unavailable: unav, not_recorded: nr, unknown_or_unavailable: unk + unav + nr,
});

const shared: ActivitySummary = {
  label: ACTIVITY_LABEL, sharing: true, timezone: 'Europe/London',
  groups: { active: 'walking or on stairs', still: 'sitting or standing', other_movement: 'other arm movement' },
  model: { version: 'ira-care-activity-mlp/1', sha256: 'a'.repeat(64) },
  days: [
    { date: '2026-11-23', minutes: m(0, 0, 0, 0, 0, 1440) },
    { date: '2026-11-24', minutes: m(30, 30, 40, 10, 10, 1320) },
  ],
  weeks: [{ start: '2026-11-18', end: '2026-11-24', minutes: m(30, 30, 40, 10, 10, 9960) }],
  sources: ['device'],
};

test('the trend is labelled as an estimate and shows minutes per group, day and week', async () => {
  render(<ActivityTrend api={{ activity: async () => shared }} personId="p1" />);
  await waitFor(() => expect(screen.getByTestId('activity-trend')).toBeInTheDocument());
  expect(screen.getByTestId('activity-label')).toHaveTextContent("Estimated from the watch's motion sensor; not a measurement of health.");
  const days = screen.getByTestId('activity-days');
  expect(days).toHaveTextContent('Active (walking or on stairs)');
  expect(days).toHaveTextContent('30 min');
  expect(days).toHaveTextContent('40 min');
  // Not recorded is shown as such, never as zero activity.
  expect(days).toHaveTextContent('24 h 0 min');
  expect(days).toHaveTextContent('22 h 20 min');
  expect(screen.getByTestId('activity-weeks')).toHaveTextContent('166 h 20 min');
  expect(screen.queryByTestId('activity-simulated')).toBeNull();
});

test('nothing about eating, health status or alerts is claimed', async () => {
  const { container } = render(<ActivityTrend api={{ activity: async () => shared }} personId="p1" />);
  await waitFor(() => expect(screen.getByTestId('activity-trend')).toBeInTheDocument());
  const text = container.textContent ?? '';
  // The one sentence that says what it is NOT is the only place these words may appear.
  const disclaimer = 'It can be wrong, and it does not tell whether someone ate, drank or is unwell. It never raises an alert.';
  expect(text).toContain(disclaimer);
  expect(text.replace(disclaimer, '')).not.toMatch(/eating|eats|\bate\b|meal|drink|healthy|unhealthy|unwell|frail|decline|diagnos|alert/i);
  expect(container.querySelector('.pill.urgent, .pill.sos')).toBeNull();
});

test('without the consent only the fact is shown, and no numbers', async () => {
  const off: ActivitySummary = { ...shared, sharing: false, days: [], weeks: [], sources: [] };
  render(<ActivityTrend api={{ activity: async () => off }} personId="p1" />);
  await waitFor(() => expect(screen.getByTestId('activity-not-shared')).toBeInTheDocument());
  expect(screen.getByTestId('activity-trend').textContent).not.toMatch(/\d+\s*(min|h)\b/);
});

test('simulated input is said to be simulated', async () => {
  render(<ActivityTrend api={{ activity: async () => ({ ...shared, sources: ['simulated'] }) }} personId="p1" />);
  await waitFor(() => expect(screen.getByTestId('activity-simulated')).toBeInTheDocument());
});

test('nothing is shown when the trend cannot be loaded', async () => {
  const { container } = render(<ActivityTrend api={{ activity: async () => { throw new Error('offline'); } }} personId="p1" />);
  await new Promise((r) => setTimeout(r, 0));
  expect(container).toBeEmptyDOMElement();
});

test('the panel has no inline script or inline style (the CSP work)', () => {
  const src = readFileSync(join(__dirname, '../components/ActivityTrend.tsx'), 'utf8');
  expect(src).not.toMatch(/dangerouslySetInnerHTML|<script|style=\{/);
});
