'use client';

import { useEffect, useMemo, useState } from 'react';
import { Api } from '@/lib/api';
import { loadSession, saveSession, type StoredSession } from '@/lib/session';

/** The signed-in API, or a redirect to the login page. */
export function useApi(): { api: Api | null; session: StoredSession | null; ready: boolean } {
  const [session, setSession] = useState<StoredSession | null>(null);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const s = loadSession();
    if (!s) { window.location.href = '/login'; return; }
    setSession(s); setReady(true);
  }, []);
  const api = useMemo(() => {
    if (!session) return null;
    const a = new Api(session);
    // Keep the renewed token for the next page, without re-rendering this one.
    a.onRenewed = (accessToken) => { const now = loadSession(); if (now) saveSession({ ...now, accessToken }); };
    return a;
  }, [session]);
  return { api, session, ready };
}

export function canManage(session: StoredSession | null): boolean {
  return session?.role === 'workspace_admin';
}
