'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect, useState } from 'react';
import { Api } from '@/lib/api';
import { loadSession, saveSession, type StoredSession } from '@/lib/session';
import type { OpsStatus } from '@/lib/types';

/** The role in a person's words, not the workspace's. */
export function roleWords(role: string): string {
  return role === 'workspace_admin' ? 'care manager' : role === 'viewer' ? 'viewer' : 'team member';
}

export function TopBar() {
  const pathname = usePathname();
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
  // The watch is a watch: no dashboard header around it.
  if (pathname?.startsWith('/watch')) return null;
  return (
    <>
    {ops?.monitoring_paused ? (
      <div role="alert" data-testid="monitoring-paused" style={{ background: 'var(--sos)', color: '#fff', padding: '10px 20px', fontWeight: 700 }}>
        Monitoring paused: the care jobs have not run for over {Math.round(ops.stale_after_s / 60)} minutes. Check-ins and alerts are not being processed. The operations contact has been told.
      </div>
    ) : null}
    <header className="top" role="banner">
      <h1>IRA CARE</h1>
      {session ? (
        <>
          <nav aria-label="Main">
            <Link href="/">People</Link>
            <Link href="/alerts">Alerts</Link>
        {session.role === 'workspace_admin' ? <Link href="/billing">Plan</Link> : null}
          </nav>
          <span className="muted who">{session.workspaceName} · {roleWords(session.role)}</span>
          <button className="grey" onClick={() => { saveSession(null); window.location.href = '/login'; }}>Sign out</button>
        </>
      ) : null}
    </header>
    </>
  );
}
