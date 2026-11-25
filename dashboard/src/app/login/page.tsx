'use client';

import { useState } from 'react';
import { Api, ApiError } from '@/lib/api';
import type { Workspace } from '@/lib/types';
import { DEFAULT_BASE_URL, saveSession } from '@/lib/session';

export default function LoginPage() {
  const [baseUrl, setBaseUrl] = useState(DEFAULT_BASE_URL);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [choices, setChoices] = useState<{ token: string; refresh: string; workspaces: Workspace[] } | null>(null);

  const pick = (token: string, refresh: string, w: Workspace) => {
    saveSession({ baseUrl: baseUrl.replace(/\/$/, ''), accessToken: token, refreshToken: refresh, workspaceId: w.id, role: w.role, workspaceName: w.name, username });
    window.location.href = '/';
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setError('');
    try {
      const url = baseUrl.replace(/\/$/, '');
      const tokens = await Api.login(url, username, password);
      const list = await new Api({ baseUrl: url, accessToken: tokens.access_token, workspaceId: null, role: '' }).workspaces();
      if (list.workspaces.length === 1) pick(tokens.access_token, tokens.refresh_token, list.workspaces[0]);
      else setChoices({ token: tokens.access_token, refresh: tokens.refresh_token, workspaces: list.workspaces });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong.');
    }
  };

  if (choices) {
    return (
      <div className="card" style={{ maxWidth: 480, margin: '40px auto' }}>
        <h2>Choose a care organisation</h2>
        {choices.workspaces.map((w) => (
          <div key={w.id} className="card">
            <strong>{w.name}</strong> <span className="muted">{w.role.replace('_', ' ')}</span>
            <div><button onClick={() => pick(choices.token, choices.refresh, w)}>Open</button></div>
          </div>
        ))}
      </div>
    );
  }

  return (
    <form className="card" style={{ maxWidth: 480, margin: '40px auto' }} onSubmit={submit}>
      <h2>Sign in</h2>
      <p className="muted"><a href="/signup">New care organisation? Start here.</a></p>
      <p className="muted">For carers and care organisations. Family use the phone app.</p>
      <details className="advanced"><summary className="muted">Advanced</summary><label>Care server<br /><input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} style={{ width: '100%' }} aria-label="Care server" /></label></details>
      <label>Username<br /><input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" style={{ width: '100%' }} /></label><br /><br />
      <label>Password<br /><input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" style={{ width: '100%' }} /></label><br /><br />
      {error ? <p className="error">{error}</p> : null}
      <button type="submit" disabled={!username || !password}>Sign in</button>
    </form>
  );
}
