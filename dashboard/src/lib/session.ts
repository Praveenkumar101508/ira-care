'use client';

import type { Session } from './api';

const KEY = 'ira_care_dashboard_session';

export interface StoredSession extends Session { workspaceName: string; username: string }

export function loadSession(): StoredSession | null {
  if (typeof window === 'undefined') return null;
  try { const raw = window.sessionStorage.getItem(KEY); return raw ? (JSON.parse(raw) as StoredSession) : null; } catch { return null; }
}

export function saveSession(s: StoredSession | null): void {
  if (typeof window === 'undefined') return;
  try { if (s) window.sessionStorage.setItem(KEY, JSON.stringify(s)); else window.sessionStorage.removeItem(KEY); } catch { /* ignore */ }
}

export const DEFAULT_BASE_URL = process.env.NEXT_PUBLIC_CARE_API_BASE_URL ?? 'http://localhost:8000';
