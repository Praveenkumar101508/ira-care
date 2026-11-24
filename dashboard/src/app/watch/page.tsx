'use client';

import { useEffect, useState } from 'react';
import { faceWords } from '@/lib/face';
import { useBrowserWatch } from '@/lib/browserWatch';

/**
 * The watch, in the browser, as the person sees it: a round face, the words
 * IRA says, I'm OK, Help, Talk to IRA. It talks to the same server as the real
 * watch. Nothing here is for the presenter: the controls a demo needs live on
 * /presenter, which is never shown in a recording.
 */
export default function WatchPage() {
  const w = useBrowserWatch();
  const [now, setNow] = useState('');
  useEffect(() => {
    const tick = () => setNow(new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }));
    tick();
    const t = setInterval(tick, 10_000);
    return () => clearInterval(t);
  }, []);
  const visit = w.sos && typeof (w.sos as unknown as { visit?: { state: string; carer_name: string; planned_start: string } }).visit === 'object'
    ? (w.sos as unknown as { visit: { state: string; carer_name: string; planned_start: string } }).visit : null;
  const { words, second: visitWords } = faceWords({ fallCountdown: w.fallCountdown, sos: w.sos, sosPending: w.sosPending, question: w.question?.question_text ?? null, visit });

  return (
    <main className="watch-page" data-testid="watch-page">
      <div className={`watch-case${w.flash ? ' buzz' : ''}`} data-testid="watch-face" aria-label="IRA CARE watch">
        <div className="watch-button watch-button-help" aria-hidden="true" />
        <div className="watch-button watch-button-side" aria-hidden="true" />
        <div className="watch-glass">
          {!w.client ? (
            <form className="watch-screen" onSubmit={(e) => { e.preventDefault(); void w.pair(); }}>
              <div className="watch-time">{now}</div>
              <div className="watch-words">Type the code from your care team to start.</div>
              <input className="watch-code" value={w.code} onChange={(e) => w.setCode(e.target.value)} placeholder="code" aria-label="Pairing code" autoCapitalize="none" />
              <button type="submit" className="watch-key watch-key-ok" disabled={!w.code}>Start</button>
            </form>
          ) : (
            <div className="watch-screen">
              <div className="watch-time">{now}{w.online === false ? ' · saving for later' : ''}</div>
              <div className="watch-words" role="status" aria-live="assertive" data-testid="watch-text">{words}</div>
              {visitWords && words === 'All well' ? <div className="watch-visit">{visitWords}</div> : null}
              <button className="watch-key watch-key-ok" data-testid="ok" onClick={() => { if (w.fallCountdown !== null) void w.fallOk(); else if (w.question) void w.answer('', 'button'); }}>I&apos;m OK</button>
              <button className="watch-key watch-key-help" data-testid="help" onClick={() => { void w.help(w.fallCountdown !== null ? 'fall' : 'button'); }}>Help</button>
              {!w.question && w.fallCountdown === null ? <button className="watch-key watch-key-talk" data-testid="talk-button" onClick={w.listen}>Talk to IRA</button> : null}
              <input className="watch-say" value={w.typed} onChange={(e) => w.setTyped(e.target.value)} placeholder={w.question ? 'Say your answer…' : 'Say something…'} aria-label={w.question ? 'Your answer' : 'Say something to IRA'} data-testid="typed"
                onKeyDown={(e) => { if (e.key === 'Enter' && w.typed.trim()) { if (w.question) void w.answer(w.typed.trim(), 'voice'); else void w.talk(w.typed.trim()); } }} />
            </div>
          )}
        </div>
      </div>
      <p className="watch-caption">The IRA CARE watch, in the browser. It speaks with the same server as the watch on the wrist.</p>
    </main>
  );
}
