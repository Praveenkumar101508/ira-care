'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { loadSession, saveSession, type StoredSession } from '@/lib/session';

export function TopBar() {
  const [session, setSession] = useState<StoredSession | null>(null);
  useEffect(() => { setSession(loadSession()); }, []);
  return (
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
  );
}
