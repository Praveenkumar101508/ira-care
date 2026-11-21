'use client';

import { useEffect, useMemo, useState } from 'react';
import { Api } from '@/lib/api';
import { loadSession, type StoredSession } from '@/lib/session';

/** The signed-in API, or a redirect to the login page. */
export function useApi(): { api: Api | null; session: StoredSession | null; ready: boolean } {
  const [session, setSession] = useState<StoredSession | null>(null);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const s = loadSession();
    if (!s) { window.location.href = '/login'; return; }
    setSession(s); setReady(true);
  }, []);
  const api = useMemo(() => (session ? new Api(session) : null), [session]);
  return { api, session, ready };
}

export function canManage(session: StoredSession | null): boolean {
  return session?.role === 'workspace_admin';
}
