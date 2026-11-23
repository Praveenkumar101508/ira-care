import Constants from 'expo-constants';
import { StatusBar } from 'expo-status-bar';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Linking } from 'react-native';
import { ApiError, CareClient } from './api/client';
import type { Alert, Person, Workspace } from './api/types';
import { registerForPush } from './push';
import { AlertsScreen } from './screens/AlertsScreen';
import { HomeScreen } from './screens/HomeScreen';
import { LoginScreen, type LoginResult } from './screens/LoginScreen';
import { PersonScreen } from './screens/PersonScreen';
import { TalkScreen } from './screens/TalkScreen';
import { PairWatchScreen } from './screens/PairWatchScreen';
import { JoinScreen } from './screens/JoinScreen';
import { WorkspaceScreen } from './screens/WorkspaceScreen';
import { loadSession, saveSession, type StoredSession } from './storage';

type Route = { name: 'home' } | { name: 'alerts' } | { name: 'person'; person: Person } | { name: 'talk'; person: Person } | { name: 'pair'; person: Person };

const DEFAULT_BASE_URL: string =
  (Constants.expoConfig?.extra as { careApiBaseUrl?: string } | undefined)?.careApiBaseUrl ?? 'http://10.0.2.2:8000';

/** How often the home screen refreshes alerts while open. */
export const REFRESH_MS = 30_000;

export default function App() {
  const [session, setSession] = useState<StoredSession | null | undefined>(undefined);
  const [joining, setJoining] = useState(false);
  const [pendingWorkspaces, setPendingWorkspaces] = useState<LoginResult | null>(null);
  const [route, setRoute] = useState<Route>({ name: 'home' });
  const [people, setPeople] = useState<Person[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [role, setRole] = useState('');
  const [error, setError] = useState('');
  const client = useRef<CareClient | null>(null);

  useEffect(() => { void loadSession().then((s) => setSession(s)); }, []);

  useEffect(() => {
    if (!session) { client.current = null; return; }
    client.current = new CareClient({ baseUrl: session.baseUrl, accessToken: session.accessToken, workspaceId: session.workspaceId });
    void registerForPush(client.current);
  }, [session]);

  const refresh = useCallback(async () => {
    const c = client.current;
    if (!c || !session?.workspaceId) return;
    try {
      const [p, a] = await Promise.all([c.people(), c.alerts(true)]);
      setPeople(p); setAlerts(a); setError('');
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) { await saveSession(null); setSession(null); return; }
      setError(e instanceof ApiError ? e.message : 'Could not refresh.');
    }
  }, [session]);

  useEffect(() => {
    if (!session?.workspaceId) return;
    void refresh();
    const timer = setInterval(() => { void refresh(); }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [session, refresh]);

  const finishLogin = async (result: LoginResult, workspace: Workspace) => {
    const s: StoredSession = {
      baseUrl: result.baseUrl, accessToken: result.accessToken, refreshToken: result.refreshToken,
      workspaceId: workspace.id, workspaceName: workspace.name, username: result.username,
    };
    setRole(workspace.role);
    await saveSession(s);
    setPendingWorkspaces(null);
    setSession(s);
  };

  const onLoggedIn = (result: LoginResult) => {
    if (result.workspaces.length === 1) void finishLogin(result, result.workspaces[0]);
    else setPendingWorkspaces(result);
  };

  if (session === undefined) return null;
  if (pendingWorkspaces) {
    return <WorkspaceScreen workspaces={pendingWorkspaces.workspaces} onPick={(w) => { void finishLogin(pendingWorkspaces, w); }} />;
  }
  if (!session && joining) {
    return (
      <><StatusBar style="dark" />
        <JoinScreen baseUrl={DEFAULT_BASE_URL} onBack={() => setJoining(false)}
          onJoined={(j) => { void finishLogin({ baseUrl: DEFAULT_BASE_URL, accessToken: j.accessToken, refreshToken: j.refreshToken, username: j.username, workspaces: [] }, { id: j.workspaceId ?? '', name: 'Care team', role: 'member', slug: 'care-team', is_default: true, status: 'active' } as Workspace); }} />
      </>
    );
  }
  if (!session) return <><StatusBar style="dark" /><LoginScreen defaultBaseUrl={DEFAULT_BASE_URL} onLoggedIn={onLoggedIn} onJoin={() => setJoining(true)} /></>;

  const c = client.current!;
  const canManage = role === 'workspace_admin';
  const signOut = async () => { await saveSession(null); setSession(null); setRoute({ name: 'home' }); };

  return (
    <>
      <StatusBar style="dark" />
      {route.name === 'home' && (
        <HomeScreen people={people} alerts={alerts} workspaceName={session.workspaceName ?? ''} error={error}
          onOpenPerson={(person) => setRoute({ name: 'person', person })}
          onOpenAlerts={() => setRoute({ name: 'alerts' })} onRefresh={() => { void refresh(); }} onSignOut={() => { void signOut(); }} />
      )}
      {route.name === 'alerts' && (
        <AlertsScreen alerts={alerts} people={people}
          onAcknowledge={async (a) => { await c.acknowledge(a.id); await refresh(); }}
          onArrived={async (a) => { await c.arrived(a.id); await refresh(); }}
          onResolve={async (a, outcome) => { await c.resolve(a.id, outcome === 'all_well' ? 'All well; resolved from the family app.' : 'Needed help; resolved from the family app.', outcome); await refresh(); }}
          onBack={() => setRoute({ name: 'home' })}
          onCall999={() => { void Linking.openURL('tel:999'); }}
          onAccessNotes={(a) => c.accessNotes(a.id)} />
      )}
      {route.name === 'person' && (
        <PersonScreen client={c} person={route.person} canManage={canManage} onBack={() => setRoute({ name: 'home' })}
          onTalk={() => setRoute({ name: 'talk', person: route.person })}
          onPair={canManage ? () => setRoute({ name: 'pair', person: route.person }) : undefined} />
      )}
      {route.name === 'pair' && (
        <PairWatchScreen client={c} person={route.person} onBack={() => setRoute({ name: 'person', person: route.person })} />
      )}
      {route.name === 'talk' && (
        <TalkScreen person={route.person} onSend={(t) => c.talk(route.person.id, t)} onBack={() => setRoute({ name: 'person', person: route.person })} />
      )}
    </>
  );
}
