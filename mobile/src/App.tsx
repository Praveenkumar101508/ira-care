import Constants from 'expo-constants';
import { StatusBar } from 'expo-status-bar';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert as Dialog, Linking, View } from 'react-native';
import { ApiError, CareClient } from './api/client';
import type { Alert, CircleMember, ConsentList, Person, PersonStatus, Reminder, Visit, Workspace } from './api/types';
import { ThemeProvider } from './appearance/ThemeContext';
import { DEFAULT_APPEARANCE, normaliseAppearance, type Appearance } from './appearance/theme';
import { alertBadge, isWearerOf, pickPerson, type Experience, type Membership, type Tab } from './model/experience';
import { registerForPush, unregisterForPush } from './push';
import { AlertsScreen } from './screens/AlertsScreen';
import { JoinScreen } from './screens/JoinScreen';
import { LoginScreen, type LoginResult } from './screens/LoginScreen';
import { PairWatchScreen } from './screens/PairWatchScreen';
import { PersonScreen } from './screens/PersonScreen';
import { TalkScreen } from './screens/TalkScreen';
import { WorkspaceScreen } from './screens/WorkspaceScreen';
import { HomeTab } from './shell/HomeTab';
import { SettingsTab, pickPhoto } from './shell/SettingsTab';
import { TabBar } from './shell/TabBar';
import { clearAppearance, loadAppearance, loadSession, saveAppearance, saveSession, type StoredSession } from './storage';

const DEFAULT_BASE_URL: string =
  (Constants.expoConfig?.extra as { careApiBaseUrl?: string } | undefined)?.careApiBaseUrl ?? 'http://10.0.2.2:8000';

/** How often the app refreshes while open. */
export const REFRESH_MS = 30_000;

type Overlay = { name: 'talk' } | { name: 'pair' } | null;

export default function App() {
  const [session, setSession] = useState<StoredSession | null | undefined>(undefined);
  const [joining, setJoining] = useState(false);
  const [pendingWorkspaces, setPendingWorkspaces] = useState<LoginResult | null>(null);
  const [tab, setTab] = useState<Tab>('home');
  const [overlay, setOverlay] = useState<Overlay>(null);
  const [people, setPeople] = useState<Person[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [experience, setExperience] = useState<Experience>('family');
  const [memberships, setMemberships] = useState<Membership[]>([]);
  const [canManage, setCanManage] = useState(false);
  const [personId, setPersonId] = useState<string | null>(null);
  const [status, setStatus] = useState<PersonStatus | null>(null);
  const [visits, setVisits] = useState<Visit[]>([]);
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [circle, setCircle] = useState<CircleMember[]>([]);
  const [consents, setConsents] = useState<ConsentList | null>(null);
  const [appearance, setAppearance] = useState<Appearance>(DEFAULT_APPEARANCE);
  const [pushRegistered, setPushRegistered] = useState<boolean | null>(null);
  const [error, setError] = useState('');
  const [signedOutBecause, setSignedOutBecause] = useState('');
  const client = useRef<CareClient | null>(null);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** Which signed-in session an answer belongs to. Bumped on every sign-in and sign-out, so a
   *  reply that arrives late for one account is dropped and never shown to the next. */
  const generation = useRef(0);

  useEffect(() => {
    void loadSession().then((s) => setSession(s));
    void loadAppearance().then((a) => { if (a) setAppearance(normaliseAppearance(a)); });
  }, []);

  useEffect(() => {
    generation.current += 1;
    if (!session) { client.current = null; return; }
    const c = new CareClient({ baseUrl: session.baseUrl, accessToken: session.accessToken, workspaceId: session.workspaceId, refreshToken: session.refreshToken });
    // A renewed access token is kept for next time, without rebuilding the client or re-registering push.
    c.onRenewed = (accessToken) => { void saveSession({ ...session, accessToken }); };
    client.current = c;
    const mine = generation.current;
    void registerForPush(c).then((ok) => { if (generation.current === mine) setPushRegistered(ok); });
  }, [session]);

  /** Everything the last account could see, gone: data, selection, overlays, appearance, pending writes. */
  const forgetAccount = useCallback(() => {
    generation.current += 1;
    if (saveTimer.current) { clearTimeout(saveTimer.current); saveTimer.current = null; }
    setPeople([]); setAlerts([]); setExperience('family'); setMemberships([]); setCanManage(false); setPersonId(null);
    setStatus(null); setVisits([]); setReminders([]); setCircle([]); setConsents(null); setOverlay(null); setTab('home');
    setPushRegistered(null); setError(''); setAppearance(DEFAULT_APPEARANCE); setPendingWorkspaces(null); setJoining(false);
  }, []);

  /** Sign out: the phone stops receiving this account's alerts, then forgets it. Local sign-out
   *  always completes, online or not. */
  const signOut = useCallback(async (because = '') => {
    const c = client.current;
    forgetAccount();
    await unregisterForPush(c);
    await clearAppearance();
    await saveSession(null);
    setSignedOutBecause(because);
    setSession(null);
  }, [forgetAccount]);

  const person = useMemo(() => pickPerson(people, memberships, personId ?? appearance.selected_person_id), [people, memberships, personId, appearance.selected_person_id]);

  const refresh = useCallback(async () => {
    const c = client.current;
    if (!c || !session?.workspaceId) return;
    const mine = generation.current;
    try {
      const [p, a, me, prefs] = await Promise.all([c.people(), c.alerts(false), c.experience(), c.preferences()]);
      if (generation.current !== mine) return; // signed out, or another account, since this was asked
      setPeople(p); setAlerts(a); setExperience(me.experience); setMemberships(me.memberships); setCanManage(me.can_manage);
      if (prefs.appearance && Object.keys(prefs.appearance).length > 0) {
        const merged = normaliseAppearance(prefs.appearance);
        setAppearance(merged);
        void saveAppearance(merged);
      }
      setError('');
    } catch (e) {
      if (generation.current !== mine) return;
      // The sign-in could not be renewed: said plainly on the sign-in screen, never a silent sign-out.
      if (e instanceof ApiError && e.status === 401) { await signOut('Your sign-in has expired. Please sign in again to see alerts.'); return; }
      setError(e instanceof ApiError ? e.message : 'Could not refresh.');
    }
  }, [session, signOut]);

  const refreshPerson = useCallback(async () => {
    const c = client.current;
    if (!c || !person) return;
    const mine = generation.current;
    try {
      const [s, v, r, m, k] = await Promise.all([c.status(person.id), c.visits(person.id), c.reminders(person.id), c.circle(person.id), c.consents(person.id)]);
      if (generation.current !== mine) return;
      setStatus(s); setVisits(v); setReminders(r); setCircle(m); setConsents(k);
    } catch (e) {
      if (generation.current !== mine) return;
      setError(e instanceof ApiError ? e.message : 'Could not refresh.');
    }
  }, [person]);

  useEffect(() => {
    if (!session?.workspaceId) return;
    void refresh();
    const timer = setInterval(() => { void refresh(); }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [session, refresh]);
  useEffect(() => { void refreshPerson(); }, [refreshPerson, alerts]);

  /** Appearance changes apply at once, are kept on this phone, and reach the server a moment later. */
  const changeAppearance = (a: Appearance) => {
    setAppearance(a);
    void saveAppearance(a);
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => { void client.current?.putPreferences(a).catch(() => {}); }, 600);
  };

  const finishLogin = async (result: LoginResult, workspace: Workspace) => {
    const s: StoredSession = {
      baseUrl: result.baseUrl, accessToken: result.accessToken, refreshToken: result.refreshToken,
      workspaceId: workspace.id, workspaceName: workspace.name, username: result.username,
    };
    setCanManage(workspace.role === 'workspace_admin');
    await saveSession(s);
    setPendingWorkspaces(null);
    setSignedOutBecause('');
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
      <ThemeProvider appearance={appearance}><StatusBar style="auto" />
        <JoinScreen baseUrl={DEFAULT_BASE_URL} onBack={() => setJoining(false)}
          onJoined={(j) => { void finishLogin({ baseUrl: DEFAULT_BASE_URL, accessToken: j.accessToken, refreshToken: j.refreshToken, username: j.username, workspaces: [] }, { id: j.workspaceId ?? '', name: 'Care team', role: 'member', slug: 'care-team', is_default: true, status: 'active' } as Workspace); }} />
      </ThemeProvider>
    );
  }
  if (!session) return <ThemeProvider appearance={appearance}><StatusBar style="auto" /><LoginScreen defaultBaseUrl={DEFAULT_BASE_URL} onLoggedIn={onLoggedIn} onJoin={() => setJoining(true)} notice={signedOutBecause} /></ThemeProvider>;

  const c = client.current!;
  const act = async (fn: () => Promise<unknown>) => { try { await fn(); await refresh(); await refreshPerson(); } catch (e) { setError(e instanceof ApiError ? e.message : 'Failed.'); } };
  const switchPerson = (p: Person) => { setPersonId(p.id); changeAppearance({ ...appearance, selected_person_id: p.id }); };

  /** Help from the phone is a two-step action: the second step is the confirmation. */
  const help = () => {
    if (!person) return;
    Dialog.alert('Ask for help?', 'Your family and carers will be told now, with where you are.',
      [{ text: 'Not now', style: 'cancel' }, { text: 'Yes, get help', style: 'destructive', onPress: () => { void act(() => c.phoneHelp(person.id)); setTab('alerts'); } }]);
  };

  const body = overlay?.name === 'talk' && person && isWearerOf(memberships, person.id) ? (
    <TalkScreen person={person} onSend={(t) => c.talk(person.id, t)} onBack={() => setOverlay(null)} />
  ) : overlay?.name === 'pair' && person ? (
    <PairWatchScreen client={c} person={person} onBack={() => { setOverlay(null); void refreshPerson(); }} />
  ) : tab === 'home' ? (
    <HomeTab
      d={{ experience, memberships, people, person, device: status?.devices[0], alerts, visits, reminders, lastCheckin: status?.last_checkin, cardOrder: appearance.card_order, error }}
      a={{ onHelp: help, onTalk: () => setOverlay({ name: 'talk' }), onAlerts: () => setTab('alerts'), onCare: () => setTab('care'), onSettings: () => setTab('settings'),
        onSwitchPerson: switchPerson, onOnMyWay: (al) => { void act(() => c.acknowledge(al.id)); },
        onArrive: (v) => { void act(() => c.arrive(v.id)); }, onDepart: () => setTab('care'),
        onReminder: (r, action) => { void act(() => c.actOnReminder(r.id, action)); } }} />
  ) : tab === 'care' ? (
    person ? (
      <PersonScreen key={person.id} client={c} person={person} canManage={canManage} onBack={() => setTab('home')}
        onTalk={isWearerOf(memberships, person.id) ? () => setOverlay({ name: 'talk' }) : undefined} onPair={canManage ? () => setOverlay({ name: 'pair' }) : undefined} />
    ) : <HomeTab d={{ experience, memberships, people, alerts, visits: [], reminders: [], cardOrder: [], error: 'No one is set up yet. A carer adds people from the dashboard.' }}
      a={{ onHelp: () => {}, onTalk: () => {}, onAlerts: () => setTab('alerts'), onCare: () => {}, onSettings: () => setTab('settings'), onSwitchPerson: switchPerson, onOnMyWay: () => {}, onArrive: () => {}, onDepart: () => {}, onReminder: () => {} }} />
  ) : tab === 'alerts' ? (
    <AlertsScreen alerts={alerts.filter((x) => x.status !== 'resolved')} people={people}
      onAcknowledge={async (al) => { await c.acknowledge(al.id); await refresh(); }}
      onArrived={async (al) => { await c.arrived(al.id); await refresh(); }}
      onResolve={async (al, outcome) => { await c.resolve(al.id, outcome === 'all_well' ? 'All well; resolved from the app.' : 'Needed help; resolved from the app.', outcome); await refresh(); }}
      onBack={() => setTab('home')}
      onCall999={() => { void Linking.openURL('tel:999'); }}
      onAccessNotes={(al) => c.accessNotes(al.id)} />
  ) : (
    <SettingsTab appearance={appearance} onAppearance={changeAppearance} experience={experience} person={person} device={status?.devices[0]}
      circle={circle} consents={consents} pushRegistered={pushRegistered} onRegisterPush={() => { void registerForPush(c).then(setPushRegistered); }}
      username={session.username} server={session.baseUrl} onSignOut={() => { void signOut(); }} onPairWatch={() => setOverlay({ name: 'pair' })}
      onOpenCare={() => setTab('care')} onPickPhoto={pickPhoto} />
  );

  return (
    <ThemeProvider appearance={appearance}>
      <StatusBar style="auto" />
      <View style={{ flex: 1 }}>
        <View style={{ flex: 1 }}>{body}</View>
        {overlay ? null : <TabBar tab={tab} onTab={(t) => { setTab(t); }} alertCount={alertBadge(alerts.filter((x) => x.status !== 'resolved'), memberships)} />}
      </View>
    </ThemeProvider>
  );
}
