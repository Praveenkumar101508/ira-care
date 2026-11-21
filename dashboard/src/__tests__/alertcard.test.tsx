import { fireEvent, render, screen } from '@testing-library/react';
import { AlertCard } from '@/components/AlertCard';
import type { Alert } from '@/lib/types';

const alert: Alert = {
  id: 'a1', person_id: 'p', kind: 'no_answer_no_movement', severity: 'sos', status: 'escalating', opened_at: new Date().toISOString(),
  current_tier: 3, acknowledged_at: null, resolved_at: null,
  details: { summary: 'No answer to the check-in and no movement for 3 minutes.', show_call_999_screen: true },
};

test('an escalating SOS shows its words, the tier-3 note and the acknowledge button', () => {
  const ack = vi.fn();
  render(<AlertCard alert={alert} personName="Test Person" busy={false} onAcknowledge={ack} onResolve={() => {}} />);
  expect(screen.getByText('No answer and no movement')).toBeInTheDocument();
  expect(screen.getByText(/call 999 now/)).toBeInTheDocument();
  fireEvent.click(screen.getByText("I'm on my way"));
  expect(ack).toHaveBeenCalledWith(alert);
});

test('a notice has no acknowledge button, an acknowledged alert can only be resolved', () => {
  render(<AlertCard alert={{ ...alert, severity: 'notice', kind: 'low_battery', details: {} }} personName="P" busy={false} onAcknowledge={() => {}} onResolve={() => {}} />);
  expect(screen.queryByText("I'm on my way")).toBeNull();
  expect(screen.getByText('Resolve')).toBeInTheDocument();
  render(<AlertCard alert={{ ...alert, status: 'acknowledged', details: {} }} personName="P" busy={false} onAcknowledge={() => {}} onResolve={() => {}} />);
  expect(screen.queryByText("I'm on my way")).toBeNull();
});
