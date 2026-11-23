'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { DeviceClient, DeviceError, heartRateRun, noMovement, wristOff, type DueCheckin } from '@/lib/device';
import { DEFAULT_BASE_URL } from '@/lib/session';

/**
 * A watch in the browser. It talks to the same device API as the real watch,
 * with a device token from a pairing code, so everything it does (check-ins,
 * answers, help, a fall, readings) is the real thing against the real server.
 * The injectors are the demo's hardware: a heart rate, no movement, a fall,
 * taking the watch off.
 */
const POLL_MS = 5_000;
const KEY = 'ira_care_simulator';

type Line = { who: 'ira' | 'you' | 'note'; text: string };

export default function SimulatorPage() {
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
    try { const raw = window.localStorage.getItem(KEY); if (raw) { const s = JSON.parse(raw) as { baseUrl: string; token: string }; setBaseUrl(s.baseUrl); setToken(s.token); } } catch { /* ignore */ }
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
  }, [client, question, say]);

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
    say('I am getting help for you now. Stay where you are.', true);
    try { const a = await client.help(via, location()); note(`SOS opened: ${a.kind} (${a.status}), at home: ${a.at_home === null ? 'unknown' : a.at_home ? 'yes' : 'no'}.`); setQuestion(null); }
    catch (e) { note(`Help could not be sent (${e instanceof Error ? e.message : 'error'}).`); }
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
    if (!client) return;
    try {
      if (what === 'heart') { await client.report(heartRateRun([heartRate, heartRate + 4, heartRate + 7]), { on_wrist: onWrist }); note(`Sent three heart-rate readings of about ${heartRate} bpm, a minute apart.`); }
      if (what === 'still') { await client.report(noMovement(), { on_wrist: onWrist }); note('Sent: no steps, no movement.'); }
      if (what === 'off') { setOnWrist(false); await client.report(wristOff(), { on_wrist: false }); note('Watch taken off the wrist.'); }
      if (what === 'on') { setOnWrist(true); await client.report([{ kind: 'on_wrist', value: 1, unit: '', recorded_at: new Date().toISOString() }], { on_wrist: true }); note('Watch back on the wrist.'); }
      // Readings may have opened a safety question: fetch it now rather than at the next poll.
      const due = await client.due(); if (due && !question) { setQuestion(due); askedAt.current = Date.now(); if (due.safety) vibrate(); say(due.question_text, due.safety); }
    } catch (e) { note(`Could not send (${e instanceof Error ? e.message : 'error'}).`); }
  };

  const fall = async () => {
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
    try { const p = await DeviceClient.pair(baseUrl.replace(/\/$/, ''), code.trim()); setToken(p.device_token); setCode(''); note('Paired. The device token is kept in this browser only.'); }
    catch (e) { note(e instanceof Error ? e.message : 'Pairing failed.'); }
  };

  return (
    <div style={{ maxWidth: 900, margin: '0 auto', padding: 16 }}>
      <h2>Watch simulator</h2>
      <p className="muted">A watch in the browser. It uses the same device API and the same device token as the real watch. Everything here is a demo with fake people; nothing dials emergency services.</p>
      <div className="card row" style={{ gap: 8 }}>
        <label>API <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} style={{ width: 220 }} /></label>
        <label>Device token <input value={token} onChange={(e) => setToken(e.target.value)} placeholder="cd1…" style={{ width: 260 }} data-testid="token" /></label>
        <label>or pairing code <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="cp1…" style={{ width: 200 }} /></label>
        <button className="grey" onClick={() => { void pair(); }} disabled={!code}>Pair</button>
        <span className="muted">{client ? (online === false ? 'offline' : online ? 'online' : 'connecting…') : 'not paired'}</span>
      </div>

      <div className="card" data-testid="watch-face" style={{ border: `6px solid ${flash ? '#c62828' : '#333'}`, borderRadius: 40, padding: 24, textAlign: 'center', background: '#111', color: '#fff', transition: 'border-color .2s' }}>
        <div style={{ fontSize: 22, minHeight: 60 }} data-testid="watch-text">
          {fallCountdown !== null ? <>Did you fall? Are you OK?<br />Help in {fallCountdown} s</> : question ? question.question_text : 'All well'}
        </div>
        <div style={{ display: 'flex', gap: 10, justifyContent: 'center', marginTop: 16 }}>
          <button className="ok" style={{ minWidth: 140, fontSize: 18 }} data-testid="ok" onClick={() => { if (fallCountdown !== null) void fallOk(); else if (question) void answer('', 'button'); }}>I&apos;m OK</button>
          <button style={{ minWidth: 140, fontSize: 18, background: '#c62828', color: '#fff' }} data-testid="help" onClick={() => { if (fallTimer.current) clearInterval(fallTimer.current); setFallCountdown(null); void help(fallCountdown !== null ? 'fall' : 'button'); }}>Help</button>
        </div>
        <div className="row" style={{ justifyContent: 'center', marginTop: 12 }}>
          <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder={question ? 'Type your answer…' : 'Talk to IRA…'} style={{ width: 320 }} data-testid="typed"
            onKeyDown={(e) => { if (e.key === 'Enter' && typed.trim()) { if (question) void answer(typed.trim(), 'voice'); else void talk(typed.trim()); } }} />
          <button className="grey" onClick={() => { if (!typed.trim()) return; if (question) void answer(typed.trim(), 'voice'); else void talk(typed.trim()); }} data-testid="send">{question ? 'Answer' : 'Talk to IRA'}</button>
          <button className="grey" onClick={listen}>🎤 Speak</button>
          <label className="muted"><input type="checkbox" checked={speak} onChange={(e) => setSpeak(e.target.checked)} /> voice</label>
        </div>
      </div>

      <div className="card">
        <strong>Inject (the demo&apos;s hardware)</strong>
        <div className="row" style={{ gap: 8, marginTop: 8 }}>
          <label>Heart rate <input type="number" value={heartRate} onChange={(e) => setHeartRate(Number(e.target.value))} style={{ width: 70 }} /></label>
          <button className="grey" onClick={() => { void inject('heart'); }} data-testid="inject-heart">Send 3 readings over 2 minutes</button>
          <button className="grey" onClick={() => { void inject('still'); }} data-testid="inject-still">No movement</button>
          <button className="grey" onClick={() => { void fall(); }} disabled={fallCountdown !== null} data-testid="inject-fall">Fall</button>
          {onWrist ? <button className="grey" onClick={() => { void inject('off'); }} data-testid="inject-off">Remove watch</button>
            : <button className="grey" onClick={() => { void inject('on'); }}>Put watch back on</button>}
        </div>
        <div className="row" style={{ gap: 8, marginTop: 8 }}>
          <span className="muted">Location sent with an SOS:</span>
          <label>home lat <input value={homeLat} onChange={(e) => setHomeLat(e.target.value)} style={{ width: 90 }} /></label>
          <label>lon <input value={homeLon} onChange={(e) => setHomeLon(e.target.value)} style={{ width: 90 }} /></label>
          <label><input type="checkbox" checked={away} onChange={(e) => setAway(e.target.checked)} /> 2 km away from home</label>
        </div>
      </div>

      <div className="card" data-testid="log">
        <strong>What the watch said and heard</strong>
        {log.length === 0 ? <p className="muted">Nothing yet. Pair, then wait for a question or ask the dashboard to check in now.</p> : null}
        {log.map((l, i) => <p key={i} style={{ margin: '4px 0', color: l.who === 'note' ? '#777' : l.who === 'you' ? '#1a5fb4' : '#1b4d3e' }}>{l.who === 'ira' ? 'IRA: ' : l.who === 'you' ? 'You: ' : ''}{l.text}</p>)}
      </div>
    </div>
  );
}
