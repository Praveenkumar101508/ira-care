import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { render, screen, within } from '@testing-library/react';
import { axe } from 'jest-axe';
import { vi } from 'vitest';
import { ResearchBanner, ResearchView } from '@/components/ResearchHeart';
import { Api } from '@/lib/api';
import {
  RESEARCH_BANNER, answerWords, boxOf, confidenceWords, estimateSeries, isResearchResult, scaledPolyline, seriesPoints,
  type ResearchResult, type ResearchWindow,
} from '@/lib/research';

const win = (i: number, extra: Partial<ResearchWindow> = {}): ResearchWindow => ({
  index: i, t_start_s: 2 * i, t_end_s: 2 * i + 8, status: 'estimate', bpm: 72, method: 'learned', confidence: 0.81,
  confidence_kind: 'probability_within_5_bpm', reason: null, reason_text: null, fallback: null,
  quality: { usable: true, motion_rms_g: 0.12, clipped_share: 0.01, spectral_concentration: 0.6, accelerometer: true },
  classical: { bpm: 74, candidate_bpm: 74, confidence: 0.6, reason: null },
  learned: { bpm: 72, candidate_bpm: 72, probability: 0.81, reason: null },
  reference_bpm: 71.5, processing_ms: 12.3, ...extra,
});

const unavailable = win(1, {
  status: 'unavailable', bpm: null, method: null, confidence: null, confidence_kind: null, reason: 'motion_artefact',
  reason_text: 'too much movement', quality: { usable: false, reason: 'motion_artefact', motion_rms_g: 1.1 },
});
const fallback = win(2, { method: 'classical', bpm: 74, confidence: 0.55, confidence_kind: 'spectral_concentration', fallback: 'learned_low_confidence',
  learned: { bpm: null, candidate_bpm: 90, probability: 0.3, reason: 'low_confidence' } });

function result(extra: Partial<ResearchResult> = {}): ResearchResult {
  return {
    kind: 'heart_research_result', source: 'research_recording', name: 'ppg_dalia_s1_excerpt',
    is_measurement: false, may_alert: false, stored: false, notice: 'Research: not a measurement of any person in care.',
    provenance: {
      pipeline: 'ira-heart-research-1',
      learned_model: { available: true, version: 'heart_selector@3b0863a9a8cf', sha256: '3b08', trained_on: 'PPG-DaLiA, all 15 subjects', dataset: 'PPG-DaLiA', dataset_licence: 'CC BY 4.0' },
      classical: { module: 'care.heart_estimate', min_confidence: 0.3 },
      recording: { source: 'research_recording', sample: 'ppg_dalia_s1_excerpt', attribution: 'A. Reiss et al., Sensors 2019. CC BY 4.0.', reference_kind: 'ECG-derived' },
    },
    contract: { window_s: 8, shift_s: 2, ppg_hz: 64, acc_hz: 32, seconds: 14, motion_reject_g: 0.5 },
    resources: { total_ms: 40, per_window_ms_mean: 13.3, per_window_ms_max: 15 },
    summary: { windows: 3, estimates: 2, unavailable: 1, unavailable_by_reason: { motion_artefact: 1 }, learned_estimates: 1, classical_fallbacks: 1 },
    windows: [win(0), unavailable, fallback],
    preview: { ppg: [[0, 1], [0.5, -1], [1, null], [1.5, 0.5]], ppg_unit: 'arbitrary', acc_magnitude_g: [[0, 1], [1, 1.2]] },
    ...extra,
  };
}

test('the page says plainly that this is research, not a measurement of this person', () => {
  render(<ResearchBanner />);
  expect(RESEARCH_BANNER).toBe('Research — not a measurement of this person');
  expect(screen.getByTestId('research-banner').textContent).toContain(RESEARCH_BANNER);
  expect(screen.getByTestId('research-banner').textContent).toMatch(/raise an alert/);
});

test('a research result shows the banner, the signal, both methods, quality and confidence', () => {
  render(<ResearchView result={result()} />);
  const view = screen.getByTestId('research-result');
  expect(within(view).getByTestId('research-banner')).toBeInTheDocument();
  expect(screen.getByRole('img', { name: 'Research signal (wrist PPG)' }).querySelector('polyline')).not.toBeNull();
  expect(screen.getByRole('img', { name: 'Heart-rate estimates' }).querySelectorAll('polyline').length).toBe(4);
  const rows = within(screen.getByTestId('research-windows')).getAllByRole('row').slice(1);
  expect(rows).toHaveLength(3);
  expect(rows[0].textContent).toContain('72.0 bpm (learned)');
  expect(rows[0].textContent).toContain('81% likely within 5 bpm');
  expect(rows[2].textContent).toContain('classical, because the learned answer was not confident enough');
  expect(screen.getByTestId('research-provenance').textContent).toContain('heart_selector@3b0863a9a8cf');
  expect(screen.getByTestId('research-provenance').textContent).toContain('CC BY 4.0');
});

test('an unavailable window shows its reason and never a number', () => {
  render(<ResearchView result={result()} />);
  const row = within(screen.getByTestId('research-windows')).getAllByRole('row')[2];
  const reported = row.querySelectorAll('td')[4].textContent ?? '';
  expect(reported).toBe('Unavailable: too much movement');
  expect(reported).not.toMatch(/\d/);
  expect(answerWords(unavailable)).toBe('Unavailable: too much movement');
  expect(answerWords(win(0))).toBe('72.0 bpm (learned)');               // negative control
  expect(confidenceWords(unavailable)).toBe('—');
});

test.each([
  ['a device reading', { source: 'device' }],
  ['a watch reading', { source: 'watch' }],
  ['something calling itself a measurement', { is_measurement: true }],
  ['something that may alert', { may_alert: true }],
  ['something stored', { stored: true }],
  ['a reading of another kind', { kind: 'heart_rate' as unknown as 'heart_research_result' }],
])('%s is refused, not drawn', (_name, change) => {
  const bad = { ...result(), ...change };
  expect(isResearchResult(bad)).toBe(false);
  render(<ResearchView result={bad} />);
  expect(screen.getByTestId('research-refused')).toBeInTheDocument();
  expect(screen.queryByTestId('research-windows')).toBeNull();
});

test('negative control: both research sources are accepted', () => {
  expect(isResearchResult(result())).toBe(true);
  expect(isResearchResult(result({ source: 'simulated' }))).toBe(true);
  expect(isResearchResult(null)).toBe(false);
});

test('the chart helpers drop gaps and share one scale across lines', () => {
  expect(seriesPoints([[0, 1], [1, null], [2, 3]])).toEqual([{ t: 0, v: 1 }, { t: 2, v: 3 }]);
  const s = estimateSeries([win(0), unavailable, fallback]);
  expect(s.reported.map((p) => p.v)).toEqual([72, 74]);
  expect(s.classical).toHaveLength(3);
  expect(s.learned.map((p) => p.v)).toEqual([72, 72, 90]);
  const box = boxOf(s.classical, s.learned);
  expect(box).toEqual({ t0: 4, t1: 8, v0: 72, v1: 90 });
  expect(scaledPolyline([{ t: 4, v: 90 }, { t: 8, v: 72 }], box, 100, 50, 0)).toBe('0.0,0.0 100.0,50.0');
});

test('the research result is accessible', async () => {
  const { container } = render(<ResearchView result={result()} />);
  expect(await axe(container)).toHaveNoViolations();
});

test('the api calls the research routes with the workspace', async () => {
  const f = vi.fn(async () => new Response(JSON.stringify({ notice: 'n', samples: [] }), { status: 200 }));
  const api = new Api({ baseUrl: 'https://api.example', accessToken: 'a', workspaceId: 'w1', role: 'workspace_admin' }, f);
  await api.researchSamples();
  await api.runResearch({ sample: 'simulated_rest' });
  const calls = f.mock.calls as unknown as [string, RequestInit][];
  expect(calls[0][0]).toBe('https://api.example/api/v1/care/research/heart/samples');
  expect(calls[1][0]).toBe('https://api.example/api/v1/care/research/heart/run');
  expect(calls[1][1].method).toBe('POST');
  expect(JSON.parse(String(calls[1][1].body))).toEqual({ sample: 'simulated_rest' });
  expect((calls[1][1].headers as Record<string, string>)['X-Workspace-ID']).toBe('w1');
});

test('research never appears on a person\'s record and the page has no inline script or style', () => {
  const src = join(__dirname, '..');
  const person = readFileSync(join(src, 'app', 'people', '[id]', 'page.tsx'), 'utf8');
  expect(person).not.toMatch(/research/i);
  for (const f of [join(src, 'app', 'research', 'heart', 'page.tsx'), join(src, 'components', 'ResearchHeart.tsx')]) {
    const text = readFileSync(f, 'utf8');
    expect(text).not.toMatch(/dangerouslySetInnerHTML|<script|style=\{\{/);
  }
});
