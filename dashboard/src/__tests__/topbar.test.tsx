import { render, screen, waitFor } from '@testing-library/react';
import { TopBar } from '@/components/TopBar';
import { saveSession } from '@/lib/session';

const session = { baseUrl: 'http://care.invalid', accessToken: 't', workspaceId: 'w', role: 'viewer', workspaceName: 'Org', username: 'u' };

function fakeFetch(paused: boolean) {
  return (async () => ({ ok: true, status: 200, json: async () => ({ monitoring_paused: paused, stale_after_s: 300, checked_at: '', jobs: {} }) })) as unknown as typeof fetch;
}

test('the red banner appears only when monitoring is paused', async () => {
  saveSession(session);
  vi.stubGlobal('fetch', fakeFetch(true));
  render(<TopBar />);
  await waitFor(() => expect(screen.getByTestId('monitoring-paused')).toBeInTheDocument());
  expect(screen.getByRole('alert').textContent).toContain('Monitoring paused');
});

test('no banner while the jobs are running', async () => {
  saveSession(session);
  vi.stubGlobal('fetch', fakeFetch(false));
  render(<TopBar />);
  await waitFor(() => expect(screen.getByText('Sign out')).toBeInTheDocument());
  expect(screen.queryByTestId('monitoring-paused')).toBeNull();
});
