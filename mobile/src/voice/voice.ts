/**
 * Voice in and out on the phone (remediation C2).
 *
 * Out: IRA's answer is read aloud as each sentence arrives (expo-speech, the platform's own
 * text-to-speech). In: the person taps Speak and says what they want; the phone's own speech
 * recogniser turns it into text (expo-speech-recognition 1.1.1, the Expo SDK 52 line), which is
 * then sent exactly as if typed.
 *
 * Both native modules are loaded behind a guard, so the web build, the tests and an older
 * install keep working: `listen` is then simply unavailable and the screen keeps typing only.
 * IRA keeps no recording: recognition runs one utterance at a time and only the text is sent,
 * the same as typing. The phone's own recogniser (Apple's or Google's) may process the audio on
 * its maker's servers under that maker's terms; the permission prompt says the phone does the
 * listening, and the DPIA must cover it before real use.
 */

export interface Speaker { speak(text: string, options?: { language?: string; rate?: number }): void; stop(): void }

export type Heard = { text: string } | { error: 'not_allowed' | 'nothing_heard' | 'unavailable' | 'failed' };

export interface Listener {
  available(): boolean;
  /** One utterance: resolves with the final text, or why there is none. Never throws. */
  listen(language: string): Promise<Heard>;
  stop(): void;
}

type SpeechModule = { speak(text: string, opts?: Record<string, unknown>): void; stop(): void };
type Subscription = { remove(): void };
type RecognitionModule = {
  isRecognitionAvailable(): boolean;
  requestPermissionsAsync(): Promise<{ granted: boolean }>;
  start(options: Record<string, unknown>): void;
  stop(): void;
  abort(): void;
  addListener(event: string, fn: (e: any) => void): Subscription; // eslint-disable-line @typescript-eslint/no-explicit-any
};

function load<T>(name: 'expo-speech' | 'expo-speech-recognition'): T | null {
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const mod = name === 'expo-speech' ? require('expo-speech') : require('expo-speech-recognition').ExpoSpeechRecognitionModule;
    return (mod ?? null) as T | null;
  } catch {
    return null;
  }
}

/** The phone's text-to-speech; a silent stand-in where it is missing. */
export function makeSpeaker(mod: SpeechModule | null = load<SpeechModule>('expo-speech')): Speaker {
  return {
    speak(text, options) {
      if (!mod || !text.trim()) return;
      try { mod.speak(text, { language: options?.language ?? 'en-GB', rate: options?.rate ?? 0.9 }); } catch { /* speech is a help, never a failure */ }
    },
    stop() { try { mod?.stop(); } catch { /* ignore */ } },
  };
}

/** The phone's speech recogniser, one utterance at a time. */
export function makeListener(mod: RecognitionModule | null = load<RecognitionModule>('expo-speech-recognition'), timeoutMs = 20_000): Listener {
  const available = () => { try { return !!mod && mod.isRecognitionAvailable(); } catch { return false; } };
  return {
    available,
    stop() { try { mod?.stop(); } catch { /* ignore */ } },
    async listen(language) {
      if (!mod || !available()) return { error: 'unavailable' };
      try {
        const perm = await mod.requestPermissionsAsync();
        if (!perm.granted) return { error: 'not_allowed' };
      } catch { return { error: 'not_allowed' }; }
      return new Promise<Heard>((resolve) => {
        let text = '';
        let settled = false;
        const subs: Subscription[] = [];
        const done = (h: Heard) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          subs.forEach((s) => { try { s.remove(); } catch { /* ignore */ } });
          resolve(h);
        };
        // A recogniser that never ends must not leave the screen listening for ever.
        const timer = setTimeout(() => { try { mod.abort(); } catch { /* ignore */ } done(text.trim() ? { text: text.trim() } : { error: 'nothing_heard' }); }, timeoutMs);
        subs.push(mod.addListener('result', (e: { isFinal?: boolean; results?: { transcript?: string }[] }) => {
          const t = e?.results?.[0]?.transcript ?? '';
          if (t) text = t;
          if (e?.isFinal && text.trim()) done({ text: text.trim() });
        }));
        subs.push(mod.addListener('error', (e: { error?: string }) => {
          done(e?.error === 'no-speech' || e?.error === 'speech-timeout' ? { error: 'nothing_heard' } : e?.error === 'not-allowed' ? { error: 'not_allowed' } : { error: 'failed' });
        }));
        subs.push(mod.addListener('end', () => done(text.trim() ? { text: text.trim() } : { error: 'nothing_heard' })));
        try {
          mod.start({ lang: language, interimResults: true, continuous: false, maxAlternatives: 1 });
        } catch {
          done({ error: 'failed' });
        }
      });
    },
  };
}

/** Plain words for the person when nothing usable was heard. */
export function heardWords(h: Heard): string | null {
  if ('text' in h) return null;
  return {
    not_allowed: 'The phone has not been allowed to use the microphone. You can type instead, or allow it in Settings.',
    nothing_heard: "I didn't catch that. Please tap Speak and try again, or type.",
    unavailable: 'Speaking is not available on this phone. Please type instead.',
    failed: "Something went wrong while listening. Please try again, or type.",
  }[h.error];
}
