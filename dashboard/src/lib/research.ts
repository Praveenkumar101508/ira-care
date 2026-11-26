/**
 * Research mode for heart rate (D-086): what the backend's research pipeline returns, and the
 * small pure helpers the research page draws with. A research result is never a reading of a
 * person: it comes from a recorded research excerpt or a simulation, it is not stored, and it
 * can raise no alert. `isResearchResult` refuses anything else, so the page cannot be used to
 * show a device reading as if it were research, or research as if it were a reading.
 */

export const RESEARCH_SOURCES = ['simulated', 'research_recording'] as const;
export type ResearchSource = (typeof RESEARCH_SOURCES)[number];

export const RESEARCH_BANNER = 'Research — not a measurement of this person';

export interface ResearchSample {
  name: string; source: ResearchSource; title: string; seconds: number | null;
  dataset: string | null; licence: string | null; attribution: string | null;
}

export interface ResearchWindow {
  index: number; t_start_s: number; t_end_s: number;
  status: 'estimate' | 'unavailable'; bpm: number | null; method: 'learned' | 'classical' | null;
  confidence: number | null; confidence_kind: 'probability_within_5_bpm' | 'spectral_concentration' | null;
  reason: string | null; reason_text: string | null; fallback: string | null;
  quality: { usable: boolean; reason?: string; motion_rms_g?: number | null; clipped_share?: number | null; spectral_concentration?: number | null; accelerometer?: boolean };
  classical: { bpm: number | null; candidate_bpm: number | null; confidence: number | null; reason: string | null } | null;
  learned: { bpm: number | null; candidate_bpm: number | null; probability: number | null; reason: string | null } | null;
  reference_bpm: number | null; processing_ms: number;
}

export interface ResearchResult {
  kind: 'heart_research_result'; source: string; name: string | null;
  is_measurement: boolean; may_alert: boolean; stored: boolean; notice: string;
  provenance: {
    pipeline: string;
    learned_model: { available: boolean; reason?: string; version?: string; sha256?: string; trained_on?: string; dataset?: string; dataset_licence?: string };
    classical: { module: string; min_confidence: number };
    recording: { source: string; sample: string | null; dataset?: string; licence?: string; attribution?: string; reference_kind?: string };
  };
  contract: { window_s: number; shift_s: number; ppg_hz: number; acc_hz: number | null; seconds: number; motion_reject_g: number };
  resources: { total_ms: number; per_window_ms_mean: number; per_window_ms_max: number };
  summary: {
    windows: number; estimates: number; unavailable: number; unavailable_by_reason: Record<string, number>;
    learned_estimates: number; classical_fallbacks: number;
  };
  windows: ResearchWindow[];
  preview: { ppg: [number, number | null][]; ppg_unit: string; acc_magnitude_g?: [number, number | null][] };
}

/** Only a result that says it is research, from a research source, never stored and never alerting. */
export function isResearchResult(r: unknown): r is ResearchResult {
  if (!r || typeof r !== 'object') return false;
  const x = r as Partial<ResearchResult>;
  return x.kind === 'heart_research_result'
    && (RESEARCH_SOURCES as readonly string[]).includes(String(x.source))
    && x.is_measurement === false && x.may_alert === false && x.stored === false
    && Array.isArray(x.windows);
}

export const SOURCE_WORDS: Record<ResearchSource, string> = {
  simulated: 'Simulated signal',
  research_recording: 'Recorded research data (public dataset)',
};

/** Why the classical estimator answered. Only where the learned selector cannot run at all: when it
 * ran and was unsure, the window is unavailable (`learned_low_confidence`), never answered by the
 * classical estimator instead (D-094). */
export const FALLBACK_WORDS: Record<string, string> = {
  no_model: 'the learned model is not available',
  no_accelerometer: 'there is no accelerometer signal',
};

export const LEARNED_UNSURE_WORDS =
  'When the learned selector runs but is not confident, the window is unavailable: the classical estimate is not reported in its place.';

/** The reported answer in words: a number only when there is one. */
export function answerWords(w: ResearchWindow): string {
  if (w.status !== 'estimate' || w.bpm === null) return `Unavailable: ${w.reason_text ?? 'no reliable estimate'}`;
  const how = w.method === 'learned' ? 'learned' : `classical, because ${FALLBACK_WORDS[w.fallback ?? ''] ?? 'the learned answer was not used'}`;
  return `${w.bpm.toFixed(1)} bpm (${how})`;
}

export function confidenceWords(w: ResearchWindow): string {
  if (w.confidence === null) return '—';
  const pct = Math.round(w.confidence * 100);
  return w.confidence_kind === 'probability_within_5_bpm' ? `${pct}% likely within 5 bpm` : `${pct}% of the spectrum at the peak`;
}

/** Points for a chart from [t, value] pairs, gaps (null) dropped. */
export function seriesPoints(pairs: [number, number | null][] | undefined): { t: number; v: number }[] {
  return (pairs ?? []).filter((p): p is [number, number] => p[1] !== null && Number.isFinite(p[1])).map(([t, v]) => ({ t, v }));
}

/** Per-window lines for the estimate chart: each method's candidate, the reported answer, the reference. */
export function estimateSeries(windows: ResearchWindow[]) {
  const mid = (w: ResearchWindow) => (w.t_start_s + w.t_end_s) / 2;
  const pick = (f: (w: ResearchWindow) => number | null | undefined) =>
    windows.flatMap((w) => { const v = f(w); return v === null || v === undefined ? [] : [{ t: mid(w), v }]; });
  return {
    classical: pick((w) => w.classical?.candidate_bpm),
    learned: pick((w) => w.learned?.candidate_bpm),
    reported: pick((w) => (w.status === 'estimate' ? w.bpm : null)),
    reference: pick((w) => w.reference_bpm),
  };
}

/** One scale for several lines, so they can be compared: x over [t0, t1], y over [v0, v1]. */
export function scaledPolyline(points: { t: number; v: number }[], box: { t0: number; t1: number; v0: number; v1: number },
  width: number, height: number, pad = 4): string {
  const x = (t: number) => (box.t1 === box.t0 ? width / 2 : pad + ((t - box.t0) / (box.t1 - box.t0)) * (width - 2 * pad));
  const y = (v: number) => (box.v1 === box.v0 ? height / 2 : height - pad - ((v - box.v0) / (box.v1 - box.v0)) * (height - 2 * pad));
  return points.map((p) => `${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ');
}

export function boxOf(...series: { t: number; v: number }[][]) {
  const all = series.flat();
  if (all.length === 0) return { t0: 0, t1: 1, v0: 0, v1: 1 };
  const ts = all.map((p) => p.t), vs = all.map((p) => p.v);
  return { t0: Math.min(...ts), t1: Math.max(...ts), v0: Math.min(...vs), v1: Math.max(...vs) };
}
