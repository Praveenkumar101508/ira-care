'use client';

import { useState } from 'react';
import { Api, ApiError } from '@/lib/api';
import { DEFAULT_BASE_URL, saveSession } from '@/lib/session';

/**
 * A care organisation starts on its own: an account, the confirmation link
 * (by mail; log-only until SMTP is configured, so the operator sees it in the
 * server log), then the organisation itself with a 30-day trial of a plan.
 */
export default function SignupPage() {
  const [baseUrl, setBaseUrl] = useState(DEFAULT_BASE_URL);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [org, setOrg] = useState('');
  const [plan, setPlan] = useState<'care_family' | 'care_organisation'>('care_organisation');
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const url = baseUrl.replace(/\/$/, '');

  const signup = async (e: React.FormEvent) => {
    e.preventDefault(); setError('');
    const r = await fetch(`${url}/api/v1/care/signup`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password }) }).catch(() => null);
    if (!r) { setError('No connection to the server.'); return; }
    if (r.status === 404) { setError('Sign-up is not enabled on this server (IRA_CLOUD_REGISTRATION_ENABLED).'); return; }
    if (!r.ok) { setError((await r.json().catch(() => ({ detail: 'Sign-up failed.' }))).detail ?? 'Sign-up failed.'); return; }
    setMessage('If that address can be registered, a confirmation link has been sent to it. Confirm it, then come back here.');
    setStep(2);
  };

  const create = async (e: React.FormEvent) => {
    e.preventDefault(); setError('');
    try {
      const tokens = await Api.login(url, email, password);
      const r = await fetch(`${url}/api/v1/care/organisations`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${tokens.access_token}` }, body: JSON.stringify({ name: org, plan_id: plan }) });
      if (!r.ok) { setError((await r.json().catch(() => ({ detail: 'Could not create the organisation.' }))).detail ?? 'Could not create the organisation.'); return; }
      const made = await r.json() as { workspace_id: string; name: string };
      saveSession({ baseUrl: url, accessToken: tokens.access_token, refreshToken: tokens.refresh_token, workspaceId: made.workspace_id, role: 'workspace_admin', workspaceName: made.name, username: email });
      setStep(3);
    } catch (err) { setError(err instanceof ApiError ? err.message : 'Something went wrong.'); }
  };

  return (
    <div style={{ maxWidth: 520, margin: '40px auto' }}>
      <h2>Start a care organisation</h2>
      <ol className="muted">
        <li>Create your account and confirm your email address.</li>
        <li>Sign in and name your organisation; a 30-day trial of a plan starts.</li>
        <li>Add the person you look after, invite the family, pair the watch.</li>
      </ol>
      {error ? <p className="error">{error}</p> : null}
      {message ? <p className="ok">{message}</p> : null}
      {step === 1 ? (
        <form className="card" onSubmit={(e) => { void signup(e); }}>
          <details className="advanced"><summary className="muted">Advanced</summary><label>Care server<br /><input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} style={{ width: '100%' }} aria-label="Care server" /></label></details>
          <label>Email<br /><input value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" style={{ width: '100%' }} /></label><br /><br />
          <label>Password (12 characters or more)<br /><input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" style={{ width: '100%' }} /></label><br /><br />
          <button type="submit" disabled={!email || password.length < 12}>Create account</button>
        </form>
      ) : null}
      {step === 2 ? (
        <form className="card" onSubmit={(e) => { void create(e); }}>
          <p className="muted">Confirmed your address? Name the organisation and pick a plan. No card is taken; the plan is a trial and the prices are not set until the owner sets them.</p>
          <label>Organisation name<br /><input value={org} onChange={(e) => setOrg(e.target.value)} style={{ width: '100%' }} /></label><br /><br />
          <label><input type="radio" checked={plan === 'care_family'} onChange={() => setPlan('care_family')} /> Family: one person, a circle of up to six</label><br />
          <label><input type="radio" checked={plan === 'care_organisation'} onChange={() => setPlan('care_organisation')} /> Organisation: up to fifty people, a circle of up to twenty each</label><br /><br />
          <button type="submit" disabled={org.length < 2}>Create the organisation</button>
        </form>
      ) : null}
      {step === 3 ? (
        <div className="card">
          <p className="ok">Done. You are signed in as the manager of {org}.</p>
          <p><a href="/people">Add the person you look after</a>, then invite the family from the Circle tab and pair the watch from the Watch tab.</p>
        </div>
      ) : null}
      <p className="muted"><a href="/login">Already have an account? Sign in.</a></p>
    </div>
  );
}
