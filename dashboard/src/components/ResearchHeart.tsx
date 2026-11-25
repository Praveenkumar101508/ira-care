import {
  FALLBACK_WORDS, RESEARCH_BANNER, SOURCE_WORDS, answerWords, boxOf, confidenceWords, estimateSeries, isResearchResult,
  scaledPolyline, seriesPoints, type ResearchResult, type ResearchSource,
} from '@/lib/research';

/** Shown above everything on the research page and above every result. */
export function ResearchBanner() {
  return (
    <div className="research-banner" role="note" data-testid="research-banner">
      <strong>{RESEARCH_BANNER}</strong>
      <p>
        These heart-rate estimates come from a recorded research excerpt or a simulated signal, never from anyone in
        your care. Nothing here is stored in anyone&apos;s record, shown with their readings, or able to raise an alert.
      </p>
    </div>
  );
}

const W = 640, H = 120;

function Lines({ label, lines, unit }: { label: string; unit: string; lines: { name: string; cls: string; points: { t: number; v: number }[] }[] }) {
  const box = boxOf(...lines.map((l) => l.points));
  return (
    <figure className="research-chart">
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label={label} preserveAspectRatio="none">
        {lines.map((l) => (l.points.length ? <polyline key={l.name} className={`research-line ${l.cls}`} fill="none" points={scaledPolyline(l.points, box, W, H)} /> : null))}
      </svg>
      <figcaption className="muted">
        {label}: {box.v0.toFixed(unit === 'bpm' ? 0 : 2)} to {box.v1.toFixed(unit === 'bpm' ? 0 : 2)} {unit}, over {Math.round(box.t1 - box.t0)} s.
        {lines.length > 1 ? <span className="research-legend">{lines.map((l) => <span key={l.name} className={`research-key ${l.cls}`}>{l.name}</span>)}</span> : null}
      </figcaption>
    </figure>
  );
}

export function ResearchView({ result }: { result: unknown }) {
  if (!isResearchResult(result)) {
    return <p className="error" data-testid="research-refused">This result is not research data, so it is not shown here.</p>;
  }
  const r: ResearchResult = result;
  const s = estimateSeries(r.windows);
  const m = r.provenance.learned_model;
  const rec = r.provenance.recording;
  const reasons = Object.entries(r.summary.unavailable_by_reason);
  return (
    <section aria-label="Research result" data-testid="research-result">
      <ResearchBanner />
      <p>
        <span className="pill grey">{SOURCE_WORDS[r.source as ResearchSource]}</span>{' '}
        {r.contract.seconds.toFixed(0)} s, {r.summary.windows} windows of {r.contract.window_s} s every {r.contract.shift_s} s.
      </p>
      <Lines label="Research signal (wrist PPG)" unit={r.preview.ppg_unit} lines={[{ name: 'PPG', cls: 'line-signal', points: seriesPoints(r.preview.ppg) }]} />
      {r.preview.acc_magnitude_g ? (
        <Lines label="Wrist movement (accelerometer)" unit="g" lines={[{ name: 'Movement', cls: 'line-motion', points: seriesPoints(r.preview.acc_magnitude_g) }]} />
      ) : null}
      <Lines label="Heart-rate estimates" unit="bpm" lines={[
        { name: 'Classical', cls: 'line-classical', points: s.classical },
        { name: 'Learned', cls: 'line-learned', points: s.learned },
        { name: 'Reported', cls: 'line-reported', points: s.reported },
        { name: 'Recording reference', cls: 'line-reference', points: s.reference },
      ]} />
      <div className="card" data-testid="research-summary">
        <p>
          {r.summary.estimates} of {r.summary.windows} windows have an estimate: {r.summary.learned_estimates} from the learned
          selector and {r.summary.classical_fallbacks} from the classical estimator as a fallback.
        </p>
        {reasons.length ? (
          <p>Unavailable: {reasons.map(([k, n]) => `${n} for ${r.windows.find((w) => w.reason === k)?.reason_text ?? k}`).join('; ')}.</p>
        ) : <p>No window was unavailable.</p>}
        <p className="muted">Processing took {r.resources.per_window_ms_mean.toFixed(1)} ms a window on average.</p>
      </div>
      <div className="research-table-wrap">
        <table className="research-table" data-testid="research-windows">
          <caption className="muted">Every window: signal quality, both methods, the reported answer and its confidence.</caption>
          <thead>
            <tr><th scope="col">Time</th><th scope="col">Quality</th><th scope="col">Classical</th><th scope="col">Learned</th><th scope="col">Reported</th><th scope="col">Confidence</th><th scope="col">Reference</th></tr>
          </thead>
          <tbody>
            {r.windows.map((w) => (
              <tr key={w.index} className={w.status === 'estimate' ? '' : 'research-unavailable'}>
                <td>{w.t_start_s.toFixed(0)}–{w.t_end_s.toFixed(0)} s</td>
                <td>{w.quality.usable ? 'usable' : `unusable: ${w.reason_text ?? w.reason}`}{w.quality.motion_rms_g != null ? `, movement ${w.quality.motion_rms_g.toFixed(2)} g` : ''}</td>
                <td>{w.classical?.candidate_bpm != null ? `${w.classical.candidate_bpm.toFixed(1)}` : '—'}</td>
                <td>{w.learned?.candidate_bpm != null ? `${w.learned.candidate_bpm.toFixed(1)}` : '—'}</td>
                <td>{answerWords(w)}</td>
                <td>{confidenceWords(w)}</td>
                <td>{w.reference_bpm != null ? w.reference_bpm.toFixed(1) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="card" data-testid="research-provenance">
        <h3>Where this came from</h3>
        <ul>
          <li>Pipeline {r.provenance.pipeline}.</li>
          <li>{m.available ? `Learned selector ${m.version}, trained on ${m.trained_on ?? m.dataset} (${m.dataset_licence}).` : `Learned selector not used: ${m.reason}.`}</li>
          <li>Classical estimator: no trained weights; minimum confidence {r.provenance.classical.min_confidence}.</li>
          <li>Recording: {SOURCE_WORDS[r.source as ResearchSource]}{rec.sample ? `, sample “${rec.sample}”` : ', uploaded'}.{rec.reference_kind ? ` Reference: ${rec.reference_kind}.` : ''}</li>
          {rec.attribution ? <li className="muted">{rec.attribution}</li> : null}
        </ul>
        <p className="muted">A fallback to the classical estimator happens when {Object.values(FALLBACK_WORDS).join(', or ')}.</p>
      </div>
    </section>
  );
}
