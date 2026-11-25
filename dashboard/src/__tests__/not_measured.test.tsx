import { render, screen, waitFor } from '@testing-library/react';
import { NotMeasured } from '@/components/NotMeasured';
import type { Capability } from '@/lib/types';

const list: Capability[] = [
  { key: 'heart_rate', label: 'Heart rate', status: 'available', reading: 'heart_rate', how: 'Measured by the watch.' },
  { key: 'spo2', label: 'Blood oxygen (SpO2)', status: 'not_available', reason: 'The current watch has no validated sensor for this.' },
  { key: 'camera', label: 'Camera', status: 'not_available', reason: 'The watch has no camera.' },
];

test('what IRA does not measure is listed with the reason, and never with a value', async () => {
  render(<NotMeasured api={{ capabilities: async () => list }} />);
  await waitFor(() => expect(screen.getByTestId('not-measured')).toBeInTheDocument());
  expect(screen.getByTestId('not-measured-spo2')).toHaveTextContent('Blood oxygen (SpO2): not available. The current watch has no validated sensor for this.');
  expect(screen.getByTestId('not-measured-camera')).toHaveTextContent('not available');
  expect(screen.queryByTestId('not-measured-heart_rate')).toBeNull();
  expect(screen.getByTestId('not-measured').textContent).not.toMatch(/\d+\s*(%|bpm|mmHg)/);
});

test('nothing is shown when the list cannot be loaded', async () => {
  const { container } = render(<NotMeasured api={{ capabilities: async () => { throw new Error('offline'); } }} />);
  await new Promise((r) => setTimeout(r, 0));
  expect(container).toBeEmptyDOMElement();
});
