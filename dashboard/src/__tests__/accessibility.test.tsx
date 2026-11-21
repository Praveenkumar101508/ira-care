import { render } from '@testing-library/react';
import { axe } from 'jest-axe';
import { AlertCard } from '@/components/AlertCard';
import type { Alert } from '@/lib/types';

const alert: Alert = {
  id: 'a1', person_id: 'p', kind: 'suspected_fall', severity: 'sos', status: 'open', opened_at: new Date().toISOString(),
  current_tier: 1, acknowledged_at: null, resolved_at: null, details: { summary: 'The watch detected a hard impact.' },
  location_latitude: 51.518, location_longitude: -0.12, at_home: false,
};

test('an alert card has no accessibility violations and a name a screen reader can say', async () => {
  const { container, getByRole } = render(<AlertCard alert={alert} personName="Test Person" busy={false} onAcknowledge={() => {}} onResolve={() => {}} />);
  expect(getByRole('article').getAttribute('aria-label')).toContain('Suspected fall, Test Person');
  expect(await axe(container)).toHaveNoViolations();
});

test('the colours meet the contrast the theme promises', () => {
  // The dashboard's tokens: text on the background, muted on the background, white on each pill.
  const L = (hex: string) => { const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)); return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]; };
  const ratio = (a: string, b: string) => { const [x, y] = [L(a), L(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
  expect(ratio('#1f2a24', '#f6f4ef')).toBeGreaterThan(7);
  expect(ratio('#5b6b63', '#f6f4ef')).toBeGreaterThan(4.5);
  for (const pill of ['#b3261e', '#b35c00', '#2f5d8a', '#2e7d32']) expect(ratio('#ffffff', pill)).toBeGreaterThan(4.5);
});
