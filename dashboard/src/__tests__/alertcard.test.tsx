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

test('an SOS says where the person is and offers the access notes; a notice does neither', async () => {
  const person = { id: 'p', display_name: 'Test Person', preferred_name: '', timezone: 'UTC', status: 'active', notes: '', home_address: '1 Example Street',
    home_latitude: 51.5, home_longitude: -0.12, home_radius_m: 150, emergency_summary: 'Hard of hearing.', has_access_notes: true };
  const away = { ...alert, location_latitude: 51.518, location_longitude: -0.12, location_accuracy_m: 12, at_home: false };
  const notes = vi.fn(async () => ({ allowed: true, access_notes: 'Key safe left of the door.', emergency_summary: '', home_address: '' }));
  render(<AlertCard alert={away} personName="Test Person" person={person} busy={false} onAcknowledge={() => {}} onResolve={() => {}} onAccessNotes={notes} />);
  expect(screen.getByTestId('whereabouts').textContent).toContain('Away from home (±12 m). 51.51800, -0.12000. Home: 1 Example Street');
  expect(screen.getByText('open map').getAttribute('href')).toContain('openstreetmap.org');
  expect(screen.getByText(/Hard of hearing/)).toBeInTheDocument();
  fireEvent.click(screen.getByText(/Show access notes/));
  await screen.findByTestId('access-notes');
  expect(screen.getByTestId('access-notes').textContent).toContain('Key safe left of the door.');
  render(<AlertCard alert={{ ...alert, severity: 'notice', kind: 'low_battery', details: {} }} personName="P" person={person} busy={false} onAcknowledge={() => {}} onResolve={() => {}} onAccessNotes={notes} />);
  expect(screen.queryAllByTestId('whereabouts')).toHaveLength(1);
});

test('no location is said plainly, never guessed', () => {
  render(<AlertCard alert={alert} personName="P" person={null} busy={false} onAcknowledge={() => {}} onResolve={() => {}} />);
  expect(screen.getByTestId('whereabouts').textContent).toContain('Location unavailable from the watch.');
});

test('an acknowledged SOS offers "I\'ve arrived" until the responder has arrived', () => {
  const arrived = vi.fn();
  render(<AlertCard alert={{ ...alert, status: 'acknowledged', details: {}, acknowledgements: [{ id: 'k1', kind: 'on_my_way', note: '', acknowledged_at: new Date().toISOString() }] }} personName="P" busy={false} onAcknowledge={() => {}} onArrived={arrived} onResolve={() => {}} />);
  fireEvent.click(screen.getByText("I've arrived"));
  expect(arrived).toHaveBeenCalled();
  render(<AlertCard alert={{ ...alert, id: 'a2', status: 'acknowledged', details: {}, acknowledgements: [{ id: 'k2', kind: 'arrived', note: '', acknowledged_at: new Date().toISOString() }] }} personName="P" busy={false} onAcknowledge={() => {}} onArrived={arrived} onResolve={() => {}} />);
  expect(screen.getAllByText(/the watch has been told/).length).toBe(1);
});

test('push attempts show the delivery state the service reported', () => {
  render(<AlertCard alert={{ ...alert, id: 'a3', attempts: [
    { id: 't1', tier: 1, channel: 'push', outcome: 'sent', detail: '', attempted_at: new Date().toISOString(), delivery: 'delivered', delivered_at: new Date().toISOString() },
    { id: 't2', tier: 1, channel: 'sms', outcome: 'logged', detail: '', attempted_at: new Date().toISOString() },
    { id: 't3', tier: 2, channel: 'push', outcome: 'skipped', detail: '', attempted_at: new Date().toISOString(), delivery: 'no phone registered' },
  ] }} personName="P" busy={false} onAcknowledge={() => {}} onResolve={() => {}} />);
  const text = screen.getByTestId('delivery').textContent ?? '';
  expect(text).toContain('Tier 1 phone: delivered');
  expect(text).toContain('Tier 2 phone: no phone registered');
  expect(text).not.toContain('sms');
});
