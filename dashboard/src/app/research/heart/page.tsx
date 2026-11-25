'use client';

import { useCallback, useEffect, useState } from 'react';
import { ResearchBanner, ResearchView } from '@/components/ResearchHeart';
import { useApi } from '@/components/useApi';
import { ApiError } from '@/lib/api';
import { may } from '@/lib/carePlan';
import type { ResearchResult, ResearchSample } from '@/lib/research';
import type { Permissions } from '@/lib/types';

/** Research mode for heart rate (D-086). Care managers only; never linked from a person's record. */
export default function ResearchHeartPage() {
  const { api, ready } = useApi();
  const [perms, setPerms] = useState<Permissions | null>(null);
  const [samples, setSamples] = useState<ResearchSample[]>([]);
  const [chosen, setChosen] = useState('');
  const [result, setResult] = useState<ResearchResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    if (!api) return;
    try {
      const p = await api.permissions();
      setPerms(p);
      if (!may(p, 'run_heart_research')) return;
      const s = await api.researchSamples();
      setSamples(s.samples);
      setChosen((c) => c || s.samples[0]?.name || '');
    } catch (e) { setError(e instanceof ApiError ? e.message : 'Could not load.'); }
  }, [api]);
  useEffect(() => { void load(); }, [load]);
  if (!ready) return null;

  const runWith = async (body: { sample: string } | { recording: unknown }) => {
    if (!api) return;
    setBusy(true); setError('');
    try { setResult(await api.runResearch(body)); }
    catch (e) { setResult(null); setError(e instanceof ApiError ? e.message : 'Could not run.'); }
    finally { setBusy(false); }
  };
  const onFile = async (file: File | undefined) => {
    if (!file) return;
    try { await runWith({ recording: JSON.parse(await file.text()) }); }
    catch { setError('That file is not a recording this page can read.'); }
  };

  return (
    <>
      <h2>Heart-rate research</h2>
      <ResearchBanner />
      {error ? <p className="error" role="alert">{error}</p> : null}
      {perms && !may(perms, 'run_heart_research') ? (
        <p data-testid="research-not-allowed">Only a care manager can open the research tools.</p>
      ) : null}
      {perms && may(perms, 'run_heart_research') ? (
        <div className="card">
          <label htmlFor="research-sample">Recording</label>{' '}
          <select id="research-sample" value={chosen} onChange={(e) => setChosen(e.target.value)}>
            {samples.map((s) => <option key={s.name} value={s.name}>{s.title}</option>)}
          </select>{' '}
          <button disabled={busy || !chosen} onClick={() => void runWith({ sample: chosen })}>{busy ? 'Running…' : 'Run'}</button>
          <p className="muted">
            <label htmlFor="research-file">Or run a recording file of your own (recorded or simulated, never a person&apos;s data): </label>
            <input id="research-file" type="file" accept=".json,application/json" disabled={busy} onChange={(e) => void onFile(e.target.files?.[0])} />
          </p>
        </div>
      ) : null}
      {result ? <ResearchView result={result} /> : null}
    </>
  );
}
