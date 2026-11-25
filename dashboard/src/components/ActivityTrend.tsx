'use client';

import { useEffect, useState } from 'react';
import type { Api } from '@/lib/api';
import type { ActivityMinutes, ActivitySummary } from '@/lib/types';

/** The words carers read. The model's class names are never shown (care/activity.py, D-084). */
export const ACTIVITY_LABEL = "Estimated from the watch's motion sensor; not a measurement of health.";
export const GROUP_HEADINGS: { key: keyof ActivityMinutes; words: string }[] = [
  { key: 'active', words: 'Active (walking or on stairs)' },
  { key: 'still', words: 'Still (sitting or standing)' },
  { key: 'other_movement', words: 'Other arm movement' },
  { key: 'unknown_or_unavailable', words: 'Could not tell, not worn or not recorded' },
];

function hm(min: number): string {
  if (min < 60) return `${min} min`;
  return `${Math.floor(min / 60)} h ${min % 60} min`;
}

function dayWords(iso: string): string {
  const d = new Date(`${iso}T12:00:00Z`);
  return d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
}

/**
 * IRA Care Activity on the person's health tab: minutes a day and a week that look active or
 * still, from the watch's motion sensor. An estimate, labelled as one; shown only when the person
 * has agreed to share it; never coloured as good or bad and never an alert. One bar series (active
 * minutes per day) and a table with every group, so nothing depends on colour.
 */
export function ActivityTrend({ api, personId }: { api: Pick<Api, 'activity'>; personId: string }) {
  const [data, setData] = useState<ActivitySummary | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    api.activity(personId).then((d) => { if (live) setData(d); }).catch(() => { if (live) setFailed(true); });
    return () => { live = false; };
  }, [api, personId]);

  if (failed) return null;
  if (!data) return null;
  if (!data.sharing) {
    return (
      <div className="card" data-testid="activity-trend">
        <strong>Activity (estimated)</strong>
        <p className="muted" data-testid="activity-not-shared">Not shared. The person has not agreed to share the watch&apos;s activity estimate, so nothing is sent or shown.</p>
      </div>
    );
  }
  const max = Math.max(60, ...data.days.map((d) => d.minutes.active));
  const W = 260, rowH = 22, labelW = 90;
  return (
    <div className="card" data-testid="activity-trend">
      <strong>Activity (estimated)</strong>
      <p className="muted" data-testid="activity-label">{data.label}</p>
      {data.sources.includes('simulated') ? <p className="muted" data-testid="activity-simulated">Includes simulated input, not the watch&apos;s own sensors.</p> : null}
      <svg width={labelW + W + 70} height={data.days.length * rowH} role="img" aria-label="Active minutes per day, last seven days" className="activity-bars">
        {data.days.map((d, i) => {
          const w = Math.round((d.minutes.active / max) * W);
          return (
            <g key={d.date} transform={`translate(0 ${i * rowH})`}>
              <text x={0} y={15} className="activity-axis">{dayWords(d.date)}</text>
              {w > 0 ? <rect x={labelW} y={4} width={w} height={14} rx={4} className="activity-bar"><title>{`${dayWords(d.date)}: ${hm(d.minutes.active)} active`}</title></rect> : null}
              <text x={labelW + w + 6} y={15} className="activity-axis">{hm(d.minutes.active)}</text>
            </g>
          );
        })}
      </svg>
      <table data-testid="activity-days">
        <thead><tr><th>Day</th>{GROUP_HEADINGS.map((g) => <th key={g.key}>{g.words}</th>)}</tr></thead>
        <tbody>
          {data.days.map((d) => (
            <tr key={d.date}><td>{dayWords(d.date)}</td>{GROUP_HEADINGS.map((g) => <td key={g.key}>{hm(d.minutes[g.key])}</td>)}</tr>
          ))}
        </tbody>
      </table>
      <table data-testid="activity-weeks">
        <thead><tr><th>Week ending</th>{GROUP_HEADINGS.map((g) => <th key={g.key}>{g.words}</th>)}</tr></thead>
        <tbody>
          {data.weeks.map((w) => (
            <tr key={w.end}><td>{dayWords(w.end)}</td>{GROUP_HEADINGS.map((g) => <td key={g.key}>{hm(w.minutes[g.key])}</td>)}</tr>
          ))}
        </tbody>
      </table>
      <p className="muted">It can be wrong, and it does not tell whether someone ate, drank or is unwell. It never raises an alert.</p>
    </div>
  );
}
