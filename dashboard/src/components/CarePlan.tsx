'use client';
import { useState } from 'react';
import { WHO_WORDS, draftFrom, draftProblems, reviewWords } from '@/lib/carePlan';
import type { CarePlan, CarePlanDraft, PlanWho } from '@/lib/types';

/** The person's care plan: the current version, every earlier version with who changed it and
 *  why, and, for a care manager, a form that saves a NEW version (the old one is kept). */
export function CarePlanSection({ plan, history, canEdit, busy, onSave }: {
  plan: CarePlan | null; history: CarePlan[]; canEdit: boolean; busy: boolean; onSave: (d: CarePlanDraft) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [shown, setShown] = useState<number | null>(null);
  const old = shown !== null ? history.find((v) => v.version === shown) ?? null : null;

  return (
    <div data-testid="care-plan">
      {plan ? <PlanView plan={plan} title="Current care plan" /> : (
        <div className="card"><strong>No care plan written yet.</strong>
          <div className="muted">{canEdit ? 'Write the first one below.' : 'A care manager writes the plan; it will show here.'}</div></div>
      )}
      {canEdit && !editing ? <button disabled={busy} onClick={() => setEditing(true)}>{plan ? 'Change the plan' : 'Write the care plan'}</button> : null}
      {!canEdit && plan ? <p className="muted">Only a care manager can change the plan.</p> : null}
      {canEdit && editing ? (
        <PlanForm start={draftFrom(plan)} busy={busy} onCancel={() => setEditing(false)}
          onSave={async (d) => { try { await onSave(d); setEditing(false); } catch { /* keep the draft; the page shows why */ } }} />
      ) : null}
      <div className="card">
        <strong>Earlier versions</strong>
        {history.length <= 1 ? <div className="muted">None yet: each change is kept here, with who made it and why.</div> : (
          <table><thead><tr><th>Version</th><th>Saved</th><th>By</th><th>Why</th><th /></tr></thead><tbody>
            {history.map((v) => (
              <tr key={v.id}><td>{v.version}{plan && v.version === plan.version ? ' (current)' : ''}</td>
                <td className="muted">{v.created_at.slice(0, 16).replace('T', ' ')}</td><td>{v.author_name || '-'}</td><td>{v.reason}</td>
                <td>{plan && v.version !== plan.version ? <button onClick={() => setShown(shown === v.version ? null : v.version)}>{shown === v.version ? 'Hide' : 'Show'}</button> : null}</td></tr>
            ))}
          </tbody></table>
        )}
      </div>
      {old ? <PlanView plan={old} title={`Version ${old.version} (no longer current)`} /> : null}
    </div>
  );
}

function PlanView({ plan, title }: { plan: CarePlan; title: string }) {
  const review = reviewWords(plan);
  return (
    <div className="card" data-testid={`plan-v${plan.version}`}>
      <div className="row"><strong>{title}</strong> <span className="muted">version {plan.version}, saved {plan.created_at.slice(0, 10)}{plan.author_name ? ` by ${plan.author_name}` : ''}</span>
        <span className={`pill ${review.overdue ? 'urgent' : 'grey'}`}>{review.words}</span></div>
      <Section label="What the care is for" text={plan.goals} />
      <Section label="Support needed" text={plan.support} />
      <div><strong>Daily routine</strong>
        {plan.routine.length ? (
          <table><thead><tr><th>What</th><th>When</th><th>Who</th></tr></thead><tbody>
            {plan.routine.map((r, i) => <tr key={i}><td>{r.what}</td><td>{r.when || '-'}</td><td>{WHO_WORDS[r.who] ?? r.who}</td></tr>)}
          </tbody></table>
        ) : <div className="muted">Not written.</div>}
      </div>
      <Section label="Risks to watch for" text={plan.risks} />
      <Section label="In an emergency" text={plan.emergency} />
      <div className="muted">Why this version: {plan.reason}</div>
    </div>
  );
}

function Section({ label, text }: { label: string; text: string }) {
  return <div><strong>{label}</strong><div style={{ whiteSpace: 'pre-wrap' }}>{text || <span className="muted">Not written.</span>}</div></div>;
}

function PlanForm({ start, busy, onSave, onCancel }: { start: CarePlanDraft; busy: boolean; onSave: (d: CarePlanDraft) => Promise<void>; onCancel: () => void }) {
  const [d, setD] = useState<CarePlanDraft>(start);
  const problems = draftProblems(d);
  const set = (k: keyof CarePlanDraft) => (e: { target: { value: string } }) => setD({ ...d, [k]: e.target.value });
  const setLine = (i: number, k: 'what' | 'when' | 'who', v: string) =>
    setD({ ...d, routine: d.routine.map((r, j) => (j === i ? { ...r, [k]: v } : r)) });
  return (
    <form className="card" data-testid="plan-form" onSubmit={(e) => { e.preventDefault(); if (!problems.length) void onSave({ ...d, review_on: d.review_on || null }); }}>
      <label>What the care is for<textarea value={d.goals} onChange={set('goals')} maxLength={2000} /></label>
      <label>Support needed<textarea value={d.support} onChange={set('support')} maxLength={4000} /></label>
      <fieldset><legend>Daily routine</legend>
        {d.routine.map((r, i) => (
          <div className="row" key={i}>
            <input aria-label={`Routine ${i + 1}: what`} placeholder="What" value={r.what} onChange={(e) => setLine(i, 'what', e.target.value)} maxLength={200} />
            <input aria-label={`Routine ${i + 1}: when`} placeholder="When, e.g. 08:00" value={r.when} onChange={(e) => setLine(i, 'when', e.target.value)} maxLength={60} />
            <select aria-label={`Routine ${i + 1}: who`} value={r.who} onChange={(e) => setLine(i, 'who', e.target.value as PlanWho)}>
              {(Object.keys(WHO_WORDS) as PlanWho[]).map((w) => <option key={w} value={w}>{WHO_WORDS[w]}</option>)}
            </select>
            <button type="button" onClick={() => setD({ ...d, routine: d.routine.filter((_, j) => j !== i) })}>Remove</button>
          </div>
        ))}
        <button type="button" onClick={() => setD({ ...d, routine: [...d.routine, { what: '', when: '', who: 'carer' }] })}>Add a routine line</button>
      </fieldset>
      <label>Risks to watch for<textarea value={d.risks} onChange={set('risks')} maxLength={4000} /></label>
      <label>In an emergency<textarea value={d.emergency} onChange={set('emergency')} maxLength={2000} /></label>
      <label>Next review<input type="date" value={d.review_on ?? ''} onChange={set('review_on')} /></label>
      <label>Why the plan is changing<input value={d.reason} onChange={set('reason')} maxLength={500} required /></label>
      {problems.length ? <ul className="error">{problems.map((p) => <li key={p}>{p}</li>)}</ul> : null}
      <div className="row"><button type="submit" disabled={busy || problems.length > 0}>Save as a new version</button><button type="button" onClick={onCancel}>Cancel</button></div>
    </form>
  );
}
