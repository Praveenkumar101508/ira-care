import { sosWords, type SosState } from '@/lib/device';

/** "All well" promises someone would hear about trouble; with no connection the watch says so instead. */
export const NO_CONNECTION = 'No connection. Help is saved and sent when it is back.';

export type VisitOnFace = { state: string; carer_name: string; planned_start: string } | null;

/**
 * What the watch face says, most pressing first: a fall countdown, a live SOS, a question,
 * a carer in the room, a recently closed SOS, then the day. The second line names a visit
 * that is coming.
 */
export function faceWords(a: { fallCountdown: number | null; sos: SosState | null; sosPending: 'sending' | 'waiting' | null; question: string | null; visit: VisitOnFace; online?: boolean | null }): { words: string; second: string | null } {
  const sosLine = sosWords(a.sos, a.sosPending);
  const sosActive = a.sosPending !== null || (a.sos !== null && a.sos.state !== 'closed' && a.sos.state !== 'idle');
  const words = a.fallCountdown !== null ? `Did you fall? Are you OK? Help in ${a.fallCountdown} s`
    : sosActive && sosLine ? sosLine
    : a.question ? a.question
    : a.visit?.state === 'here' ? `${a.visit.carer_name} is here.`
    : sosLine ?? (a.online === false ? NO_CONNECTION : 'All well');
  const second = a.visit?.state === 'expected' || a.visit?.state === 'late' ? `${a.visit.carer_name} is coming at ${a.visit.planned_start.slice(11, 16)}.` : null;
  return { words, second };
}
