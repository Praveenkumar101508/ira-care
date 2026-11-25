import { NO_CONNECTION, faceWords } from '@/lib/face';

const visit = { state: 'here', carer_name: 'Praveen', planned_start: '2026-11-24T10:00:00+00:00' };

test('a carer in the room outranks a closed SOS, and a live SOS outranks everything but a fall', () => {
  expect(faceWords({ fallCountdown: null, sos: { state: 'closed', alert_id: 'a', responder_name: null }, sosPending: null, question: null, visit }).words).toBe('Praveen is here.');
  expect(faceWords({ fallCountdown: null, sos: { state: 'on_the_way', alert_id: 'a', responder_name: 'Sravani' }, sosPending: null, question: 'Ada, are you OK?', visit }).words).toBe('Sravani is on the way.');
  expect(faceWords({ fallCountdown: 12, sos: { state: 'on_the_way', alert_id: 'a', responder_name: 'Sravani' }, sosPending: null, question: null, visit }).words).toMatch(/Help in 12 s/);
});

test('a question comes before a visit, a closed SOS is still said when nothing else is happening', () => {
  expect(faceWords({ fallCountdown: null, sos: null, sosPending: null, question: 'Ada, are you OK?', visit }).words).toBe('Ada, are you OK?');
  expect(faceWords({ fallCountdown: null, sos: { state: 'closed', alert_id: 'a', responder_name: null }, sosPending: null, question: null, visit: null }).words).toBe('This is now resolved.');
  expect(faceWords({ fallCountdown: null, sos: null, sosPending: null, question: null, visit: null }).words).toBe('All well');
});

test('a visit that is coming is the second line, never the first', () => {
  const r = faceWords({ fallCountdown: null, sos: null, sosPending: null, question: null, visit: { ...visit, state: 'expected' } });
  expect(r.words).toBe('All well');
  expect(r.second).toBe('Praveen is coming at 10:00.');
});

test('a watch with no connection never says All well', () => {
  const quiet = { fallCountdown: null, sos: null, sosPending: null, question: null, visit: null };
  expect(faceWords({ ...quiet, online: false }).words).toBe(NO_CONNECTION);
  expect(faceWords({ ...quiet, online: true }).words).toBe('All well');
  // Help still wins over the connection line: it is saved and will go.
  expect(faceWords({ ...quiet, online: false, fallCountdown: 9 }).words).toMatch(/Help in 9 s/);
});
