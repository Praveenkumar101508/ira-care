
import { useCallback, useEffect, useRef, useState } from 'react';
import { DeviceClient, DeviceError, heartRateRun, noMovement, outOfRange, wristOff, type DueCheckin, type ReadingSettings, type SosState } from '@/lib/device';
import { DEFAULT_BASE_URL } from '@/lib/session';

/**
 * The browser watch's state and actions, shared by the watch page (what the
 * person sees) and the presenter page (the controls a demo needs). A watch in the browser. It talks to the same device API as the real watch,
 * with a device token from a pairing code, so everything it does (check-ins,
 * answers, help, a fall, readings) is the real thing against the real server.
 * The injectors are the demo's hardware: a heart rate, no movement, a fall,
 * taking the watch off.
 */
const POLL_MS = 5_000;
/** How long the watch keeps saying a closed SOS is resolved before it returns to "All well". */
export const RESOLVED_WORDS_MS = 60_000;
const KEY = 'ira_care_simulator';

type Line = { who: 'ira' | 'you' | 'note'; text: string };

type Command =
  | { type: 'hello' }
  | { type: 'credentials'; baseUrl: string; token: string }
  | { type: 'pair'; baseUrl: string; code: string }
  | { type: 'inject'; what: 'heart' | 'still' | 'off' | 'on'; heartRate: number }
  | { type: 'fall' }
  | { type: 'set'; homeLat?: string; homeLon?: string; away?: boolean; speak?: boolean }
  | { type: 'mirror'; log: Line[]; online: boolean | null; onWrist: boolean; fallCountdown: number | null; paired: boolean; baseUrl: string };

/**
 * `role` 'watch' runs the watch; 'presenter' drives a watch open in another
 * tab of the same browser over a BroadcastChannel and mirrors what it says, so
 * the recording shows only the watch.
 */
export function useBrowserWatch(role: 'watch' | 'presenter' = 'watch') {
  const [linked, setLinked] = useState(false);
  const [paired, setPaired] = useState(false);
  const channel = useRef<BroadcastChannel | null>(null);
  const actions = useRef<{ inject: (w: 'heart' | 'still' | 'off' | 'on') => Promise<void>; fall: () => Promise<void>; pair: () => Promise<void> } | null>(null);
  const post = (m: Command) => { try { channel.current?.postMessage(m); } catch { /* no channel */ } };
  const [baseUrl, setBaseUrl] = useState(DEFAULT_BASE_URL);
  const [token, setToken] = useState('');
  const [code, setCode] = useState('');
  const [client, setClient] = useState<DeviceClient | null>(null);
  const [question, setQuestion] = useState<DueCheckin | null>(null);
  const [typed, setTyped] = useState('');
  const [log, setLog] = useState<Line[]>([]);
  const [online, setOnline] = useState<boolean | null>(null);
  const [onWrist, setOnWrist] = useState(true);
  const [heartRate, setHeartRate] = useState(160);
  const [fallCountdown, setFallCountdown] = useState<number | null>(null);
  const [flash, setFlash] = useState(false);
  const [speak, setSpeak] = useState(true);
  const [homeLat, setHomeLat] = useState('51.5000');
  const [homeLon, setHomeLon] = useState('-0.1200');
  const [away, setAway] = useState(false);
  const [sos, setSos] = useState<SosState | null>(null);
  const closedSince = useRef<{ id: string | null; at: number } | null>(null);
  /** A closed SOS is said to be resolved for a minute, then the face returns to the day. */
  const applySos = (s: SosState | null) => {
    if (s && s.state === 'closed') {
      if (!closedSince.current || closedSince.current.id !== s.alert_id) closedSince.current = { id: s.alert_id, at: Date.now() };
      if (Date.now() - closedSince.current.at > RESOLVED_WORDS_MS) { setSos(null); return; }
    } else closedSince.current = null;
    setSos(s);
  };
  const [sosPending, setSosPending] = useState<'sending' | 'waiting' | null>(null);
  const [ranges, setRanges] = useState<ReadingSettings | null>(null);
  const [queuedHelp, setQueuedHelp] = useState<{ key: string; via: 'button' | 'voice' | 'fall' } | null>(null);
  const fallTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const askedAt = useRef<number>(0);
  const repeatedFor = useRef<string | null>(null);

  const say = useCallback((text: string, loud = false) => {
    setLog((l) => [...l.slice(-60), { who: 'ira', text }]);
    if (speak && typeof window !== 'undefined' && 'speechSynthesis' in window) {
      const u = new SpeechSynthesisUtterance(text); u.rate = 0.95; u.volume = loud ? 1 : 0.8; u.lang = 'en-GB';
      window.speechSynthesis.speak(u);
    }
  }, [speak]);
  const note = (text: string) => setLog((l) => [...l.slice(-60), { who: 'note', text }]);
  const vibrate = () => { setFlash(true); setTimeout(() => setFlash(false), 600); if (typeof navigator !== 'undefined' && navigator.vibrate) navigator.vibrate(300); };

  useEffect(() => {
    try { const raw = window.localStorage.getItem(KEY); if (raw) { const s = JSON.parse(raw) as { baseUrl: string; token: string }; setBaseUrl(s.baseUrl || DEFAULT_BASE_URL); setToken(s.token); } } catch { /* ignore */ }
  }, []);
  useEffect(() => {
    if (!token) { setClient(null); return; }
    setClient(new DeviceClient(baseUrl, token));
    try { window.localStorage.setItem(KEY, JSON.stringify({ baseUrl, token })); } catch { /* ignore */ }
  }, [baseUrl, token]);

  const location = () => {
    const lat = Number(homeLat), lon = Number(homeLon);
    if (Number.isNaN(lat) || Number.isNaN(lon)) return undefined;
    return away ? { latitude: lat + 0.018, longitude: lon, accuracy_m: 12 } : { latitude: lat + 0.0002, longitude: lon + 0.0001, accuracy_m: 20 };
  };

  // The watch's loop: is a question due? Repeat it after a while; give up later.
  useEffect(() => {
    if (!client) return;
    let stop = false;
    const tick = async () => {
      if (stop) return;
      try {
        // The SOS line is the server's word: asked on every tick, like the watch's held stream.
        const state = await client.sos(); applySos(state);
        if (queuedHelp) {
          // The outbox: a Help request that could not be sent is re-sent with the same key.
          try { await client.help(queuedHelp.via, location(), queuedHelp.key); setQueuedHelp(null); setSosPending(null); note('The saved help request has reached the server.'); } catch { setSosPending('waiting'); }
        }
        if (!ranges) { const me = await client.me(); setRanges(me.readings); }
        if (!question) {
          const due = await client.due();
          setOnline(true);
          if (due) {
            setQuestion(due); askedAt.current = Date.now(); repeatedFor.current = null;
            if (due.safety) vibrate();
            say(due.question_text, due.safety);
          }
        } else {
          const age = Date.now() - askedAt.current;
          const repeatMs = question.safety ? 20_000 : 60_000, missMs = question.safety ? 45_000 : 180_000;
          if (age > missMs) { await client.missed(question.id); note('No answer: reported as missed.'); setQuestion(null); }
          else if (age > repeatMs && repeatedFor.current !== question.id) { repeatedFor.current = question.id; vibrate(); say(question.question_text, true); await client.repeated(question.id); }
        }
      } catch (e) { setOnline(!(e instanceof DeviceError && e.status === 0)); if (e instanceof DeviceError && e.status === 401) note('The device token was not accepted. Pair again.'); }
    };
    void tick();
    const t = setInterval(() => { void tick(); }, POLL_MS);
    return () => { stop = true; clearInterval(t); };
  }, [client, question, say, queuedHelp, ranges]);

  const answer = async (text: string, via: 'button' | 'voice') => {
    if (!client || !question) return;
    setLog((l) => [...l, { who: 'you', text: text || "(I'm OK)" }]);
    try { await client.answer(question.id, text, via); note('Answer sent.'); } catch { note('Could not send the answer; the watch would keep it and retry.'); }
    setQuestion(null); setTyped('');
  };

  const listen = () => {
    const W = window as unknown as { webkitSpeechRecognition?: new () => { lang: string; onresult: (e: { results: { 0: { 0: { transcript: string } } } }) => void; onerror: () => void; start: () => void } };
    if (!W.webkitSpeechRecognition) { note('This browser has no speech recognition; type the answer instead.'); return; }
    const r = new W.webkitSpeechRecognition(); r.lang = 'en-GB';
    r.onresult = (e) => { const heard = e.results[0][0].transcript; if (question) void answer(heard, 'voice'); else void talk(heard); };
    r.onerror = () => note('Did not catch that.');
    r.start(); note('Listening…');
  };

  const help = async (via: 'button' | 'voice' | 'fall') => {
    if (!client) return;
    // Like the watch: the request is saved first (with its key), the person is told it is being sent,
    // and "received" or "[Name] is on the way" come only from the server.
    const key = (typeof crypto !== 'undefined' && 'randomUUID' in crypto) ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    setSosPending('sending'); say('Sending your request for help. Stay where you are.', true);
    try {
      const a = await client.help(via, location(), key);
      setSosPending(null); if (a.sos) applySos(a.sos);
      note(`SOS opened: ${a.kind} (${a.status}), at home: ${a.at_home === null ? 'unknown' : a.at_home ? 'yes' : 'no'}.`); setQuestion(null);
      say('Help request received.', true);
    } catch (e) {
      if (e instanceof DeviceError && e.status === 0) { setQueuedHelp({ key, via }); setSosPending('waiting'); say('Waiting for connection. Your help request is saved and will be sent.', true); }
      else { setSosPending(null); note(`Help could not be sent (${e instanceof Error ? e.message : 'error'}).`); }
    }
  };

  const talk = async (text: string) => {
    if (!client) return;
    setLog((l) => [...l, { who: 'you', text }]);
    try {
      const r = await client.talk(text, (s) => say(s));
      if (r.action?.kind === 'call') note(`Shows the contact ${String(r.action.name)}; the phone would dial.`);
      if (r.action?.kind === 'safety_check') { const due = await client.due(); if (due) { setQuestion(due); askedAt.current = Date.now(); vibrate(); say(due.question_text, true); } }
    } catch { note('IRA could not answer right now.'); }
    setTyped('');
  };

  const inject = async (what: 'heart' | 'still' | 'off' | 'on') => {
    if (role === 'presenter') { post({ type: 'inject', what, heartRate }); return; }
    if (!client) return;
    try {
      if (what === 'heart') {
        const run = heartRateRun([heartRate, heartRate + 4, heartRate + 7]);
        const urgent = run.some((r) => outOfRange(r.value, ranges));
        await client.report(run, { on_wrist: onWrist });
        note(urgent ? `Heart rate ${heartRate} bpm is outside the range set for this person: three samples uploaded at once, not at the next interval.` : `Sent three heart-rate readings of about ${heartRate} bpm, a minute apart.`);
      }
      if (what === 'still') { await client.report(noMovement(), { on_wrist: onWrist }); note('Sent: no steps, no movement.'); }
      if (what === 'off') { setOnWrist(false); await client.report(wristOff(), { on_wrist: false }); note('Watch taken off the wrist.'); }
      if (what === 'on') { setOnWrist(true); await client.report([{ kind: 'on_wrist', value: 1, unit: '', recorded_at: new Date().toISOString() }], { on_wrist: true }); note('Watch back on the wrist.'); }
      // Readings may have opened a safety question: fetch it now rather than at the next poll.
      const due = await client.due(); if (due && !question) { setQuestion(due); askedAt.current = Date.now(); if (due.safety) vibrate(); say(due.question_text, due.safety); }
    } catch (e) { note(`Could not send (${e instanceof Error ? e.message : 'error'}).`); }
  };

  const fall = async () => {
    if (role === 'presenter') { post({ type: 'fall' }); return; }
    if (!client || fallCountdown !== null) return;
    let seconds = 30;
    try { const me = await client.me(); if (!me.fall.fall_detection_enabled) { note('Fall detection is turned off for this person.'); return; } seconds = me.fall.fall_countdown_s; await client.fallStarted(3.4, 20); }
    catch (e) { note(`The server refused the fall start (${e instanceof Error ? e.message : 'error'}); the countdown runs anyway and Help will carry it.`); }
    setFallCountdown(seconds); vibrate(); say("It looks like you may have fallen. Are you OK? Tap I'm OK, or Help.", true);
    fallTimer.current = setInterval(() => {
      setFallCountdown((s) => {
        if (s === null) return null;
        if (s <= 1) { if (fallTimer.current) clearInterval(fallTimer.current); void help('fall'); return null; }
        if ((s - 1) % 10 === 0) { vibrate(); say(`Are you OK? ${s - 1} seconds.`, true); }
        return s - 1;
      });
    }, 1000);
  };
  const fallOk = async () => { if (fallTimer.current) clearInterval(fallTimer.current); setFallCountdown(null); try { await client?.fallOk(); } catch { /* the server may have no asking alert */ } say('Good. I will leave you be.'); };

  const pair = async () => {
    if (role === 'presenter') { post({ type: 'pair', baseUrl, code: code.trim() }); setCode(''); return; }
    try { const p = await DeviceClient.pair(baseUrl.replace(/\/$/, ''), code.trim()); setToken(p.device_token); setCode(''); note('Paired. The device token is kept in this browser only.'); }
    catch (e) { note(e instanceof Error ? e.message : 'Pairing failed.'); }
  };

  actions.current = { inject, fall, pair };
  useEffect(() => {
    if (typeof BroadcastChannel === 'undefined') return;
    const ch = new BroadcastChannel('ira_care_watch');
    channel.current = ch;
    ch.onmessage = (e: MessageEvent<Command>) => {
      const m = e.data;
      if (role === 'watch') {
        if (m.type === 'hello') setLog((l) => [...l]);
        else if (m.type === 'credentials') { setBaseUrl(m.baseUrl); setToken(m.token); }
        else if (m.type === 'pair') { setBaseUrl(m.baseUrl); setCode(m.code); setTimeout(() => { void actions.current?.pair(); }, 50); }
        else if (m.type === 'inject') { setHeartRate(m.heartRate); setTimeout(() => { void actions.current?.inject(m.what); }, 50); }
        else if (m.type === 'fall') void actions.current?.fall();
        else if (m.type === 'set') { if (m.homeLat !== undefined) setHomeLat(m.homeLat); if (m.homeLon !== undefined) setHomeLon(m.homeLon); if (m.away !== undefined) setAway(m.away); if (m.speak !== undefined) setSpeak(m.speak); }
      } else if (m.type === 'mirror') {
        setLinked(true); setLog(m.log); setOnline(m.online); setOnWrist(m.onWrist); setFallCountdown(m.fallCountdown); setPaired(m.paired); setBaseUrl((b) => b || m.baseUrl);
      }
    };
    if (role === 'presenter') ch.postMessage({ type: 'hello' } as Command);
    return () => { ch.close(); channel.current = null; };
  }, [role]);
  useEffect(() => {
    if (role !== 'watch') return;
    post({ type: 'mirror', log, online, onWrist, fallCountdown, paired: client !== null, baseUrl });
  }, [role, log, online, onWrist, fallCountdown, client, baseUrl]);
  const presenterSetToken = (t: string) => { setToken(t); if (role === 'presenter') post({ type: 'credentials', baseUrl: baseUrl || DEFAULT_BASE_URL, token: t }); };
  const presenterSetBaseUrl = (b: string) => { setBaseUrl(b); if (role === 'presenter' && token) post({ type: 'credentials', baseUrl: b, token }); };
  const presenterSet = (v: { homeLat?: string; homeLon?: string; away?: boolean; speak?: boolean }) => {
    if (v.homeLat !== undefined) setHomeLat(v.homeLat); if (v.homeLon !== undefined) setHomeLon(v.homeLon); if (v.away !== undefined) setAway(v.away); if (v.speak !== undefined) setSpeak(v.speak);
    if (role === 'presenter') post({ type: 'set', ...v });
  };

  return { role, linked, paired: role === 'presenter' ? paired : client !== null, presenterSetToken, presenterSetBaseUrl, presenterSet, baseUrl, setBaseUrl, token, setToken, code, setCode, client, question, typed, setTyped, log, online, onWrist, heartRate, setHeartRate,
    fallCountdown, flash, speak, setSpeak, homeLat, setHomeLat, homeLon, setHomeLon, away, setAway, sos, sosPending, ranges, queuedHelp,
    say, note, answer, talk, help, fall, fallOk, inject, pair, listen };
}
