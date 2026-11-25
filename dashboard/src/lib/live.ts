/** Keeping a person's record live, and saying honestly how live it is.
 *
 *  LiveGate      one load at a time per person, and an answer that arrives after the reader
 *                has moved to another person is dropped, never shown on the wrong record.
 *  liveWords     what the record says about its own freshness: live, reconnecting, or not
 *                connected, with when it last updated.
 *  settleAll     the record's sections loaded side by side; one that fails is named, the
 *                others still show.
 */

export const REFRESH_MS = 30_000;
/** After this long without a successful refresh the record says it may be out of date. */
export const STALE_AFTER_MS = 90_000;

export class LiveGate {
  private gen = 0;
  private busy = -1;

  /** The reader moved to another person (or signed out): everything in flight is now stale. */
  next(): number {
    this.gen += 1;
    this.busy = -1;
    return this.gen;
  }

  /** Start a load, or null when one for this person is already running (no overlap). */
  begin(): number | null {
    if (this.busy === this.gen) return null;
    this.busy = this.gen;
    return this.gen;
  }

  end(ticket: number): void {
    if (this.busy === ticket) this.busy = -1;
  }

  /** Is an answer for `ticket` still about what the reader is looking at? */
  current(ticket: number): boolean {
    return ticket === this.gen;
  }
}

export interface LiveStatus {
  lastOk: Date | null;
  failures: number;
}

export function liveWords(s: LiveStatus, now = new Date()): { state: 'live' | 'reconnecting' | 'offline' | 'loading'; words: string } {
  if (!s.lastOk) return s.failures > 0 ? { state: 'offline', words: 'Not connected: this record could not be loaded. Trying again.' } : { state: 'loading', words: 'Loading…' };
  const age = Math.max(0, Math.round((now.getTime() - s.lastOk.getTime()) / 1000));
  const ago = age < 60 ? `${age} s ago` : `${Math.round(age / 60)} min ago`;
  if (s.failures > 0 || now.getTime() - s.lastOk.getTime() > STALE_AFTER_MS) {
    return { state: 'reconnecting', words: `Connection lost: reconnecting. What you see was last updated ${ago} and may be out of date.` };
  }
  return { state: 'live', words: `Live: updated ${ago}` };
}

/** Run every loader side by side. Values for those that worked; the names of those that failed. */
export async function settleAll<T extends Record<string, () => Promise<unknown>>>(loaders: T): Promise<{ values: Partial<{ [K in keyof T]: Awaited<ReturnType<T[K]>> }>; failed: (keyof T)[] }> {
  const keys = Object.keys(loaders) as (keyof T)[];
  const results = await Promise.allSettled(keys.map((k) => loaders[k]()));
  const values: Partial<{ [K in keyof T]: Awaited<ReturnType<T[K]>> }> = {};
  const failed: (keyof T)[] = [];
  results.forEach((r, i) => {
    if (r.status === 'fulfilled') (values as Record<keyof T, unknown>)[keys[i]] = r.value;
    else failed.push(keys[i]);
  });
  return { values, failed };
}
