'use client';

import { Suspense, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { Api, ApiError } from '@/lib/api';
import { DEFAULT_BASE_URL, saveSession } from '@/lib/session';

/** An invitation link: sign in (or create an account) with the invited address, then join the circle. */
function Join() {
  const params = useSearchParams();
  const token = params.get('token') ?? '';
  const [baseUrl, setBaseUrl] = useState(DEFAULT_BASE_URL);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [mode, setMode] = useState<'signin' | 'create'>('signin');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const url = baseUrl.replace(/\/$/, '');

  const go = async (e: React.FormEvent) => {
    e.preventDefault(); setError(''); setMessage('');
    try {
      if (mode === 'create') {
        const r = await fetch(`${url}/api/v1/care/signup`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password }) });
        if (!r.ok) { setError(r.status === 404 ? 'Account creation is not enabled on this server.' : 'Could not create the account.'); return; }
        setMessage('Confirm your email address from the link you were sent, then come back to this page and sign in.'); setMode('signin'); return;
      }
      const tokens = await Api.login(url, email, password);
      const r = await fetch(`${url}/api/v1/care/invitations/accept`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${tokens.access_token}` }, body: JSON.stringify({ token }) });
      if (!r.ok) { setError((await r.json().catch(() => ({ detail: 'This invitation link is not valid.' }))).detail ?? 'This invitation link is not valid.'); return; }
      const joined = await r.json() as { workspace_id: string | null };
      if (joined.workspace_id) saveSession({ baseUrl: url, accessToken: tokens.access_token, workspaceId: joined.workspace_id, role: 'member', workspaceName: 'Care team', username: email });
      setMessage('You have joined the care circle. The family app and this dashboard now show the person you were invited for.');
    } catch (err) { setError(err instanceof ApiError ? err.message : 'Something went wrong.'); }
  };

  return (
    <div style={{ maxWidth: 520, margin: '40px auto' }}>
      <h2>Join a care circle</h2>
      {!token ? <p className="error">This page needs an invitation link.</p> : null}
      {error ? <p className="error">{error}</p> : null}
      {message ? <p className="ok">{message}</p> : null}
      <form className="card" onSubmit={(e) => { void go(e); }}>
        <p className="muted">Use the email address the invitation was sent to; the link works only for that address, once.</p>
        <label>Server<br /><input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} style={{ width: '100%' }} /></label><br /><br />
        <label>Email<br /><input value={email} onChange={(e) => setEmail(e.target.value)} style={{ width: '100%' }} /></label><br /><br />
        <label>Password<br /><input type="password" value={password} onChange={(e) => setPassword(e.target.value)} style={{ width: '100%' }} /></label><br /><br />
        <label><input type="radio" checked={mode === 'signin'} onChange={() => setMode('signin')} /> I have an account</label>{' '}
        <label><input type="radio" checked={mode === 'create'} onChange={() => setMode('create')} /> Create one (12 characters or more)</label><br /><br />
        <button type="submit" disabled={!token || !email || !password}>{mode === 'create' ? 'Create account' : 'Sign in and join'}</button>
      </form>
    </div>
  );
}

export default function JoinPage() { return <Suspense><Join /></Suspense>; }
