'use client';

import { useBrowserWatch } from '@/lib/browserWatch';

/**
 * The presenter's controls for the browser watch: pairing, the demo's
 * "hardware" (a heart rate, no movement, a fall, taking the watch off), the
 * location sent with an SOS, and what the watch said and heard. Open /watch
 * in another window for what the person sees. This page is never shown in a
 * recording.
 */
export default function PresenterPage() {
  const w = useBrowserWatch('presenter');
  return (
    <div style={{ maxWidth: 900, margin: '0 auto', padding: 16 }} data-testid="presenter">
      <h2>Presenter controls</h2>
      <p className="muted">For demonstrations only. Open the watch itself at <a href="/watch" target="_blank" rel="noreferrer">/watch</a> in another tab of this browser; these controls drive it from here. Everything here is a demonstration with made-up people; nothing dials emergency services.</p>
      <div className="card row" style={{ gap: 8 }}>
        <label>Server <input value={w.baseUrl} onChange={(e) => w.presenterSetBaseUrl(e.target.value)} style={{ width: 220 }} aria-label="Server" /></label>
        <label>Device key <input value={w.token} onChange={(e) => w.presenterSetToken(e.target.value)} style={{ width: 260 }} data-testid="token" aria-label="Device key" /></label>
        <label>or pairing code <input value={w.code} onChange={(e) => w.setCode(e.target.value)} style={{ width: 200 }} aria-label="Pairing code" /></label>
        <button className="grey" onClick={() => { void w.pair(); }} disabled={!w.code}>Pair</button>
        <span className="muted" data-testid="link-state">{!w.linked ? 'open /watch in another tab of this browser' : w.paired ? (w.online === false ? 'watch paired, offline' : w.online ? 'watch paired, online' : 'watch connecting…') : 'watch not paired'}</span>
      </div>
      <div className="card">
        <strong>The demonstration&apos;s hardware</strong>
        <div className="row" style={{ gap: 8, marginTop: 8 }}>
          <label>Heart rate <input type="number" value={w.heartRate} onChange={(e) => w.setHeartRate(Number(e.target.value))} style={{ width: 70 }} /></label>
          <button className="grey" onClick={() => { void w.inject('heart'); }} data-testid="inject-heart">Send 3 readings over 2 minutes</button>
          <button className="grey" onClick={() => { void w.inject('still'); }} data-testid="inject-still">No movement</button>
          <button className="grey" onClick={() => { void w.fall(); }} disabled={w.fallCountdown !== null} data-testid="inject-fall">Fall</button>
          {w.onWrist ? <button className="grey" onClick={() => { void w.inject('off'); }} data-testid="inject-off">Remove watch</button>
            : <button className="grey" onClick={() => { void w.inject('on'); }}>Put watch back on</button>}
          <label className="muted"><input type="checkbox" checked={w.speak} onChange={(e) => w.presenterSet({ speak: e.target.checked })} /> voice on this computer</label>
        </div>
        <div className="row" style={{ gap: 8, marginTop: 8 }}>
          <span className="muted">Location sent with an SOS:</span>
          <label>home lat <input value={w.homeLat} onChange={(e) => w.presenterSet({ homeLat: e.target.value })} style={{ width: 90 }} /></label>
          <label>lon <input value={w.homeLon} onChange={(e) => w.presenterSet({ homeLon: e.target.value })} style={{ width: 90 }} /></label>
          <label><input type="checkbox" checked={w.away} onChange={(e) => w.presenterSet({ away: e.target.checked })} /> 2 km away from home</label>
        </div>
      </div>
      <div className="card" data-testid="log">
        <strong>What the watch said and heard</strong>
        {w.log.length === 0 ? <p className="muted">Nothing yet. Pair, then wait for a question or ask the dashboard to check in now.</p> : null}
        {w.log.map((l, i) => <p key={i} style={{ margin: '4px 0', color: l.who === 'note' ? 'var(--muted)' : l.who === 'you' ? 'var(--notice)' : 'var(--text)' }}>{l.who === 'ira' ? 'IRA: ' : l.who === 'you' ? 'You: ' : ''}{l.text}</p>)}
      </div>
    </div>
  );
}
