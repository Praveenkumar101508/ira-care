'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { Api } from '@/lib/api';
import { loadSession, saveSession, type StoredSession } from '@/lib/session';
import type { OpsStatus } from '@/lib/types';

export function TopBar() {
  const [session, setSession] = useState<StoredSession | null>(null);
  const [ops, setOps] = useState<OpsStatus | null>(null);
  useEffect(() => { setSession(loadSession()); }, []);
  useEffect(() => {
    if (!session) return;
    const api = new Api(session);
    const poll = () => { api.opsStatus().then(setOps).catch(() => setOps(null)); };
    poll();
    const t = setInterval(poll, 60_000);
    return () => clearInterval(t);
  }, [session]);
  return (
    <>
    {ops?.monitoring_paused ? (
      <div role="alert" data-testid="monitoring-paused" style={{ background: 'var(--sos)', color: '#fff', padding: '10px 20px', fontWeight: 700 }}>
        Monitoring paused: the care jobs have not run for over {Math.round(ops.stale_after_s / 60)} minutes. Check-ins and alerts are not being processed. The operations contact has been told.
      </div>
    ) : null}
    <header className="top">
      <h1>IRA CARE</h1>
      {session ? (
        <>
          <nav>
            <Link href="/">People</Link>
            <Link href="/alerts">Alerts</Link>
          </nav>
          <span className="muted" style={{ marginLeft: 'auto' }}>{session.workspaceName} · {session.role.replace('_', ' ')}</span>
          <button className="grey" onClick={() => { saveSession(null); window.location.href = '/login'; }}>Sign out</button>
        </>
      ) : null}
    </header>
    </>
  );
}
